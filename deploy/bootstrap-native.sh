#!/usr/bin/env bash
# 服务器原生部署（Ubuntu 26.04 / Python 3.14 / PostgreSQL 18 + pgvector）
#
# 为什么不用 Docker：这台机器的 Docker Hub 出口不通（SOCKS5 报错），而 apt 与 PyPI
# 都通。原生部署少一层依赖，也少一类"镜像拉不到"的故障。
#
# 为什么 OR-Tools 是 9.15 而不是 9.11：9.11.4210 没有 cp313/cp314 轮子，而本机只有
# Python 3.14；9.15.6755 提供 cp314 manylinux 轮子，且本项目只用 CP-SAT 稳定 API。
#
# 用法（在仓库根目录）：
#   bash deploy/bootstrap-native.sh              # 装依赖 + 建库 + 迁移 + 起服务
#   bash deploy/bootstrap-native.sh --check      # 只检查环境，不动任何东西
#
# 幂等：重复执行会跳过已装的包、保留已有 .env、迁移按 checksum 跳过。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

say()  { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
die()  { printf '\n\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

PYBIN="$(command -v python3.14 || command -v python3)"
VENV="$ROOT/.venv"
VENV_PY="$VENV/bin/python"

say "0. 环境"
ok "仓库：$ROOT"
ok "$($PYBIN -V 2>&1)"
ok "系统：$(. /etc/os-release && echo "$PRETTY_NAME")"
command -v psql >/dev/null && ok "psql：$(psql --version)" || warn "还没装 psql"
[ -x "$VENV_PY" ] && ok "已有虚拟环境：$($VENV_PY -V 2>&1)" || warn "还没有虚拟环境"

if [ "$CHECK_ONLY" -eq 1 ]; then
  say "只检查：上面缺什么就装什么"
  echo "  sudo apt install -y postgresql-18 postgresql-18-pgvector python3.14-venv python3.14-dev build-essential libpq-dev"
  exit 0
fi

say "1. 系统包（PostgreSQL 18 + pgvector + 编译工具）"
sudo apt update -qq
sudo apt install -y postgresql-18 postgresql-18-pgvector python3.14-venv python3.14-dev \
                    build-essential libpq-dev curl git
ok "系统包就绪"

say "2. 生成 .env（已存在则保留）"
DB_USER="mathagent"
DB_NAME="mathagent"
if [ ! -f .env ]; then
  DB_PASS="$(head -c 32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 24)"
  cat > .env <<EOF
# 由 deploy/bootstrap-native.sh 生成。对外前务必改 AUTH_MODE / APP_ENV / CORS_ORIGINS。
DATABASE_URL=postgresql://${DB_USER}:${DB_PASS}@127.0.0.1:5432/${DB_NAME}
DB_POOL_MAX=8
CORS_ORIGINS=http://127.0.0.1:8000
AUTH_MODE=dev
AUTH_DEV_ROLES=admin
APP_ENV=dev
SECRETS_DIR=./secrets
EOF
  chmod 600 .env
  ok ".env 已生成（数据库口令随机）"
else
  ok ".env 已存在，保留"
fi
mkdir -p secrets
DB_PASS="$(grep -oP '(?<=^DATABASE_URL=postgresql://[^:]+:)[^@]+' .env || true)"
[ -n "$DB_PASS" ] || die ".env 里的 DATABASE_URL 格式看不懂（应为 postgresql://user:pass@host:port/db）"

say "3. 建库与扩展"
sudo systemctl enable --now postgresql >/dev/null 2>&1 || true
sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='${DB_USER}'" | grep -q 1 \
  || sudo -u postgres psql -c "CREATE USER ${DB_USER} WITH PASSWORD '${DB_PASS}'"
sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'" | grep -q 1 \
  || sudo -u postgres psql -c "CREATE DATABASE ${DB_NAME} OWNER ${DB_USER}"
# 口令可能与 .env 不同步（比如 .env 里是旧的）：以 .env 为准显式重设一次
sudo -u postgres psql -c "ALTER USER ${DB_USER} WITH PASSWORD '${DB_PASS}'" >/dev/null
sudo -u postgres psql -d "${DB_NAME}" -c "CREATE EXTENSION IF NOT EXISTS vector" >/dev/null
sudo -u postgres psql -d "${DB_NAME}" -c "GRANT ALL ON SCHEMA public TO ${DB_USER}" >/dev/null
ok "库 ${DB_NAME} 与扩展 vector 就绪"

say "4. Python 虚拟环境与依赖"
[ -x "$VENV_PY" ] || "$PYBIN" -m venv "$VENV"
"$VENV_PY" -m pip install -q -U pip
"$VENV_PY" -m pip install -q -r server/requirements.txt
"$VENV_PY" -m pip install -q -r server/requirements-assembly.txt
ok "依赖装好：$("$VENV_PY" -c "from importlib.metadata import version as v; print('ortools', v('ortools'))")"

say "5. 迁移"
"$VENV_PY" db/migrate.py --apply
"$VENV_PY" db/migrate.py --status | tail -3

say "6. 起服务（systemd）"
sudo tee /etc/systemd/system/mathagent-api.service >/dev/null <<EOF
[Unit]
Description=mathagent API
After=network.target postgresql.service
Wants=postgresql.service

[Service]
Type=simple
User=$(id -un)
WorkingDirectory=${ROOT}
EnvironmentFile=${ROOT}/.env
ExecStart=${VENV}/bin/uvicorn app.main:app --app-dir server --host 127.0.0.1 --port 8000 --proxy-headers
Restart=always
RestartSec=3
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF

sudo tee /etc/systemd/system/mathagent-worker.service >/dev/null <<EOF
[Unit]
Description=mathagent worker（组卷求解 / 渲染 / 生成）
After=network.target postgresql.service
Wants=postgresql.service

[Service]
Type=simple
User=$(id -un)
WorkingDirectory=${ROOT}
EnvironmentFile=${ROOT}/.env
ExecStart=${VENV}/bin/python scripts/run_worker.py
Restart=always
RestartSec=5
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now mathagent-api mathagent-worker
sleep 6
sudo systemctl --no-pager --lines=0 status mathagent-api mathagent-worker || true

say "7. 自检"
if curl -fsS -m 5 http://127.0.0.1:8000/api/v1/health >/dev/null; then
  ok "健康检查通过：http://127.0.0.1:8000/api/v1/health"
else
  warn "健康检查没过：sudo journalctl -u mathagent-api -n 60 --no-pager"
fi

cat <<'EOF'

下一步：
  1) 浏览器/隧道打开 http://127.0.0.1:8000/       （dev 模式 API 同时发静态页）
  2) 求解器自检：
       .venv/bin/python server/tests/test_assembly_solver.py
       .venv/bin/python scripts/load_assembly.py --slots 50 --candidates 1000
  3) 全量验收：
       bash scripts/verify-on-server.sh
  4) 日志：
       sudo journalctl -u mathagent-api -f
       sudo journalctl -u mathagent-worker -f
EOF
