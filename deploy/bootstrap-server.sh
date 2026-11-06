#!/usr/bin/env bash
# 服务器一键部署（Docker 版）：检查环境 → 建 .env → 起库 → 迁移 → 起 API 与 worker → 自检
#
# 为什么是 Docker：这台服务器是 Ubuntu 26.04，自带 Python 已是 3.13/3.14，而
# OR-Tools 9.11.4210 只提供到 cp312 的 wheel。用 python:3.12-slim 镜像可以直接
# pip 装 ortools，不用在宿主机上折腾解释器版本。
#
# 幂等：重复执行只会重建/重启容器，不会动数据库数据（数据在命名卷里）。
#
# 用法：
#   cd /home/mathagent/mathagent
#   bash deploy/bootstrap-server.sh
#
# 可选环境变量：POSTGRES_PASSWORD（不给就自动生成并写进 .env）
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
COMPOSE=(docker compose -f deploy/compose.server.yml)

say()  { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
die()  { printf '\n\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

say "0. 检查 Docker"
command -v docker >/dev/null || die "没有 docker。先 sudo apt install -y docker.io docker-compose-v2"
docker info >/dev/null 2>&1 || die "docker 守护进程不可用：sudo systemctl enable --now docker，或把当前用户加进 docker 组后重新登录"
"${COMPOSE[@]}" version >/dev/null 2>&1 || die "没有 docker compose 插件（v2）"
ok "$(docker --version)"
ok "$("${COMPOSE[@]}" version --short 2>/dev/null || echo compose-v2)"

say "1. 生成 .env（已存在就保留，只补缺失项）"
if [ ! -f .env ]; then
  PASS="${POSTGRES_PASSWORD:-$(head -c 24 /dev/urandom | base64 | tr -d '/+=' | head -c 24)}"
  cat > .env <<EOF
# 由 deploy/bootstrap-server.sh 生成。至少改 AUTH_MODE/APP_ENV 再对外。
POSTGRES_USER=mathagent
POSTGRES_PASSWORD=${PASS}
POSTGRES_DB=mathagent
APP_ENV=dev
AUTH_MODE=dev
AUTH_DEV_ROLES=admin
CORS_ORIGINS=http://127.0.0.1:8000
SECRETS_DIR=./secrets
EOF
  chmod 600 .env
  ok "已生成 .env（数据库口令随机会写在其中）"
else
  for key in POSTGRES_USER POSTGRES_PASSWORD POSTGRES_DB APP_ENV AUTH_MODE AUTH_DEV_ROLES CORS_ORIGINS SECRETS_DIR; do
    grep -q "^${key}=" .env || warn ".env 缺少 ${key}，可能有服务起不来"
  done
  ok ".env 已存在，保留不动"
fi

mkdir -p secrets

say "2. 构建运行时镜像（python3.12 + 全部依赖 + OR-Tools）"
"${COMPOSE[@]}" build api
ok "镜像就绪"

say "3. 启动数据库并等它就绪"
"${COMPOSE[@]}" up -d db
for i in $(seq 1 60); do
  if docker exec mathagent-db pg_isready -U "$(grep -oP '(?<=^POSTGRES_USER=).*' .env)" >/dev/null 2>&1; then
    ok "数据库已就绪"; break
  fi
  sleep 2
  [ "$i" -eq 60 ] && die "数据库 120 秒内没就绪：docker logs mathagent-db"
done

say "4. 应用迁移（期望 35 个）"
"${COMPOSE[@]}" run --rm --no-deps api python db/migrate.py --apply
"${COMPOSE[@]}" run --rm --no-deps api python db/migrate.py --status | tail -5

say "5. 启动 API 与 worker"
"${COMPOSE[@]}" up -d api worker
sleep 8
"${COMPOSE[@]}" ps

say "6. 自检"
if curl -fsS http://127.0.0.1:8000/api/v1/health >/dev/null; then
  ok "健康检查通过：http://127.0.0.1:8000/api/v1/health"
else
  warn "健康检查没过，先看日志：docker compose -f deploy/compose.server.yml logs --tail=80 api"
fi
printf '  %-46s' "容器内 ortools 版本"
"${COMPOSE[@]}" run --rm --no-deps api python -c \
  "from importlib.metadata import version; print(version('ortools'))"

cat <<'EOF'

下一步：
  1) 浏览器打开  http://<服务器IP>:8000/        （dev 模式下 API 同时发静态页）
  2) 跑全量验收  bash scripts/verify-on-server.sh
     其中两条是本地永远验不了的：
       docker compose -f deploy/compose.server.yml run --rm --no-deps api \
         python server/tests/test_assembly_solver.py
       docker compose -f deploy/compose.server.yml run --rm --no-deps api \
         python scripts/load_assembly.py --slots 50 --candidates 1000
  3) 常用命令：
       docker compose -f deploy/compose.server.yml logs -f api worker
       docker compose -f deploy/compose.server.yml restart api worker
       docker compose -f deploy/compose.server.yml down          # 只停容器，数据保留
EOF
