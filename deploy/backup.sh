#!/usr/bin/env bash
# 数据库备份 + 保留 30 天 + 校验备份可读。
#
#   bash deploy/backup.sh                 # 备份到 /home/aiformath/backup
#   bash deploy/backup.sh --dir /mnt/bak  # 指定目录（建议放另一块盘）
#   bash deploy/backup.sh --verify-only   # 只验证最新一份能否恢复（不连生产库）
#
# 装进 cron（每天 3:20）：
#   20 3 * * * cd /home/aiformath/AIForTeaching && bash deploy/backup.sh >> /home/aiformath/backup/backup.log 2>&1
#
# 只备份不验证 = 没备份。--verify-only 会用 pg_restore -l 读目录，
# 能读出对象清单才算文件没坏；真正的恢复演练见 docs/回滚与恢复.md。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_DIR="${BACKUP_DIR:-$HOME/backup}"
KEEP_DAYS="${KEEP_DAYS:-30}"
VERIFY_ONLY=0
# 用 while + shift 解析：for 里 shift 会把迭代器搞乱（这个坑第一次写就踩了）
while [ $# -gt 0 ]; do
  case "$1" in
    --verify-only) VERIFY_ONLY=1 ;;
    --dir) shift; [ $# -gt 0 ] || { echo "--dir 后面要跟路径"; exit 2; }; BACKUP_DIR="$1" ;;
    *) echo "未知参数：$1"; exit 2 ;;
  esac
  shift
done

mkdir -p "$BACKUP_DIR"
STAMP="$(date +%F_%H%M)"
FILE="$BACKUP_DIR/whu_teaching_$STAMP.dump"

cd "$ROOT"
if [ -f .env ]; then
  # shellcheck disable=SC1091
  set -a; . ./.env; set +a
fi

if [ "$VERIFY_ONLY" -eq 1 ]; then
  LATEST="$(ls -1t "$BACKUP_DIR"/whu_teaching_*.dump 2>/dev/null | head -1 || true)"
  [ -n "$LATEST" ] || { echo "没有备份文件"; exit 1; }
  echo "校验：$LATEST"
  pg_restore -l "$LATEST" | head -5
  COUNT="$(pg_restore -l "$LATEST" | wc -l)"
  echo "对象条目 $COUNT 条"
  [ "$COUNT" -gt 100 ] || { echo "✗ 目录太短，文件可能损坏"; exit 1; }
  echo "✓ 备份文件可解析"
  exit 0
fi

[ -n "${DATABASE_URL:-}" ] || { echo "没有 DATABASE_URL：先准备 .env 或 export"; exit 1; }

echo "备份到：$FILE"
# -Fc：自定义格式，pg_restore 可选择性恢复；不加 --no-owner 会在换库时因角色不存在而失败
pg_dump -Fc --no-owner --no-privileges "$DATABASE_URL" > "$FILE.part"
mv "$FILE.part" "$FILE"
echo "大小：$(du -h "$FILE" | cut -f1)"

# 立刻校验一次：写坏的 dump 只有读的时候才会发现
COUNT="$(pg_restore -l "$FILE" | wc -l)"
[ "$COUNT" -gt 100 ] || { echo "✗ $FILE 目录异常，保留文件供排查"; exit 1; }
echo "✓ 目录 $COUNT 条"

# 清理过期：只删本脚本命名模式的文件，避免误删别的备份
find "$BACKUP_DIR" -maxdepth 1 -name 'whu_teaching_*.dump' -mtime "+$KEEP_DAYS" -print -delete | sed 's/^/  删除过期：/'
echo "保留最近 $KEEP_DAYS 天，当前 $(ls -1 "$BACKUP_DIR"/whu_teaching_*.dump 2>/dev/null | wc -l) 份"
