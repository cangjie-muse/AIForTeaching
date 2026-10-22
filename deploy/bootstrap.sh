#!/usr/bin/env bash
# 服务器基础环境一键安装（Ubuntu 22.04 / 24.04 / 26.04）。
#
# 它只做"装东西"，不做"建库"和"写密钥"：
# 数据库口令、模型 API Key 必须由人输入，脚本代填等于把口令写进 shell 历史。
#
#   bash deploy/bootstrap.sh            # 装系统包 + pgvector + venv + Python 依赖 + 生成 seed.sql
#   bash deploy/bootstrap.sh --check    # 只检查，不安装（看还缺什么）
#
# 幂等：重复执行不会重复装；已存在的 venv 只补依赖。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

say()  { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
die()  { printf '\n\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

say "0. 检查系统"
[ -f /etc/os-release ] || die "这不是 Ubuntu/Debian：找不到 /etc/os-release"
# shellcheck disable=SC1091
. /etc/os-release
ok "系统：$PRETTY_NAME"
[ "$(id -u)" -eq 0 ] && die "不要用 root 跑这个脚本：它会 chown 仓库目录。用普通账号 + sudo。"
command -v sudo >/dev/null || die "没有 sudo，无法装包"

PY="$(command -v python3 || true)"
[ -n "$PY" ] || die "没有 python3。先 sudo apt install python3"
PYVER="$($PY -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
ok "python3：$PYVER（$PY）"
case "$PYVER" in
  3.1[0-3]) ok "版本可用" ;;
  3.14) warn "3.14 太新：部分依赖可能还没有预编译包。装依赖失败时改用 python3.13" ;;
  *) warn "版本 $PYVER 未在本项目验证过（开发与 CI 用 3.12）" ;;
esac

DFREE="$(df -m "$ROOT" | awk 'NR==2 {print $4}')"
[ "$DFREE" -gt 3000 ] && ok "磁盘余量：${DFREE} MB" || warn "磁盘只剩 ${DFREE} MB，教材 PDF 会吃空间"

if [ "$CHECK_ONLY" -eq 1 ]; then
  say "只检查：下面这些命令若报 not found，就是还需要安装"
  for cmd in git nginx psql make gcc; do
    if command -v "$cmd" >/dev/null; then ok "$cmd 已装"; else warn "$cmd 缺失"; fi
  done
  [ -d "$ROOT/.venv" ] && ok "venv 已存在" || warn "venv 缺失"
  PGVER="$(ls /usr/lib/postgresql 2>/dev/null | sort -V | tail -1 || true)"
  [ -n "$PGVER" ] && ok "PostgreSQL $PGVER" || warn "PostgreSQL 未装"
  exit 0
fi

say "1. 系统包（apt）"
export DEBIAN_FRONTEND=noninteractive
sudo apt-get update -qq
# 说明：python3-venv 与 python3-pip 分开装——只有 python3-venv 时 ensurepip 可能被裁掉
sudo apt-get install -y -qq \
  python3-venv python3-pip python3-dev \
  git curl ca-certificates unzip \
  build-essential libpq-dev pkg-config \
  nginx postgresql postgresql-contrib
ok "系统包就绪"

say "2. pgvector 扩展"
PGVER="$(ls /usr/lib/postgresql | sort -V | tail -1)"
ok "PostgreSQL $PGVER"
if sudo apt-get install -y -qq "postgresql-${PGVER}-pgvector" 2>/dev/null; then
  ok "pgvector 由系统包安装"
else
  warn "系统没有 postgresql-${PGVER}-pgvector，改从源码编译（需要联网到 GitHub）"
  sudo apt-get install -y -qq "postgresql-server-dev-${PGVER}" git make gcc
  TMP="$(mktemp -d)"
  # 0.8.0 是 2026 年仍在维护的稳定分支；换版本只改这一行
  git clone --branch v0.8.0 --depth 1 https://github.com/pgvector/pgvector.git "$TMP/pgvector" \
    || die "拉取 pgvector 源码失败：服务器访问不了 GitHub。把仓库打成 zip 上传后手动编译，或让运维放通 github.com"
  ( cd "$TMP/pgvector" && make -s && sudo make -s install )
  rm -rf "$TMP"
  ok "pgvector 源码编译完成"
fi

say "3. Python 虚拟环境"
cd "$ROOT"
if [ ! -d .venv ]; then
  python3 -m venv .venv
  ok "已创建 .venv"
else
  ok ".venv 已存在，跳过创建"
fi
# 国内网络直接从 pypi.org 拉包经常几 KB/s，先试清华镜像，失败再回退官方源
if ! .venv/bin/pip install -q -U pip -i https://pypi.tuna.tsinghua.edu.cn/simple 2>/dev/null; then
  .venv/bin/pip install -q -U pip
fi
if .venv/bin/pip install -q -r server/requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple 2>/dev/null; then
  ok "依赖已装（清华镜像）"
else
  warn "镜像装失败，回退官方源重试"
  .venv/bin/pip install -q -r server/requirements.txt || die "pip install 失败，把报错发出来"
  ok "依赖已装（官方源）"
fi
.venv/bin/python -c "import fastapi, uvicorn, psycopg, pgvector; print('  依赖自检通过')" \
  || die "依赖装上了但导入失败，通常是 Python 版本与 wheel 不匹配"

say "4. 生成 db/seed.sql"
if [ -f db/seed.sql ]; then
  ok "db/seed.sql 已存在（$(du -h db/seed.sql | cut -f1)），跳过"
else
  .venv/bin/python db/build_seed.py
  ok "已生成 db/seed.sql（$(du -h db/seed.sql | cut -f1)）"
fi

say "完成。接下来手动做三件事（脚本不代劳，因为要输入口令）："
cat <<'NEXT'

  1) 建库与扩展
     sudo -u postgres psql -c "CREATE ROLE whu LOGIN PASSWORD '你的口令';"
     sudo -u postgres psql -c "CREATE DATABASE whu_teaching OWNER whu ENCODING 'UTF8' TEMPLATE template0;"
     sudo -u postgres psql -d whu_teaching -c "CREATE EXTENSION IF NOT EXISTS vector; CREATE EXTENSION IF NOT EXISTS pgcrypto;"

  2) 建表 + 导入数据 + 建管理员 + 生成问答语料
     export DATABASE_URL='postgresql://whu:你的口令@127.0.0.1:5432/whu_teaching'
     .venv/bin/python db/migrate.py --apply
     .venv/bin/python scripts/import_seed.py db/seed.sql
     .venv/bin/python scripts/create_admin.py --username admin
     .venv/bin/python scripts/rebuild_qa_corpus.py

  3) 配置并启动
     cp deploy/env.example .env && chmod 600 .env && nano .env
     sudo cp deploy/whu-teaching.service /etc/systemd/system/
     sudo systemctl daemon-reload && sudo systemctl enable --now whu-teaching
     sudo cp deploy/nginx.conf.example /etc/nginx/sites-available/whu-teaching
     sudo ln -sf /etc/nginx/sites-available/whu-teaching /etc/nginx/sites-enabled/
     sudo nginx -t && sudo systemctl reload nginx

  详细步骤见 docs/服务器部署教程.md
NEXT
