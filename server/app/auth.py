"""身份与权限：一个入口，三种身份来源，同一套角色。

三种来源（AUTH_MODE）：
    dev       本地开发。优先读会话 cookie；没有 cookie 时退回请求头
              （X-Actor / X-Roles），方便 curl 和自动化测试。生产禁用。
    password  账号密码登录。只认会话 cookie，请求头一律忽略。
    cas       学校统一身份认证。**尚未接入**：目前显式返回 501，并说明要改哪里。
              不做"看起来能登其实没校验"的假实现——那比不做更危险。

关于角色（只有三个）：
    student / teacher / admin
    审核相关的旧代码写的是 require("reviewer")。这里不新增第四种身份，
    而是在 Actor.has() 里把 teacher 视作包含 reviewer —— 角色是"人是谁"，
    不该为了历史命名多出一类人。

接入统一认证只需要动两处：
    1. `cas_login_url` / `cas_verify_ticket` / 回调路由；
    2. 回调成功后调用 `issue_session()`，签发同一种会话。
权限判断、审计、页面闸门都只看 Actor.roles，不需要改。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote

try:
    from fastapi import Depends, Header, HTTPException, Request
except ModuleNotFoundError:  # 允许在未安装依赖时跑纯逻辑测试

    class HTTPException(Exception):  # type: ignore[no-redef]
        def __init__(self, status_code: int = 500, detail: str = "") -> None:
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    def Header(default=None, alias=None):  # type: ignore[no-redef]
        return default

    def Depends(dependency=None):  # type: ignore[no-redef]
        return dependency

    Request = object  # type: ignore[assignment,misc]

from . import db

AUTH_MODE = os.environ.get("AUTH_MODE", "dev")  # dev | password | cas

# 三种身份。请求头里出现别的值直接丢弃（不会因为手误被当 admin）。
ROLES = {"student", "teacher", "admin"}

# 请求头（dev 专用通道）额外接受历史角色名 reviewer：
# 老脚本与端到端测试一直这么写，改掉只会让测试先挂、权限没变。
# 账号表只允许 ROLES 三个值——reviewer 是传输层别名，不是第四种人。
HEADER_ROLES = ROLES | {"reviewer"}

# 历史命名：老接口用 require("reviewer") 要求"能审核的人"。
# teacher 天然包含这一层，admin 本来就有全部权限。
ROLE_IMPLIES = {"reviewer": ("teacher",)}

ROLE_LABELS = {"student": "学生", "teacher": "教师", "admin": "管理员"}

# 会话 cookie。名字带 whu_ 前缀，避免和校内其它系统撞名。
SESSION_COOKIE = "whu_session"
SESSION_TTL = timedelta(hours=float(os.environ.get("SESSION_TTL_HOURS", "12")))
# 生产必须 https 才带 cookie。本地 http 调试时由 APP_ENV 决定。
COOKIE_SECURE = os.environ.get("APP_ENV", "dev") == "prod"

# 密码强度下限。故意不搞"必须有大写+符号"那种规则：它们把用户推向 Passw0rd!，
# 真正有用的是长度（加上失败锁定与哈希迭代次数）。
PASSWORD_MIN = 8
PASSWORD_MAX = 200

# 连续失败多少次锁、锁多久
LOCK_AFTER = int(os.environ.get("AUTH_LOCK_AFTER", "5"))
LOCK_FOR = timedelta(minutes=float(os.environ.get("AUTH_LOCK_MINUTES", "10")))

# 开发期的兜底角色：**默认空**。
# 以前默认 admin，结果是"没登录也什么都能干"，登录按钮就成了装饰。
# 需要免登录联调时显式设 AUTH_DEV_ROLES=admin。
DEV_ROLES = frozenset(
    r.strip() for r in os.environ.get("AUTH_DEV_ROLES", "").split(",")
    if r.strip() in ROLES
)


@dataclass(frozen=True)
class Actor:
    id: str
    roles: frozenset[str] = frozenset()
    display_name: str = ""
    source: str = "anonymous"  # session | header | anonymous | cas

    @property
    def anonymous(self) -> bool:
        return not self.id

    def has(self, *roles: str) -> bool:
        if "admin" in self.roles:
            return True
        for wanted in roles:
            if wanted in self.roles:
                return True
            # reviewer 由 teacher 覆盖：不新增第四种身份
            if wanted in ROLE_IMPLIES and any(r in self.roles for r in ROLE_IMPLIES[wanted]):
                return True
        return False

    @property
    def label(self) -> str:
        return self.display_name or self.id

    def as_dict(self) -> dict:
        primary = next(
            iter(sorted(self.roles, key=lambda r: ("admin", "teacher", "student").index(r))),
            "",
        )
        return {
            "id": self.id,
            "display_name": self.display_name,
            "roles": sorted(self.roles),
            "role": primary,
            "role_label": ROLE_LABELS.get(primary, ""),
            "source": self.source,
        }


ANONYMOUS = Actor(id="")


# ---------------------------------------------------------------------------
# 密码
# ---------------------------------------------------------------------------

def hash_password(password: str) -> str:
    """PBKDF2-HMAC-SHA256。标准库就够，不需要装 bcrypt/argon2。

    格式：pbkdf2_sha256$<迭代次数>$<salt_hex>$<hash_hex>
    把参数写进字符串，日后提高迭代次数不会让老密码失效。
    """
    if not password or len(password) > PASSWORD_MAX:
        raise ValueError(f"密码长度需在 1–{PASSWORD_MAX} 之间")
    iterations = int(os.environ.get("AUTH_PBKDF2_ITERATIONS", "260000"))
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    """常数时间比较。stored 为空（只能走统一认证）时一律 False。"""
    if not stored or not password:
        return False
    try:
        algo, iterations, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations)
        )
        return hmac.compare_digest(digest.hex(), hash_hex)
    except (ValueError, TypeError):
        return False


def password_problem(password: str) -> str | None:
    """返回不合格的原因，合格返回 None。"""
    if not password or len(password) < PASSWORD_MIN:
        return f"密码至少 {PASSWORD_MIN} 位"
    if len(password) > PASSWORD_MAX:
        return f"密码太长（上限 {PASSWORD_MAX} 位）"
    if password.strip() != password:
        return "密码首尾不能有空格"
    return None


# ---------------------------------------------------------------------------
# 会话
# ---------------------------------------------------------------------------

def new_session_token() -> tuple[str, str]:
    """返回 (原文令牌, 入库的哈希)。原文只进 cookie，从不落库。"""
    token = secrets.token_urlsafe(32)
    return token, token_hash(token)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_session(account_id: str, request: Request | None = None) -> tuple[str, datetime]:
    """签发会话，返回 (原文令牌, 过期时间)。调用方负责写 cookie。"""
    token, hashed = new_session_token()
    expires_at = datetime.now(timezone.utc) + SESSION_TTL
    db.execute(
        """INSERT INTO app_session (token_hash, account_id, expires_at, ip, user_agent)
           VALUES (%s, %s, %s, %s, %s)""",
        (
            hashed,
            account_id,
            expires_at,
            client_ip(request),
            (request.headers.get("user-agent", "")[:300] if request is not None else None),
        ),
    )
    return token, expires_at


def revoke_session(token: str | None) -> None:
    if not token:
        return
    db.execute(
        """UPDATE app_session SET revoked_at = now()
            WHERE token_hash = %s AND revoked_at IS NULL""",
        (token_hash(token),),
    )


def session_actor(token: str | None) -> Actor | None:
    """用 cookie 令牌换身份。过期、被撤销、账号停用一律当没登录。"""
    if not token:
        return None
    row = db.query_one(
        """SELECT a.id, a.display_name, a.role, s.id AS session_id
             FROM app_session s
             JOIN app_account a ON a.id = s.account_id
            WHERE s.token_hash = %s
              AND s.revoked_at IS NULL
              AND s.expires_at > now()
              AND a.disabled_at IS NULL""",
        (token_hash(token),),
    )
    if not row:
        return None
    db.execute("UPDATE app_session SET last_seen_at = now() WHERE id = %s", (row["session_id"],))
    return Actor(
        id=row["id"],
        roles=frozenset({row["role"]}),
        display_name=row["display_name"],
        source="session",
    )


def client_ip(request: Request | None) -> str | None:
    if request is None:
        return None
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()[:60]
    return getattr(request.client, "host", None)


def set_session_cookie(response, token: str, expires_at: datetime) -> None:
    max_age = max(0, int((expires_at - datetime.now(timezone.utc)).total_seconds()))
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=max_age,
        httponly=True,    # JS 读不到，XSS 也偷不走
        samesite="lax",   # 跨站表单拿不到：防 CSRF 的第一层
        secure=COOKIE_SECURE,
        path="/",
    )


def clear_session_cookie(response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


# ---------------------------------------------------------------------------
# 统一认证（CAS）——留口，不假装已接
# ---------------------------------------------------------------------------

CAS_AVAILABLE = False


def cas_login_url(next_path: str = "/") -> str:
    """接入后：拼学校 CAS 的 /login?service=<回调地址>，带上 next。"""
    raise HTTPException(
        501,
        "统一身份认证尚未接入。接入点已留好：server/app/auth.py 里的 "
        "cas_login_url / cas_verify_ticket，以及 /api/v1/auth/cas/callback 回调路由。"
        "接上之后登录页的「学校统一身份认证」按钮即可用。",
    )


def cas_verify_ticket(ticket: str, service: str) -> Actor:
    """接入后：调学校 CAS 的 serviceValidate，取出学工号，映射成 Actor。"""
    raise HTTPException(
        501,
        "统一身份认证尚未接入。这里要换成校验 CAS 票据（serviceValidate），"
        "拿到学工号后按用户名找 app_account（找不到可按规则自动建），"
        "再调用 issue_session() 签发会话。账号密码登录与权限判断不需要改。",
    )


# ---------------------------------------------------------------------------
# 身份解析
# ---------------------------------------------------------------------------

def resolve_header_actor(x_actor: str | None, x_roles: str | None) -> Actor:
    """开发期：从请求头读身份。只在 AUTH_MODE=dev 下被调用。"""
    # HTTP 头只能是 ASCII，中文姓名由前端 encodeURIComponent 后传过来。
    actor_id = unquote((x_actor or "").strip())
    raw_roles = (x_roles or "").strip()
    roles = frozenset(r.strip() for r in raw_roles.split(",") if r.strip() in HEADER_ROLES)
    if not roles and not raw_roles and DEV_ROLES:
        roles = DEV_ROLES
    if not actor_id and not roles:
        return ANONYMOUS
    return Actor(id=actor_id or "dev", roles=roles, display_name=actor_id, source="header")


def resolve_actor(
    x_actor: str | None = None,
    x_roles: str | None = None,
    session_token: str | None = None,
) -> Actor:
    """唯一的身份入口。顺序：会话 → （dev）请求头 → 匿名。"""
    if AUTH_MODE == "cas" and not CAS_AVAILABLE:
        # 接入前先拒绝：宁可"登录不了"，也不能"谁都能进"。
        raise HTTPException(
            501,
            "统一身份认证尚未接入（AUTH_MODE=cas）。本地请用 AUTH_MODE=dev 或 password。",
        )

    actor = session_actor(session_token)
    if actor:
        return actor

    if AUTH_MODE == "dev":
        return resolve_header_actor(x_actor, x_roles)

    return ANONYMOUS


def current_actor(
    request: Request,
    x_actor: str | None = Header(default=None, alias="X-Actor"),
    x_roles: str | None = Header(default=None, alias="X-Roles"),
) -> Actor:
    token = None
    try:
        token = request.cookies.get(SESSION_COOKIE)
    except AttributeError:  # 纯逻辑测试里传的是假 request
        token = None
    return resolve_actor(x_actor, x_roles, token)


def actor_from_request(request: Request) -> Actor:
    """在普通函数里（不是依赖注入）取身份时用它。

    直接调 current_actor(request) 是不行的：那个函数的其余参数是 Header(...)，
    直接把 request 传进去会把 Header 对象当成名字。这个坑踩过一次。
    """
    cookies = getattr(request, "cookies", {}) or {}
    headers = getattr(request, "headers", {}) or {}
    return resolve_actor(
        headers.get("x-actor"),
        headers.get("x-roles"),
        cookies.get(SESSION_COOKIE),
    )


def require_login(actor: Actor = Depends(current_actor)) -> Actor:
    """只要求登录，不限角色。问答、作答这类"谁都能做，但要记在某人名下"的动作用它。"""
    if actor.anonymous:
        raise HTTPException(401, "请先登录")
    return actor


def require(*roles: str):
    """依赖注入：要求登录，且具有其中任一角色。

    未登录返回 401（前端据此跳登录页），登录了但权限不够返回 403——
    两者要分开，否则前端只能猜"该不该弹登录框"。
    """

    def dependency(actor: Actor = Depends(current_actor)) -> Actor:
        if actor.anonymous:
            raise HTTPException(401, "请先登录")
        if not roles:
            return actor
        if not actor.has(*roles):
            raise HTTPException(
                403,
                f"需要角色之一：{'/'.join(ROLE_LABELS.get(r, r) for r in roles)}，"
                f"当前 {ROLE_LABELS.get(next(iter(actor.roles), ''), '未登录') or '未登录'}",
            )
        return actor

    return dependency
