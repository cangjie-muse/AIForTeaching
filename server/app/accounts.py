"""登录、登出、会话、账号管理。

为什么单独一个路由文件：
    auth.py 只管"身份是什么"，这里管"怎么证明你是你"。接统一认证时新增的是
    本文件里的 cas 回调，auth.py 的权限判断不用动。

三条底线：
    1. 密码只以 PBKDF2 哈希落库，原文永不保存、不打日志；
    2. cookie 是 httpOnly + SameSite=Lax，库里的会话令牌是哈希；
    3. 连续失败锁定——不锁的话，字典脚本半夜跑，日志里只会多出几千条 401。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

from . import auth, db

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=60)
    password: str = Field(min_length=1, max_length=auth.PASSWORD_MAX)
    next: str | None = None


class PasswordIn(BaseModel):
    current_password: str = Field(min_length=1, max_length=auth.PASSWORD_MAX)
    new_password: str = Field(min_length=auth.PASSWORD_MIN, max_length=auth.PASSWORD_MAX)


class AccountIn(BaseModel):
    username: str = Field(min_length=2, max_length=60)
    display_name: str = Field(min_length=1, max_length=60)
    role: str
    password: str | None = None
    must_change_password: bool = False


class AccountPatch(BaseModel):
    display_name: str | None = Field(default=None, max_length=60)
    role: str | None = None
    password: str | None = None
    disabled: bool | None = None
    must_change_password: bool | None = None


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def _account(username: str) -> dict | None:
    return db.query_one(
        """SELECT id, username, display_name, role, password_hash, failed_attempts,
                  locked_until, disabled_at, must_change_password, last_login_at,
                  created_at,
                  (locked_until IS NOT NULL AND locked_until > now()) AS locked
             FROM app_account WHERE username = %s""",
        (username.strip().lower(),),
    )


def _audit(actor: auth.Actor, action: str, entity_key: str, detail: dict) -> None:
    db.execute(
        """INSERT INTO audit_log (actor, action, entity, entity_key, detail)
           VALUES (%s, %s, 'app_account', %s, %s::jsonb)""",
        (actor.label, action, entity_key, json.dumps(detail, ensure_ascii=False)),
    )


def _public_account(row: dict) -> dict:
    return {
        "id": row["id"],
        "username": row.get("username", ""),
        "display_name": row["display_name"],
        "role": row["role"],
        "role_label": auth.ROLE_LABELS.get(row["role"], row["role"]),
        "disabled": bool(row.get("disabled_at")),
        "must_change_password": bool(row.get("must_change_password")),
        "last_login_at": row.get("last_login_at"),
        "created_at": row.get("created_at"),
        # 有没有本地密码：没有就是"只能走统一认证"
        "has_password": bool(row.get("password_hash")),
        "locked": bool(row.get("locked")),
    }


def _check_role(role: str) -> str:
    if role not in auth.ROLES:
        raise HTTPException(400, f"角色只能是 {'/'.join(sorted(auth.ROLES))}，收到 {role}")
    return role


# ---------------------------------------------------------------------------
# 会话
# ---------------------------------------------------------------------------

@router.get("/providers")
def providers() -> dict:
    """登录页据此决定显示哪些登录方式。

    CAS 现在 available=false，前端把按钮置灰并写明"待接入"，而不是让人点了报错。
    """
    return {
        "auth_mode": auth.AUTH_MODE,
        "password": {
            "id": "password",
            "label": "账号密码登录",
            "available": True,
            "hint": "本地账号，由管理员创建",
        },
        "cas": {
            "id": "cas",
            "label": "学校统一身份认证",
            "available": auth.CAS_AVAILABLE,
            "hint": "待接入：接口与回调位置已留好（见 docs/账号与权限.md）",
            "login_path": "/api/v1/auth/cas/login",
        },
        "session_hours": auth.SESSION_TTL.total_seconds() / 3600,
    }


@router.get("/me")
def me(request: Request) -> dict:
    """当前登录状态。未登录也返回 200——前端用它决定显示"登录"还是"退出"。"""
    actor = auth.actor_from_request(request)
    response = {"authenticated": not actor.anonymous, "actor": actor.as_dict()}
    if not actor.anonymous:
        row = db.query_one(
            "SELECT must_change_password FROM app_account WHERE id = %s", (actor.id,)
        )
        response["must_change_password"] = bool(row and row["must_change_password"])
    return response


@router.post("/login")
def login(payload: LoginIn, request: Request) -> Response:
    username = payload.username.strip().lower()
    row = _account(username)
    now = datetime.now()

    # 账号不存在与密码错误给同一句话：不告诉试探者"这个用户名存在"。
    if not row or not row.get("password_hash"):
        raise HTTPException(401, "账号或密码不正确")
    if row.get("disabled_at"):
        raise HTTPException(403, "这个账号已停用，请联系管理员")
    if row.get("locked"):
        raise HTTPException(
            423,
            f"密码错误次数过多，请 {int(auth.LOCK_FOR.total_seconds() // 60)} 分钟后再试",
        )

    if not auth.verify_password(payload.password, row["password_hash"]):
        attempts = int(row["failed_attempts"] or 0) + 1
        locked = now + auth.LOCK_FOR if attempts >= auth.LOCK_AFTER else None
        db.execute(
            """UPDATE app_account
                  SET failed_attempts = %s, locked_until = %s
                WHERE id = %s""",
            (attempts, locked, row["id"]),
        )
        if locked:
            _audit(auth.ANONYMOUS, "auth.lock", username,
                   {"attempts": attempts, "locked_until": str(locked)})
            raise HTTPException(
                423, f"密码错误次数过多，账号锁定 {int(auth.LOCK_FOR.total_seconds() // 60)} 分钟"
            )
        raise HTTPException(401, f"账号或密码不正确（还可尝试 {auth.LOCK_AFTER - attempts} 次）")

    # 成功：清零失败计数、记最后登录时间
    db.execute(
        """UPDATE app_account
              SET failed_attempts = 0, locked_until = NULL, last_login_at = now()
            WHERE id = %s""",
        (row["id"],),
    )
    token, expires_at = auth.issue_session(row["id"], request)
    actor = auth.Actor(id=row["id"], roles=frozenset({row["role"]}),
                       display_name=row["display_name"], source="session")
    _audit(actor, "auth.login", username, {"ip": auth.client_ip(request)})

    response = JSONResponse({
        "authenticated": True,
        "actor": actor.as_dict(),
        "must_change_password": bool(row.get("must_change_password")),
        "expires_at": expires_at.isoformat(),
        "next": payload.next or None,
    })
    auth.set_session_cookie(response, token, expires_at)
    return response


@router.post("/logout")
def logout(request: Request) -> Response:
    token = request.cookies.get(auth.SESSION_COOKIE)
    actor = auth.session_actor(token)
    auth.revoke_session(token)
    if actor:
        _audit(actor, "auth.logout", actor.id, {})
    response = JSONResponse({"authenticated": False})
    auth.clear_session_cookie(response)
    return response


@router.post("/password")
def change_password(payload: PasswordIn, request: Request) -> dict:
    """改自己的密码。改完把其它会话全部下线——密码泄露时这是唯一有效的动作。"""
    actor = auth.actor_from_request(request)
    if actor.anonymous:
        raise HTTPException(401, "请先登录")

    problem = auth.password_problem(payload.new_password)
    if problem:
        raise HTTPException(400, problem)
    if payload.new_password == payload.current_password:
        raise HTTPException(400, "新密码不能和旧密码相同")

    row = db.query_one(
        "SELECT id, password_hash FROM app_account WHERE id = %s", (actor.id,)
    )
    if not row or not auth.verify_password(payload.current_password, row["password_hash"]):
        raise HTTPException(400, "当前密码不正确")

    db.execute(
        """UPDATE app_account
              SET password_hash = %s, password_updated_at = now(),
                  must_change_password = false, failed_attempts = 0, locked_until = NULL
            WHERE id = %s""",
        (auth.hash_password(payload.new_password), actor.id),
    )
    # 其它会话下线，保留当前这条
    db.execute(
        """UPDATE app_session SET revoked_at = now()
            WHERE account_id = %s AND revoked_at IS NULL
              AND token_hash <> %s""",
        (actor.id, auth.token_hash(request.cookies.get(auth.SESSION_COOKIE) or "")),
    )
    _audit(actor, "auth.password", actor.id, {})
    return {"changed": True}


# ---------------------------------------------------------------------------
# 账号管理（仅管理员）
# ---------------------------------------------------------------------------

@router.get("/accounts")
def list_accounts(
    role: str | None = None,
    query: str | None = None,
    actor: auth.Actor = Depends(auth.require("admin")),
) -> dict:
    where = "WHERE 1 = 1"
    params: list = []
    if role:
        where += " AND role = %s"
        params.append(_check_role(role))
    if query:
        where += " AND (username ILIKE %s OR display_name ILIKE %s)"
        pattern = f"%{query.strip()}%"
        params.extend([pattern, pattern])
    rows = db.query(
        f"""SELECT id, username, display_name, role, password_hash, must_change_password,
                   disabled_at, last_login_at, created_at,
                   (locked_until IS NOT NULL AND locked_until > now()) AS locked
              FROM app_account {where}
             ORDER BY CASE role WHEN 'admin' THEN 0 WHEN 'teacher' THEN 1 ELSE 2 END,
                      username""",
        tuple(params),
    )
    counts = db.query("SELECT role, count(*)::int AS n FROM app_account GROUP BY role")
    return {
        "items": [_public_account(row) for row in rows],
        "counts": {row["role"]: row["n"] for row in counts},
        "total": len(rows),
    }


@router.post("/accounts")
def create_account(
    payload: AccountIn,
    request: Request,
    actor: auth.Actor = Depends(auth.require("admin")),
) -> dict:
    username = payload.username.strip().lower()
    if not username.replace(".", "").replace("_", "").replace("-", "").isalnum():
        raise HTTPException(400, "账号只能用字母、数字、点、下划线、短横线")
    if _account(username):
        raise HTTPException(409, f"账号已存在：{username}")
    role = _check_role(payload.role)

    hashed = None
    if payload.password:
        problem = auth.password_problem(payload.password)
        if problem:
            raise HTTPException(400, problem)
        hashed = auth.hash_password(payload.password)

    account_id = f"acc-{uuid.uuid4().hex[:12]}"
    db.execute(
        """INSERT INTO app_account
             (id, username, display_name, role, password_hash, password_updated_at,
              must_change_password)
           VALUES (%s, %s, %s, %s, %s, %s, %s)""",
        (account_id, username, payload.display_name.strip(), role, hashed,
         datetime.now() if hashed else None, payload.must_change_password),
    )
    _audit(actor, "account.create", username,
           {"role": role, "has_password": bool(hashed)})
    row = _account(username)
    return _public_account(row)


@router.patch("/accounts/{account_id}")
def patch_account(
    account_id: str,
    payload: AccountPatch,
    actor: auth.Actor = Depends(auth.require("admin")),
) -> dict:
    row = db.query_one(
        """SELECT id, username, display_name, role, password_hash, must_change_password,
                  disabled_at, last_login_at, created_at,
                  (locked_until IS NOT NULL AND locked_until > now()) AS locked
             FROM app_account WHERE id = %s""",
        (account_id,),
    )
    if not row:
        raise HTTPException(404, "账号不存在")

    updates, params = [], []
    if payload.display_name is not None:
        updates.append("display_name = %s")
        params.append(payload.display_name.strip())
    if payload.role is not None:
        role = _check_role(payload.role)
        # 不能把最后一个管理员降级，否则平台再也没人能管账号
        if row["role"] == "admin" and role != "admin":
            admins = db.query_one(
                "SELECT count(*)::int AS n FROM app_account WHERE role = 'admin' AND disabled_at IS NULL"
            )
            if admins and admins["n"] <= 1:
                raise HTTPException(400, "这是最后一个管理员，不能改成别的角色")
        updates.append("role = %s")
        params.append(role)
    if payload.password is not None:
        problem = auth.password_problem(payload.password)
        if problem:
            raise HTTPException(400, problem)
        updates.append("password_hash = %s")
        params.append(auth.hash_password(payload.password))
        updates.append("password_updated_at = now()")
        updates.append("failed_attempts = 0")
        updates.append("locked_until = NULL")
        # 重置密码后，这个人已登录的会话全部失效
        db.execute(
            "UPDATE app_session SET revoked_at = now() WHERE account_id = %s AND revoked_at IS NULL",
            (account_id,),
        )
    if payload.must_change_password is not None:
        updates.append("must_change_password = %s")
        params.append(payload.must_change_password)
    if payload.disabled is not None:
        if payload.disabled and row["id"] == actor.id:
            raise HTTPException(400, "不能停用自己的账号")
        if payload.disabled and row["role"] == "admin":
            admins = db.query_one(
                "SELECT count(*)::int AS n FROM app_account WHERE role = 'admin' AND disabled_at IS NULL"
            )
            if admins and admins["n"] <= 1:
                raise HTTPException(400, "这是最后一个管理员，不能停用")
        updates.append("disabled_at = CASE WHEN %s::boolean THEN now() ELSE NULL END")
        params.append(payload.disabled)
        if payload.disabled:
            db.execute(
                "UPDATE app_session SET revoked_at = now() WHERE account_id = %s AND revoked_at IS NULL",
                (account_id,),
            )
    if not updates:
        raise HTTPException(400, "没有要修改的字段")

    db.execute(
        f"UPDATE app_account SET {', '.join(updates)} WHERE id = %s",
        (*params, account_id),
    )
    _audit(actor, "account.update", row["username"],
           {key: value for key, value in payload.model_dump(exclude_none=True).items()
            if key != "password"})
    row = db.query_one(
        """SELECT id, username, display_name, role, password_hash, must_change_password,
                  disabled_at, last_login_at, created_at,
                  (locked_until IS NOT NULL AND locked_until > now()) AS locked
             FROM app_account WHERE id = %s""",
        (account_id,),
    )
    return _public_account(row)


# ---------------------------------------------------------------------------
# 统一认证回调位置（留口）
# ---------------------------------------------------------------------------

@router.get("/cas/login")
def cas_login(request: Request) -> Response:
    """接入后：返回 302 跳到学校 CAS 登录页。现在是明确的 501。"""
    try:
        target = auth.cas_login_url("/")
    except auth.HTTPException as error:  # type: ignore[attr-defined]
        raise HTTPException(error.status_code, error.detail) from error
    return RedirectResponse(target, status_code=302)


@router.get("/cas/callback")
def cas_callback(request: Request, ticket: str = "") -> Response:
    """接入后：校验票据 → 找/建 account → issue_session() → 跳回页面。

    留这个空壳而不是干脆不写，是为了让"接入时要动哪几个函数"一眼可见：
    这里 + auth.cas_verify_ticket() 是全部工作量。
    """
    raise HTTPException(
        501,
        "统一身份认证尚未接入。接入步骤见 docs/账号与权限.md："
        "① 在 auth.cas_login_url 拼 CAS 登录地址；"
        "② 在本回调里用 ticket 调 serviceValidate，拿到学工号；"
        "③ 按学工号找或建 app_account（role 默认 student，可配置白名单自动升教师）；"
        "④ 调 auth.issue_session() 写 cookie；"
        "⑤ 把 AUTH_MODE 设为 cas。",
    )
