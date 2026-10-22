#!/usr/bin/env bash
# 检查"页面能打开但没有内容"是不是静态数据的问题。
#
#   bash deploy/check-static-data.sh          # 本地文件 + 经 nginx 取一遍
#   bash deploy/check-static-data.sh --local  # 只查本地文件
#
# 背景：课程选择页的数据来自 data/courses.js（window.COURSE_DATA），不走数据库。
# 这个文件缺失、被截断、或 nginx 用 index.html 兜底返回 HTML，浏览器都会"静默失败"：
# 页面照常渲染，只是课程列表空着，顶上还把 36 门规划课当成"待接入"列出来。

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCAL_ONLY=0
case "${1:-}" in
  --local) LOCAL_ONLY=1 ;;
  "") ;;
  *) echo "未知参数：$1"; exit 2 ;;
esac

if [ -t 1 ]; then G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; B=$'\033[1;34m'; N=$'\033[0m'
else G=""; Y=""; R=""; B=""; N=""; fi
pass() { printf '  %s✓%s %s\n' "$G" "$N" "$*"; }
bad()  { printf '  %s✗%s %s\n' "$R" "$N" "$*"; }
warn() { printf '  %s!%s %s\n' "$Y" "$N" "$*"; }
say()  { printf '\n%s%s%s\n' "$B" "$*" "$N"; }
FAILS=0
fail() { bad "$*"; FAILS=$((FAILS + 1)); }

cd "$ROOT" || { echo "进不去 $ROOT"; exit 1; }
echo "仓库：$ROOT"

say "1. 关键文件在不在、大小对不对"
check_size() {
  local file="$1" min="$2" label="$3"
  if [ ! -f "$file" ]; then
    fail "$label 不存在：$file"
    return 1
  fi
  local size
  size="$(wc -c < "$file" | tr -d ' ')"
  if [ "$size" -ge "$min" ]; then
    pass "$label $(numfmt --to=iec --suffix=B "$size" 2>/dev/null || echo "$size 字节")"
  else
    fail "$label 只有 $size 字节（应 ≥ $min）——文件可能没传完"
    return 1
  fi
}
check_size index.html 500 "index.html"
check_size data/courses.js 1000000 "data/courses.js"
check_size data/courseware.js 200 "data/courseware.js"

say "2. 数据能不能解析（这是关键：能解析才有课程）"
if command -v python3 >/dev/null 2>&1; then
  # 控制台编码：Windows 的 GBK 控制台打不出 ✓，显式指定 UTF-8
  PYTHONIOENCODING=utf-8 python3 - data/courses.js <<'PY'
import json, sys
path = sys.argv[1]
try:
    raw = open(path, encoding='utf-8').read()
except FileNotFoundError:
    print("  ✗ 文件不存在"); sys.exit(1)
head = "window.COURSE_DATA"
if not raw.lstrip().startswith(head):
    print(f"  ✗ 开头不是 {head}，这个文件不是课程数据"); sys.exit(1)
body = raw.lstrip()[len(head):].lstrip()
if body.startswith("="):
    body = body[1:].lstrip()
body = body.rstrip()
if body.endswith(";"):
    body = body[:-1]
try:
    data = json.loads(body)
except Exception as error:
    print(f"  ✗ JSON 解析失败：{error}")
    print("     多半是文件被截断（SFTP 传到一半断了），重新拉一次即可")
    sys.exit(1)
ready = [c for c in data if c.get("status") == "ready"]
print(f"  ✓ 解析成功：{len(data)} 门课，其中 {len(ready)} 门已接入（status=ready）")
if not ready:
    print("  ✗ 没有一门是 ready —— 选课页会显示 0 门已接入")
    sys.exit(2)
PY
  case $? in
    0) : ;;
    2) FAILS=$((FAILS + 1)) ;;
    *) FAILS=$((FAILS + 1)) ;;
  esac
else
  warn "没有 python3，跳过解析检查"
fi

say "3. index.html 引用的本地脚本，逐个看能不能取到"
if [ "$LOCAL_ONLY" -eq 1 ]; then
  printf '     （--local 模式，跳过 HTTP）\n'
else
  MISSING=0
  TOTAL=0
  # 从 index.html 里抽出所有 src="data/... / js/... / vendor/..."
  while IFS= read -r path; do
    TOTAL=$((TOTAL + 1))
    CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 "http://127.0.0.1/$path" || echo 000)"
    if [ "$CODE" != "200" ]; then
      bad "$path → $CODE"
      MISSING=$((MISSING + 1))
    fi
  done <<EOF
$(grep -oE 'src="(data|js|vendor)/[^"?]+' index.html | sed 's/^src="//' | sort -u)
EOF
  if [ "$MISSING" -eq 0 ]; then
    pass "共 $TOTAL 个脚本，全部 200"
  else
    fail "$MISSING / $TOTAL 个脚本取不到 —— 页面会缺功能"
  fi

  say "4. 课程数据在浏览器眼里的样子"
  TMP="$(mktemp)"
  CODE="$(curl -s -o "$TMP" -w '%{http_code}' --max-time 15 http://127.0.0.1/data/courses.js || echo 000)"
  TYPE="$(curl -s -o /dev/null -w '%{content_type}' --max-time 15 http://127.0.0.1/data/courses.js || echo '?')"
  printf '     HTTP %s，Content-Type: %s\n' "$CODE" "$TYPE"
  if [ "$CODE" = "200" ]; then
    HEAD3="$(head -c 40 "$TMP" | tr -d '\n')"
    case "$HEAD3" in
      "window.COURSE_DATA"*) pass "开头是 window.COURSE_DATA" ;;
      "<"*) fail "返回的是 HTML 而不是 JS —— nginx 把缺失文件兜底成了 index.html，浏览器会报 Unexpected token '<'" ;;
      *) fail "开头不认识：$HEAD3" ;;
    esac
    case "$TYPE" in
      *javascript*|*ecmascript*) pass "MIME 类型正常" ;;
      *) warn "MIME 是 $TYPE —— 浏览器可能拒绝执行 .js（检查 nginx.conf 里有没有 include mime.types）" ;;
    esac
  else
    fail "取不到 data/courses.js（$CODE）"
  fi
  rm -f "$TMP"
fi

say "结论"
if [ "$FAILS" -gt 0 ]; then
  printf '  %s%d 项没通过%s\n' "$R" "$FAILS" "$N"
  cat <<'TIP'

  如果是 data/courses.js 缺了/没传完，最省事的修法（在服务器上）：

      cd /home/aiformath/AIForTeaching
      git status --short          # 看文件是不是干净的
      git checkout -- data/       # 用仓库里的版本覆盖回来
      ls -l data/courses.js       # 应为 8 MB 左右

  如果这里一切正常但浏览器还是空：按 F12 → Console，输入
      window.COURSE_DATA && window.COURSE_DATA.length
  得到 undefined 就是数据文件没加载成功，看 Network 里 data/courses.js 的状态码。
TIP
  exit 1
fi
printf '  %s静态数据这一侧没问题%s\n' "$G" "$N"
