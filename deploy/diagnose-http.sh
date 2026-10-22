#!/usr/bin/env bash
# 浏览器打不开时跑这个：从下往上把每一层都测一遍，最后给结论。
#
#   bash deploy/diagnose-http.sh
#
# 顺序是"浏览器 → 防火墙 → nginx → 静态文件 → 后端"。从下往上失败的第一层，
# 就是原因；上面几层不用再猜。
#
# 只读，不改任何配置。需要 sudo 的地方会提示输密码（读端口、读防火墙状态）。

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1

if [ -t 1 ]; then G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; B=$'\033[1;34m'; N=$'\033[0m'
else G=""; Y=""; R=""; B=""; N=""; fi
pass() { printf '  %s✓%s %s\n' "$G" "$N" "$*"; }
bad()  { printf '  %s✗%s %s\n' "$R" "$N" "$*"; }
warn() { printf '  %s!%s %s\n' "$Y" "$N" "$*"; }
say()  { printf '\n%s%s%s\n' "$B" "$*" "$N"; }
code() { curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$1" 2>/dev/null || echo "000"; }

DIAG=""
note() { DIAG="${DIAG}$1"$'\n'; }

LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
[ -n "$LAN_IP" ] || LAN_IP="127.0.0.1"

say "0. 这台机器的地址"
printf '     内网/公网 IP（本机看到的）：%s\n' "$LAN_IP"
printf '     主机名：%s\n' "$(hostname 2>/dev/null)"
if command -v ip >/dev/null 2>&1; then
  printf '     默认网关：%s\n' "$(ip route 2>/dev/null | awk '/^default/ {print $3; exit}')"
fi

say "1. nginx 装了吗、在跑吗"
if command -v nginx >/dev/null 2>&1; then
  pass "nginx 已安装（$(nginx -v 2>&1 | sed 's/^nginx version: //')）"
else
  bad "nginx 没装 —— sudo apt install -y nginx"
  note "nginx 没装：浏览器一定打不开。装完再跑一次本脚本。"
fi
if systemctl is-active --quiet nginx 2>/dev/null; then
  pass "nginx 服务运行中"
else
  bad "nginx 服务没在跑 —— sudo systemctl status nginx; sudo journalctl -u nginx -n 30"
  note "nginx 服务没起来：看 journalctl -u nginx -n 30。"
fi

say "2. 站点配置有没有生效"
if [ -d /etc/nginx/sites-enabled ]; then
  ENABLED="$(ls /etc/nginx/sites-enabled 2>/dev/null | tr '\n' ' ')"
  [ -n "$ENABLED" ] && pass "已启用站点：$ENABLED" || bad "sites-enabled 里是空的 —— 第 14 步的 ln -sf 没做"
else
  warn "没有 sites-enabled 目录，配置可能在 /etc/nginx/conf.d/"
fi
if command -v nginx >/dev/null 2>&1; then
  if sudo nginx -t >/tmp/ngx-test.log 2>&1; then
    pass "nginx -t 通过"
  else
    bad "nginx 配置有语法错误："
    sed 's/^/     /' /tmp/ngx-test.log | tail -5
    note "nginx -t 不通过：按提示改配置，改完 sudo nginx -t && sudo systemctl reload nginx。"
  fi
  # 实际生效的 root 与 listen，比看配置文件更可靠（可能被别的 site 覆盖）
  EFFECTIVE_ROOT="$(sudo nginx -T 2>/dev/null | awk '/^\s*root /{print $2; exit}' | tr -d ';')"
  printf '     生效的 root：%s\n' "${EFFECTIVE_ROOT:-（没读到）}"
  printf '     生效的 listen：%s\n' "$(sudo nginx -T 2>/dev/null | awk '/^\s*listen /{printf "%s ", $2}' | tr -d ';')"
  if [ -n "$EFFECTIVE_ROOT" ] && [ "$EFFECTIVE_ROOT" != "$ROOT" ]; then
    warn "root 指向 $EFFECTIVE_ROOT，但仓库在 $ROOT"
    note "nginx 的 root 与仓库路径不一致：首页会 404 或显示默认页。把 root 改成 $ROOT。"
  fi
  if [ -n "$EFFECTIVE_ROOT" ] && [ ! -f "$EFFECTIVE_ROOT/index.html" ]; then
    bad "$EFFECTIVE_ROOT/index.html 不存在"
    note "$EFFECTIVE_ROOT 下没有 index.html：nginx 找不到首页。检查 root 是否指对了目录。"
  fi
fi

say "3. 80 端口在监听吗、绑在哪"
LISTEN80="$(sudo ss -ltnp 2>/dev/null | awk '$4 ~ /:80$/ {print $4; exit}')"
if [ -n "$LISTEN80" ]; then
  pass "80 端口监听在 $LISTEN80"
  case "$LISTEN80" in
    127.0.0.1:80|\[::1\]:80)
      bad "只监听回环地址，外网进不来"
      note "80 只绑在 127.0.0.1：把配置里的 listen 改成 80（或 0.0.0.0:80），再 reload。"
      ;;
  esac
else
  bad "没有进程监听 80 端口"
  note "80 没人监听：nginx 没起来，或配置里的 listen 不是 80。"
fi
if sudo ss -ltnp 2>/dev/null | awk '$4 ~ /:8000$/ {found=1} END {exit !found}'; then
  pass "8000 端口有进程监听（后端）"
else
  bad "8000 没有监听 —— systemctl status whu-teaching"
fi

say "4. 本机自己访问（排除网络因素）"
C_ROOT="$(code http://127.0.0.1/)"
C_API="$(code http://127.0.0.1/api/v1/health)"
C_DIRECT="$(code http://127.0.0.1:8000/api/v1/health)"
C_LAN="$(code "http://$LAN_IP/")"
printf '     首页      http://127.0.0.1/            → %s\n' "$C_ROOT"
printf '     接口      http://127.0.0.1/api/v1/health → %s\n' "$C_API"
printf '     后端直连   http://127.0.0.1:8000/...     → %s\n' "$C_DIRECT"
printf '     本机IP    http://%s/       → %s\n' "$LAN_IP" "$C_LAN"

[ "$C_DIRECT" = "200" ] && pass "后端自己正常" || bad "后端 8000 也不通 —— 先修后端（journalctl -u whu-teaching -n 50）"
case "$C_ROOT" in
  200) pass "nginx 首页 200" ;;
  000) bad "连不上首页（拒绝连接或超时）" ;;
  403) bad "403：目录不可读，或 index 被 nginx 权限挡住" ;;
  404) bad "404：nginx 找不到 index.html（root 错），或路由落在了默认站点" ;;
  *)   bad "首页返回 $C_ROOT" ;;
esac
[ "$C_API" = "200" ] && pass "nginx → 后端 反代正常" || bad "接口经 nginx 返回 $C_API（后端 200 的话就是反代配置问题）"

say "5. 防火墙"
if command -v ufw >/dev/null 2>&1 && sudo ufw status 2>/dev/null | head -1 | grep -qi active; then
  printf '     ufw：\n'
  sudo ufw status 2>/dev/null | sed 's/^/       /' | head -8
  if sudo ufw status 2>/dev/null | grep -qE '^80(/tcp)?\s+ALLOW'; then
    pass "ufw 已放行 80"
  else
    bad "ufw 开着但没放行 80 —— sudo ufw allow 80,443/tcp"
    note "防火墙没放行 80：浏览器会一直转圈然后超时。sudo ufw allow 80,443/tcp。"
  fi
elif command -v firewall-cmd >/dev/null 2>&1 && systemctl is-active --quiet firewalld 2>/dev/null; then
  warn "firewalld 在跑，检查 80 是否放行：sudo firewall-cmd --list-ports"
else
  pass "没有启用 ufw/firewalld（系统层不拦）"
fi

say "结论"
printf '%s' "$DIAG" | sed 's/^/  /'
if [ -z "$DIAG" ]; then
  if [ "$C_ROOT" = "200" ] && [ "$C_LAN" = "200" ]; then
    cat <<'OK'
  服务器这一侧全通：首页 200、接口 200、防火墙没拦。
  那么问题在"从你的电脑到这台机器"这一段，常见三种：
    1) 校园网/机房出口封了 80 端口（最常见）——找网络管理员开，或临时改个高端口试；
    2) 这台机器没有公网 IP，115.156.72.1 是网关/别的机器，需要做端口映射；
    3) 你访问的地址写错了（https 而不是 http、少了端口、用了内网地址）。
  先在服务器上跑：curl -sI http://115.156.72.1/ | head -1
  如果这条在本机 200、你电脑上打不开，就是 1) 或 2)，跟我们代码无关。
OK
  elif [ "$C_ROOT" = "200" ]; then
    echo "  本机 127.0.0.1 正常、但用机器 IP 访问失败：nginx 只绑了回环，或本机 hosts 有干扰。"
  else
    echo "  先从上面第一个 ✗ 修起，修完再跑一次本脚本。"
  fi
fi
echo
echo "把这一整屏贴给我，我按结论给下一步。"
