#!/usr/bin/env bash
# 体检：随时运行，告诉你"现在到第几步、下一步该做什么"。
#
#   bash deploy/doctor.sh
#
# 它只读不写：不装包、不改配置、不动数据库。安全，可以反复跑。
# 每一项按教程的步骤编号排列，看到第一个 ✗ 就从那一步继续。
# 退出码：0 = 全绿；1 = 有阻断项（✗）；2 = 只有提醒（!）

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || { echo "进不去仓库目录 $ROOT"; exit 1; }

FAILS=0
WARNS=0
if [ -t 1 ]; then
  G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; B=$'\033[1;34m'; N=$'\033[0m'
else
  G=""; Y=""; R=""; B=""; N=""
fi

pass() { printf '  %s✓%s %s\n' "$G" "$N" "$*"; }
warn() { printf '  %s!%s %s\n' "$Y" "$N" "$*"; WARNS=$((WARNS + 1)); }
fail() { printf '  %s✗%s %s\n' "$R" "$N" "$*"; FAILS=$((FAILS + 1)); }
say()  { printf '\n%s%s%s\n' "$B" "$*" "$N"; }

say "环境"
if [ -f /etc/os-release ]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  pass "系统：$PRETTY_NAME（$HOSTNAME）"
else
  warn "认不出系统版本（没有 /etc/os-release），按 Debian 系继续"
fi
printf '     内存 %s MB，磁盘可用 %s MB，CPU %s 核\n' \
  "$(awk '/MemTotal/ {printf "%d", $2/1024}' /proc/meminfo 2>/dev/null || echo '?')" \
  "$(df -m "$ROOT" 2>/dev/null | awk 'NR==2 {print $4}')" \
  "$(nproc 2>/dev/null || echo '?')"

say "1. 依赖包"
for cmd in git curl psql nginx python3; do
  if command -v "$cmd" >/dev/null 2>&1; then
    pass "$cmd 已安装"
  else
    fail "$cmd 没装 —— 回到教程第 2 步跑 bash deploy/bootstrap.sh"
  fi
done

say "2. Python 环境"
if [ -x .venv/bin/python ]; then
  PYVER="$(.venv/bin/python -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null)"
  pass "venv 存在（Python $PYVER）"
  if .venv/bin/python -c "import fastapi, uvicorn, psycopg, pgvector" >/dev/null 2>&1; then
    pass "依赖可导入"
  else
    fail "依赖缺失 —— .venv/bin/pip install -r server/requirements.txt"
  fi
  if [ -x .venv/bin/uvicorn ]; then
    pass "uvicorn 可用"
  else
    fail "uvicorn 不在 —— 依赖没装全"
  fi
else
  fail "没有 .venv —— 回到教程第 6 步"
fi

say "3. 代码与数据文件"
[ -f db/build_seed.py ] && pass "代码完整（找到 db/build_seed.py）" || fail "不在仓库根目录，或 clone 不完整"
if [ -f db/seed.sql ]; then
  pass "db/seed.sql 已生成（$(du -h db/seed.sql | cut -f1)）"
else
  fail "db/seed.sql 不存在 —— .venv/bin/python db/build_seed.py"
fi
PGVER="$(ls /usr/lib/postgresql 2>/dev/null | sort -V | tail -1 || true)"
if [ -n "$PGVER" ]; then
  if ls "/usr/lib/postgresql/$PGVER/lib/" 2>/dev/null | grep -q '^vector'; then
    pass "pgvector 已装（PostgreSQL $PGVER）"
  else
    fail "pgvector 没装 —— 教程第 3 步，或重跑 bash deploy/bootstrap.sh"
  fi
else
  fail "PostgreSQL 没装"
fi

say "4. 配置文件 .env"
if [ -f .env ]; then
  MODE="$(stat -c '%a' .env 2>/dev/null || echo '?')"
  [ "$MODE" = "600" ] && pass ".env 权限 600" || warn ".env 权限是 $MODE，建议 chmod 600 .env（里面有密钥）"
  # shellcheck disable=SC1091
  set -a; . ./.env; set +a
  if [ -n "${DATABASE_URL:-}" ]; then
    pass "DATABASE_URL 已填"
  else
    fail ".env 里 DATABASE_URL 是空的 —— 教程第 11 步"
  fi
  case "${APP_ENV:-}" in
    prod) pass "APP_ENV=prod" ;;
    dev)  warn "APP_ENV=dev：仅适合本机调试。上线改成 prod" ;;
    *)    warn "APP_ENV 没设（默认 dev）" ;;
  esac
  case "${AUTH_MODE:-}" in
    password|cas) pass "AUTH_MODE=${AUTH_MODE}" ;;
    dev) warn "AUTH_MODE=dev：任何人带请求头就能冒充管理员" ;;
    *)   warn "AUTH_MODE 没设（默认 dev）" ;;
  esac
  if [ "${CORS_ORIGINS:-}" = "*" ] || [ -z "${CORS_ORIGINS:-}" ]; then
    warn "CORS_ORIGINS 空或为 * —— preflight 会拦"
  else
    pass "CORS_ORIGINS=${CORS_ORIGINS}"
  fi
  if [ -n "${LLM_API_KEY:-}" ]; then
    pass "LLM_API_KEY 已填"
  else
    warn "LLM_API_KEY 空：问答会返回 502，其余功能正常"
  fi
else
  fail "没有 .env —— 回到教程第 11 步"
fi

say "5. 数据库"
DB_OK=0
if [ -n "${DATABASE_URL:-}" ] && command -v psql >/dev/null 2>&1; then
  if psql "$DATABASE_URL" -tAc 'select 1' >/dev/null 2>&1; then
    DB_OK=1
    pass "能连上数据库"
  else
    fail "连不上 —— 口令错、库不存在、或 PostgreSQL 没起：sudo systemctl status postgresql"
  fi
fi
if [ "$DB_OK" -eq 1 ]; then
  VEC="$(psql "$DATABASE_URL" -tAc "select count(*) from pg_extension where extname='vector'" 2>/dev/null | tr -d ' ')"
  [ "$VEC" = "1" ] && pass "vector 扩展已启用" || fail "vector 扩展没建 —— 教程第 4 步"
  APPLIED="$(psql "$DATABASE_URL" -tAc 'select count(*) from schema_migration' 2>/dev/null | tr -d ' ')"
  TOTAL="$(ls db/migrations/*.sql 2>/dev/null | wc -l)"
  if [ "${APPLIED:-0}" -ge "$TOTAL" ] && [ "$TOTAL" -gt 0 ]; then
    pass "迁移已应用 $APPLIED/$TOTAL"
  else
    fail "迁移只应用了 ${APPLIED:-0}/$TOTAL —— .venv/bin/python db/migrate.py --apply"
  fi
  COUNTS="$(psql "$DATABASE_URL" -tAc "select (select count(*) from course) || ' ' || (select count(*) from exercise) || ' ' || (select count(*) from knowledge_node)" 2>/dev/null | xargs)"
  if [ -n "$COUNTS" ]; then
    set -- $COUNTS
    if [ "${1:-0}" -ge 14 ]; then
      pass "课程 $1 门、习题 $2 道、知识点 $3 个"
    elif [ "${1:-0}" -eq 0 ]; then
      fail "库里没有课程 —— 教程第 8 步 import_seed.py"
    else
      warn "课程只有 ${1} 门（期望 14），可能只导了一部分"
    fi
  fi
  CHUNKS="$(psql "$DATABASE_URL" -tAc "select count(*) from textbook_chunk" 2>/dev/null | tr -d ' ')"
  if [ "${CHUNKS:-0}" -gt 1000 ]; then
    pass "问答语料 $CHUNKS 块"
  else
    fail "问答语料只有 ${CHUNKS:-0} 块 —— .venv/bin/python scripts/rebuild_qa_corpus.py"
  fi
  ADMINS="$(psql "$DATABASE_URL" -tAc "select count(*) from app_account where role='admin' and disabled_at is null" 2>/dev/null | tr -d ' ')"
  if [ "${ADMINS:-0}" -ge 1 ]; then
    pass "管理员账号 $ADMINS 个"
  else
    fail "还没有管理员 —— .venv/bin/python scripts/create_admin.py --username admin"
  fi
fi

say "6. 后端服务"
if systemctl is-active --quiet whu-teaching 2>/dev/null; then
  pass "whu-teaching 运行中"
else
  fail "whu-teaching 没在跑 —— sudo systemctl status whu-teaching / journalctl -u whu-teaching -n 50"
fi
if command -v curl >/dev/null 2>&1; then
  if curl -fsS --max-time 5 http://127.0.0.1:8000/api/v1/health >/dev/null 2>&1; then
    pass "后端健康检查通过（127.0.0.1:8000）"
  else
    fail "后端 8000 无响应 —— 看 journalctl -u whu-teaching -n 50"
  fi
  if curl -fsS --max-time 5 http://127.0.0.1/api/v1/health >/dev/null 2>&1; then
    pass "nginx 反代通过"
  else
    fail "http://127.0.0.1/api/ 不通 —— nginx 配置或 8000 端口"
  fi
  if curl -fsS --max-time 5 http://127.0.0.1/ 2>/dev/null | grep -q '<title>'; then
    pass "首页能返回 HTML"
  else
    fail "首页不是 HTML —— APP_ENV=prod 时前端必须由 nginx 托管（教程第 13 步）"
  fi
fi

say "7. 上线前自检"
if [ -x .venv/bin/python ] && [ -f scripts/preflight.py ]; then
  if .venv/bin/python scripts/preflight.py >/tmp/doctor-preflight.log 2>&1; then
    pass "preflight 通过（退出码 0）"
  else
    fail "preflight 有阻断项，详情："
    sed 's/^/     /' /tmp/doctor-preflight.log | tail -20
  fi
else
  warn "跑不了 preflight（venv 或脚本缺失）"
fi

say "结论"
if [ "$FAILS" -gt 0 ]; then
  printf '  %s还有 %d 项没通过%s：按上面第一个 ✗ 的提示做，做完再跑一次本脚本。\n' "$R" "$FAILS" "$N"
  exit 1
elif [ "$WARNS" -gt 0 ]; then
  printf '  %s能跑，但有 %d 条提醒%s（多数是"还没填模型 Key"这类，不阻断上线）。\n' "$Y" "$WARNS" "$N"
  exit 2
else
  printf '  %s全部通过。%s\n' "$G" "$N"
  exit 0
fi
