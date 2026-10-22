#!/usr/bin/env bash
# 修 403 Forbidden：让 nginx（www-data）读得到前端文件。
#
#   bash deploy/fix-permissions.sh          # 检查 + 修 + 验证
#   bash deploy/fix-permissions.sh --check  # 只看问题在哪一级
#
# 为什么会有 403：Ubuntu 21.04 起新建用户的家目录默认是 750（drwxr-x---），
# 只有本人和同组能进。nginx 以 www-data 运行，走到 /home/aiformath 就被挡在门外，
# 于是"文件明明在、nginx 也说配置没错"，浏览器却是 403。
#
# 这里只放行"本来就要给浏览器读"的东西：
#   目录 o+x（能进），静态目录 o+rX（能读）
# 同时把 secrets/ 与 .env 收回 700/600 —— 不要用 chmod -R 777 图省事，
# 那会把密钥目录一起交出去。

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WWWUSER="${WWWUSER:-www-data}"
CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

if [ -t 1 ]; then G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; B=$'\033[1;34m'; N=$'\033[0m'
else G=""; Y=""; R=""; B=""; N=""; fi
pass() { printf '  %s✓%s %s\n' "$G" "$N" "$*"; }
bad()  { printf '  %s✗%s %s\n' "$R" "$N" "$*"; }
fix()  { printf '  %s→%s %s\n' "$Y" "$N" "$*"; }
say()  { printf '\n%s%s%s\n' "$B" "$*" "$N"; }

id "$WWWUSER" >/dev/null 2>&1 || { echo "没有 $WWWUSER 这个用户：nginx 装了吗？"; exit 1; }

say "1. nginx 能不能进到仓库目录"
# 从根到 ROOT 逐级检查"能否进入"（x 位），缺哪级补哪级
DIR="$ROOT"
MISSING=()
while [ "$DIR" != "/" ]; do
  if sudo -u "$WWWUSER" test -x "$DIR"; then
    pass "$WWWUSER 可进入 $DIR"
  else
    bad "$WWWUSER 进不去 $DIR（$(stat -c '%A %U:%G' "$DIR")）"
    MISSING+=("$DIR")
  fi
  DIR="$(dirname "$DIR")"
done
for d in "${MISSING[@]}"; do
  if [ "$CHECK_ONLY" -eq 1 ]; then
    fix "需要：sudo chmod o+x $d"
  else
    sudo chmod o+x "$d"
    fix "已放开进入权限：$d"
  fi
done

say "2. 静态文件能不能读"
for item in index.html js styles assets vendor data config course-packs reports; do
  path="$ROOT/$item"
  [ -e "$path" ] || continue
  if sudo -u "$WWWUSER" test -r "$path"; then
    pass "$item 可读"
  else
    bad "$item 不可读"
    if [ "$CHECK_ONLY" -eq 1 ]; then
      fix "需要：sudo chmod -R o+rX $path"
    else
      sudo chmod -R o+rX "$path"
      fix "已放开读权限：$item"
    fi
  fi
done

say "3. 这些必须继续关着"
for path in "$ROOT/secrets" "$ROOT/.env" "$ROOT/db"; do
  [ -e "$path" ] || continue
  if [ "$CHECK_ONLY" -eq 1 ]; then
    printf '     %s：%s（应为只有本人可读）\n' "$(basename "$path")" "$(stat -c '%A' "$path")"
  else
    case "$path" in
      *.env) chmod 600 "$path" ;;
      *)     chmod 700 "$path" ;;
    esac
    pass "$(basename "$path") 已收回 $(stat -c '%A' "$path")"
  fi
done

say "4. 验证"
if [ "$CHECK_ONLY" -eq 1 ]; then
  echo "  只检查模式，未做任何修改。"
  exit 0
fi
if sudo -u "$WWWUSER" test -r "$ROOT/index.html"; then
  pass "$WWWUSER 能读 index.html"
else
  bad "还是读不到 —— 看 $(namei -l "$ROOT/index.html" | sed 's/^/       /')"
  exit 1
fi
CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1/ || echo 000)"
if [ "$CODE" = "200" ]; then
  pass "http://127.0.0.1/ 返回 200"
  echo
  echo "修好了。浏览器强刷一次（Ctrl + F5）。"
else
  bad "首页仍返回 $CODE"
  echo "  权限已放开但还不是 200，说明另有原因（root 指错、站点没启用）。"
  echo "  跑一次：bash deploy/diagnose-http.sh"
  exit 1
fi
