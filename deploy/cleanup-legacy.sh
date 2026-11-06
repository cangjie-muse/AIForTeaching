#!/usr/bin/env bash
# 清理服务器上的旧系统（旧部署目录 / 旧 systemd 单元 / 旧数据库 / 旧 nginx 站点）
#
# 为什么要有它：这台机器上曾有一套旧部署（`/home/aiformath/AIForTeaching`、
# `whu-teaching.service`、`whu-worker.service`），它占着 8000 端口、抢同一条
# pipeline_job 队列，新系统会因此**永远起不来或求解永远不落库**。这些把"看起来
# 起来了但功能不对"的问题，靠日志要查很久。
#
# 默认**只打印计划**，不做任何删除。确认无误后加 --apply 才真的动手。
#
#   bash deploy/cleanup-legacy.sh                 # 只列出将删除什么
#   bash deploy/cleanup-legacy.sh --apply         # 删单元 + 旧目录 + 旧库
#   bash deploy/cleanup-legacy.sh --apply --keep-db   # 保留旧数据库（先备份数据时用）
#
# 幂等：已经不存在的东西直接跳过。
set -uo pipefail

APPLY=0
KEEP_DB=0
for arg in "$@"; do
  case "$arg" in
    --apply) APPLY=1 ;;
    --keep-db) KEEP_DB=1 ;;
    *) echo "未知参数：$arg"; exit 2 ;;
  esac
done

say()  { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
info() { printf '  %s\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
skip() { printf '  \033[33m-\033[0m %s\n' "$*"; }

OLD_UNITS=(whu-teaching.service whu-worker.service)
OLD_DIRS=(/home/aiformath/AIForTeaching)
OLD_DBS=(whu_teaching)

say "1. 旧 systemd 单元"
found_units=()
for unit in "${OLD_UNITS[@]}"; do
  if systemctl list-unit-files | grep -q "^${unit}"; then
    info "$unit：$(systemctl is-active "$unit" 2>/dev/null || true) / $(systemctl is-enabled "$unit" 2>/dev/null || true)"
    found_units+=("$unit")
  else
    skip "$unit 不存在"
  fi
done

say "2. 旧部署目录"
found_dirs=()
for dir in "${OLD_DIRS[@]}"; do
  if [ -d "$dir" ]; then
    info "$dir  ($(du -sh "$dir" 2>/dev/null | cut -f1))"
    found_dirs+=("$dir")
  else
    skip "$dir 不存在"
  fi
done

say "3. 旧数据库"
found_dbs=()
for name in "${OLD_DBS[@]}"; do
  if sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='${name}'" | grep -q 1; then
    info "$name（$(sudo -u postgres psql -d "$name" -tAc 'SELECT count(*) FROM information_schema.tables WHERE table_schema = $'"'"'public'"'"'' 2>/dev/null || echo '?') 张表）"
    found_dbs+=("$name")
  else
    skip "$name 不存在"
  fi
done

say "4. 旧 nginx 站点（只报告，不动手）"
for conf in /etc/nginx/sites-enabled/*; do
  [ -e "$conf" ] || continue
  if grep -qE 'aiformath|AIForTeaching' "$conf" 2>/dev/null; then
    info "$conf 仍指向旧目录：$(readlink -f "$conf")"
    echo "    改法：sudo rm $conf && sudo systemctl reload nginx（或把 root 改成 /home/mathagent/mathagent）"
  fi
done

if [ "$APPLY" -eq 0 ]; then
  say "只列出计划（未删除任何东西）"
  echo "  确认上面无误后执行：bash deploy/cleanup-legacy.sh --apply"
  exit 0
fi

say "执行清理"
for unit in "${found_units[@]}"; do
  sudo systemctl disable --now "$unit" 2>/dev/null
  sudo rm -f "/etc/systemd/system/${unit}"
  ok "已停用并删除单元：$unit"
done
if [ "${#found_units[@]}" -gt 0 ]; then
  sudo systemctl daemon-reload
  ok "systemd 已重载"
fi

for dir in "${found_dirs[@]}"; do
  # 删之前先确认它不属于当前部署（防止把新系统删掉）
  current="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  if [ "$(readlink -f "$dir")" = "$(readlink -f "$current")" ]; then
    skip "$dir 就是当前部署，跳过"
    continue
  fi
  sudo rm -rf "$dir"
  ok "已删除目录：$dir"
done

if [ "$KEEP_DB" -eq 1 ]; then
  skip "按要求保留数据库：${found_dbs[*]:-（无）}"
else
  for name in "${found_dbs[@]}"; do
    sudo -u postgres psql -c "DROP DATABASE IF EXISTS \"${name}\"" >/dev/null
    ok "已删除数据库：$name"
  done
fi

say "完成"
echo "  复查：sudo ss -ltnp 'sport = :8000'   应只剩 mathagent 的 uvicorn"
echo "  队列：psql \"\$(sed -n 's/^DATABASE_URL=//p' .env)\" -c \\"
echo "          \"select kind,status,count(*) from pipeline_job group by 1,2;\""
