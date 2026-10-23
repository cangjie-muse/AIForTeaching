"""出校策略：哪些主机可以收到"含校内内容"的请求。

单独一个模块的原因：这条策略现在有两处入口——`.env` 里的 `MODEL_ALLOWED_HOSTS`
（运维写的基线）和管理员在「模型配置 → 出校域名」里加的（存 `approved_host` 表）。
两边都要用同一套判断，复制一份迟早会漂移。

这里只放纯函数，不碰数据库：谁需要把库里的域名合并进来，谁自己去查（见 llm.py）。
这样 egress 不依赖 settings、settings 可以放心 import egress 做参数校验。
"""

from __future__ import annotations

import ipaddress
import os
import re
import urllib.parse

# ---------------------------------------------------------------------------
# 基线：默认白名单与校内判定
# ---------------------------------------------------------------------------

# 已批准可用于含校内内容请求的供应商域名（含子域）
APPROVED_HOSTS: frozenset[str] = frozenset(
    h.strip().lower()
    for h in os.environ.get(
        "MODEL_ALLOWED_HOSTS",
        "api.deepseek.com,api.moonshot.cn,platform.kimi.com,"
        "dashscope.aliyuncs.com,dashscope-intl.aliyuncs.com,open.bigmodel.cn,"
        "ark.cn-beijing.volces.com,ark.cn-shanghai.volces.com,"
        "sophnet.com,www.sophnet.com",
    ).split(",")
    if h.strip()
)

# 这些后缀一律视为校内地址：加进白名单没有意义，也不该允许加
INTERNAL_SUFFIXES = (".edu.cn", ".local", ".internal", ".lan")

EGRESS_MODE = os.environ.get("MODEL_EGRESS", "allowlist").lower()
VALID_MODES = {"allowlist", "any", "internal-only"}

# 一个合法域名：至少两段，字母数字与连字符，不能以连字符开头结尾
HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$")


def _host(base_url: str) -> str:
    try:
        return (urllib.parse.urlparse(base_url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def is_internal_host(host: str) -> bool:
    """主机名是不是校内地址（本机、私网、校内域名）。"""
    host = (host or "").lower().rstrip(".")
    if not host:
        return False
    if host in ("localhost", "127.0.0.1", "::1"):
        return True
    if host.endswith(INTERNAL_SUFFIXES):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local


def is_internal_url(base_url: str) -> bool:
    return is_internal_host(_host(base_url))


def normalize_host(raw: str) -> str:
    """把管理员粘进来的东西收拾成一个裸域名。

    人习惯整段粘 `https://api.example.com/v1/chat/completions`，直接存进去
    会让白名单永远匹配不上（之后报"未批准"却看不出为什么）。这里统一剥掉
    scheme、路径、端口、末尾的点。
    """
    text = (raw or "").strip().lower()
    if "://" in text:
        text = text.split("://", 1)[1]
    text = text.split("/", 1)[0].split("?", 1)[0]
    if "@" in text:
        raise ValueError("只要域名，不要把用户名或密钥粘进来")
    host = text.split(":", 1)[0].strip().rstrip(".")
    return host


def validate_host(raw: str) -> tuple[str, str]:
    """返回 (规范化域名, 错误说明)。错误说明为空表示通过。"""
    try:
        host = normalize_host(raw)
    except ValueError as error:
        return "", str(error)
    if not host:
        return "", "域名为空"
    if "*" in host:
        return "", "不支持通配符：把要放行的每个域名分别加进来，通配符会让审核变成一句空话"
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return "", "这里填域名，不要填 IP"
    # 校内判定要在正则之前：localhost 过不了"至少两段"的规则，
    # 但真正的原因是它本来就不会外发——照实说，别让人去猜格式
    if is_internal_host(host):
        return "", f"{host} 是校内地址，本来就不会外发，不需要加进白名单"
    if not HOST_RE.match(host):
        return "", f"看不出来是个域名：{host}"
    return host, ""


Hosts = "frozenset[str] | set[str] | tuple[str, ...]"


def matches(host: str, items: Hosts) -> bool:
    """域名相同或是其子域。按域名边界匹配：evil-sophnet.com 不算 sophnet.com。"""
    return any(host == item or host.endswith("." + item) for item in items)


def is_blocked(base_url: str, blocked: Hosts = ()) -> bool:
    """被管理员屏蔽。子域一起算——屏蔽 sophnet.com 却放过 www.sophnet.com
    等于没屏蔽，而人不会记得两个都填。"""
    host = _host(base_url)
    return bool(host) and matches(host, blocked)


def is_approved(base_url: str, extra: Hosts = (), blocked: Hosts = ()) -> bool:
    """已批准：域名相同或是其子域。`extra` 是库里管理员加的域名。"""
    host = _host(base_url)
    if not host or matches(host, blocked):
        return False
    return matches(host, set(APPROVED_HOSTS) | set(extra))


def classify(base_url: str, extra: Hosts = (), blocked: Hosts = ()) -> str:
    """返回 internal / blocked / approved / unapproved / invalid。

    blocked 单独一档而不是并进 unapproved：排查时要能一眼看出是
    "没人批准过这家" 还是 "有人主动屏蔽了这家"，两者该做的事完全不同。
    """
    if not _host(base_url):
        return "invalid"
    if is_internal_url(base_url):
        return "internal"
    if is_blocked(base_url, blocked):
        return "blocked"
    if matches(_host(base_url), set(APPROVED_HOSTS) | set(extra)):
        return "approved"
    return "unapproved"
