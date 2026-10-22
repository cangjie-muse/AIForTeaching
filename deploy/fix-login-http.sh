#!/usr/bin/env bash
# 修"登录成功但一翻页就掉线 / 点改密没反应 / 页面没内容"这一类问题。
#
#   bash deploy/fix-login-http.sh                    # 修 .env、重启、逐项验证
#   bash deploy/fix-login-http.sh --check            # 只检查，不改任何东西
#   bash deploy/fix-login-http.sh --login admin      # 修完顺带做一次真实登录验证（会问口令）
#
# 原理：APP_ENV=prod 时会话 cookie 带 Secure，http 下浏览器直接丢弃它，
# 于是登录接口成功、后续每个请求都是 401——页面显示"请先登录"，点"修改密码"也没反应。
# 域名与证书到位前，只能显式把 Secure 关掉（.env 里 COOKIE_SECURE=0）。

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE="${SERVICE:-whu-teaching}"
CHECK_ONLY=0
LOGIN_USER=""

while [ $# -gt 0 ]; do
  case "$1" in
    --check) CHECK_ONLY=1 ;;
    --login) shift; LOGIN_USER="${1:-admin}" ;;
    *) echo "未知参数：$1"; exit 2 ;;
  esac
  shift
done

if [ -t 1 ]; then G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; B=$'\033[1;34m'; N=$'\033[0m'
else G=""; Y=""; R=""; B=""; N=""; fi
pass() { printf '  %s✓%s %s\n' "$G" "$N" "$*"; }
bad()  { printf '  %s✗%s %s\n' "$R" "$N" "$*"; }
warn() { printf '  %s!%s %s\n' "$Y" "$N" "$*"; }
say()  { printf '\n%s%s%s\n' "$B" "$*" "$N"; }
FAILS=0
fail() { bad "$*"; FAILS=$((FAILS + 1)); }

cd "$ROOT" || { echo "进不去 $ROOT"; exit 1; }
[ -f .env ] || { echo "没有 .env：先完成部署（docs/从零开始部署（小白版）.md 第 12 步）"; exit 1; }

say "1. .env 里的 COOKIE_SECURE"
CURRENT="$(grep -E '^[[:space:]]*COOKIE_SECURE=' .env | tail -1 | cut -d= -f2- | tr -d '[:space:]' || true)"
case "$(printf '%s' "$CURRENT" | tr '[:upper:]' '[:lower:]')" in
  0|false|no|off)
    pass "已经是 COOKIE_SECURE=$CURRENT"
    ;;
  "")
    if [ "$CHECK_ONLY" -eq 1 ]; then
      fail "没有 COOKIE_SECURE：prod + http 下登录会掉线"
      printf '     修：echo "COOKIE_SECURE=0" >> .env && sudo systemctl restart %s\n' "$SERVICE"
    else
      cp .env ".env.bak.$(date +%s)"
      printf '\n# 域名与证书到位前临时关闭；上 https 后删掉这一行\nCOOKIE_SECURE=0\n' >> .env
      pass "已追加 COOKIE_SECURE=0（原文件已备份为 .env.bak.*）"
    fi
    ;;
  *)
    if [ "$CHECK_ONLY" -eq 1 ]; then
      fail "COOKIE_SECURE=$CURRENT：http 下浏览器不会回传 cookie，登录会掉线"
    else
      cp .env ".env.bak.$(date +%s)"
      sed -i 's/^[[:space:]]*COOKIE_SECURE=.*/COOKIE_SECURE=0/' .env
      pass "已把 COOKIE_SECURE 改成 0（原文件已备份）"
    fi
    ;;
esac

say "2. 重启服务"
if [ "$CHECK_ONLY" -eq 1 ]; then
  printf '     （只检查模式，不重启）systemctl is-active %s → %s\n' "$SERVICE" "$(systemctl is-active "$SERVICE" 2>/dev/null || echo unknown)"
else
  if sudo systemctl restart "$SERVICE" 2>/dev/null; then
    sleep 3
    if systemctl is-active --quiet "$SERVICE"; then
      pass "$SERVICE 已重启并运行"
    else
      fail "$SERVICE 没起来 —— sudo journalctl -u $SERVICE -n 40 --no-pager"
    fi
  else
    fail "重启失败 —— sudo systemctl status $SERVICE"
  fi
fi

say "3. 后端直连（绕过 nginx）"
API="http://127.0.0.1:8000"
CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$API/api/v1/health" || echo 000)"
if [ "$CODE" = "200" ]; then
  pass "uvicorn 8000 健康检查 200"
  curl -s --max-time 5 "$API/api/v1/health" | head -c 160; printf '\n'
else
  fail "uvicorn 8000 返回 $CODE —— sudo journalctl -u $SERVICE -n 40 --no-pager"
fi

say "4. 经过 nginx（浏览器走的就是这条路）"
PROXY="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1/api/v1/health || echo 000)"
if [ "$PROXY" = "200" ]; then
  pass "http://127.0.0.1/api/v1/health 200（nginx 反代正常）"
else
  fail "经 nginx 访问 /api/v1/health 返回 $PROXY —— 查 /etc/nginx/sites-available/whu-teaching 里的 location /api/"
fi

DATA="$(curl -s -o /dev/null -w '%{http_code} %{size_download}' --max-time 10 http://127.0.0.1/data/courses.js || echo '000 0')"
DATA_CODE="${DATA%% *}"
DATA_SIZE="${DATA##* }"
if [ "$DATA_CODE" = "200" ] && [ "${DATA_SIZE:-0}" -gt 1000000 ]; then
  pass "data/courses.js 可取（$((DATA_SIZE / 1024)) KB）"
else
  fail "data/courses.js 取不到（$DATA）—— 页面会没有课程"
  printf '     查 nginx 的 root：sudo nginx -T | grep -E "root|location /" | head -8\n'
  printf '     root 要指向仓库根目录（有 index.html 的那一层）\n'
fi

CAT="$(curl -s --max-time 10 http://127.0.0.1/api/v1/catalog || echo '')"
if printf '%s' "$CAT" | grep -q '"items"'; then
  N="$(printf '%s' "$CAT" | tr ',' '\n' | grep -c '"id"' || true)"
  pass "GET /api/v1/catalog 有响应（约 $N 条）"
else
  fail "GET /api/v1/catalog 无有效响应"
fi

say "5. 登录接口自己发的 cookie 长什么样"
HEAD="$(mktemp)"
curl -s -o /dev/null -D "$HEAD" --max-time 10 -H 'Content-Type: application/json' \
  -d '{"username":"__probe__","password":"__probe__"}' "$API/api/v1/auth/login" || true
if grep -qi 'set-cookie' "$HEAD" 2>/dev/null; then
  if grep -qi 'set-cookie:.*secure' "$HEAD"; then
    fail "Set-Cookie 里带 Secure —— http 下浏览器会丢弃它"
  else
    pass "Set-Cookie 不带 Secure"
  fi
else
  warn "登录接口没回 Set-Cookie（口令错时不发，属正常；跑 --login 做真实验证）"
fi
rm -f "$HEAD"

if [ -n "$LOGIN_USER" ]; then
  say "6. 真实登录验证（$LOGIN_USER）"
  read -rsp "  口令：" PW; echo
  JAR="$(mktemp)"
  BODY="$(mktemp)"
  JSON="$(python3 -c 'import json,sys; print(json.dumps({"username": sys.argv[1], "password": sys.argv[2]}))' "$LOGIN_USER" "$PW" 2>/dev/null)"
  LOGIN_CODE="$(curl -s -o "$BODY" -D /tmp/login-head.txt -w '%{http_code}' --max-time 10 -c "$JAR" \
    -H 'Content-Type: application/json' -d "$JSON" http://127.0.0.1/api/v1/auth/login || echo 000)"
  if [ "$LOGIN_CODE" = "200" ]; then
    pass "登录接口 200"
  else
    fail "登录接口 $LOGIN_CODE：$(head -c 200 "$BODY")"
  fi
  if grep -qi 'set-cookie:.*secure' /tmp/login-head.txt 2>/dev/null; then
    fail "Set-Cookie 仍带 Secure —— 浏览器会丢弃它，改密和审核都会 401"
    printf '     确认 .env 里是 COOKIE_SECURE=0，并且真的重启过服务\n'
  else
    pass "Set-Cookie 不带 Secure（http 下可用）"
  fi
  if grep -q 'whu_session' "$JAR" 2>/dev/null; then
    pass "已拿到会话 cookie"
  else
    fail "没有拿到会话 cookie"
  fi
  WHO="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 -b "$JAR" http://127.0.0.1/api/v1/admin/whoami || echo 000)"
  case "$WHO" in
    200) pass "带 cookie 访问 /admin/whoami 返回 200（登录态真的生效了）" ;;
    401) fail "带 cookie 仍 401 —— 检查 .env 是否被 systemd 读到：systemctl show $SERVICE -p EnvironmentFiles" ;;
    403) warn "401 已解决，但这个账号不是管理员（whoami 要 admin）" ;;
    *)   fail "/admin/whoami 返回 $WHO" ;;
  esac
  rm -f "$JAR" "$BODY"
fi

say "结论"
if [ "$FAILS" -gt 0 ]; then
  printf '  %s还有 %d 项没通过%s。先修第一个 ✗，再跑一次。\n' "$R" "$FAILS" "$N"
  exit 1
fi
cat <<'OK'
  这一侧全通了。

  请在浏览器里做三件事（顺序别换）：
    1. Ctrl + F5 强制刷新，把旧页面丢掉；
    2. 退出登录再重新登录（旧 cookie 可能已经是坏的）；
    3. 再点"修改密码"，应该能改。

  还是不行的话，按 F12 → Network，点一次"修改密码"，
  看那条请求的状态码和响应体，把它贴出来。
OK
