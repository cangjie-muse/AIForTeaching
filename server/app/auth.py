"""身份解析：开发期信任请求头，接统一认证时只改一处。

当前（开发期）：
    AUTH_MODE=dev 时，从 X-Actor / X-Roles 头读取。方便用 curl 和前端联调。

将来接学校统一身份认证（CAS / OAuth2 / 校内 SSO）：
    1. 把 CAS 的票据校验写在 resolve_actor() 里，返回统一的 Actor。
    2. 把 AUTH_MODE 默认值改成 cas。
    3. 其余代码一行不动——所有接口都通过 current_actor / require 取身份。

留口子的关键不是"加个 if"，而是**所有写操作从第一天就带 actor**：
audit_log.actor、exercise_revision.changed_by、human_verified_by 全都已经要求这个值。
所以接认证时不需要回头补数据，只是把"信任请求头"换成"信任票据"。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from urllib.parse import unquote

try:
    from fastapi import Depends, Header, HTTPException
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

AUTH_MODE = os.environ.get("AUTH_MODE", "dev")   # dev | cas（cas 待实现）

ROLES = {"student", "teacher", "reviewer", "admin"}

# 开发期的默认角色。没有它，本地连点"保存"都要手写 curl 头，最后大家会
# 直接把 require() 注释掉——那比现在这样危险得多。
# 生产必须 AUTH_MODE=cas，此时本变量不生效（见 main.py 的启动检查）。
DEV_ROLES = frozenset(
    r.strip() for r in os.environ.get("AUTH_DEV_ROLES", "admin").split(",")
    if r.strip() in ROLES
)


@dataclass(frozen=True)
class Actor:
    id: str
    roles: frozenset[str] = field(default_factory=frozenset)

    def has(self, *roles: str) -> bool:
        # admin 拥有一切权限，避免到处写 "admin or ..."
        return "admin" in self.roles or any(r in self.roles for r in roles)


def resolve_actor(x_actor: str | None, x_roles: str | None) -> Actor:
    """唯一的身份入口。接 CAS 时改这个函数就够了。"""
    if AUTH_MODE == "cas":
        # TODO(认证对接)：这里换成校验 CAS 票据 / OAuth2 token。
        # 在完成之前直接拒绝，避免"以为接了其实没接"。
        raise HTTPException(501, "统一身份认证尚未接入（AUTH_MODE=cas）")

    # HTTP 头只能是 ASCII，中文姓名由前端 encodeURIComponent 后传过来，
    # 这里解回原文。否则浏览器根本发不出这个头（fetch 会抛 TypeError）。
    actor_id = unquote((x_actor or "").strip()) or "anonymous"
    raw_roles = (x_roles or "").strip()
    roles = frozenset(r.strip() for r in raw_roles.split(",") if r.strip() in ROLES)
    # 只有"完全没传角色"才套用开发默认值。
    # 传了但全是无效角色（如 superuser）时保持为空——否则手误会被当成 admin，
    # 而这恰恰是要在开发期就能看见的行为。
    if not roles and AUTH_MODE == "dev" and not raw_roles:
        roles = DEV_ROLES
    return Actor(id=actor_id, roles=roles)


def current_actor(
    x_actor: str | None = Header(default=None, alias="X-Actor"),
    x_roles: str | None = Header(default=None, alias="X-Roles"),
) -> Actor:
    return resolve_actor(x_actor, x_roles)


def require(*roles: str):
    """依赖注入：要求具有其中任一角色。

    用法：
        @router.post("/publish")
        def publish(actor: Actor = Depends(require("teacher", "reviewer"))): ...
    """

    def dependency(actor: Actor = Depends(current_actor)) -> Actor:
        if not roles:
            return actor
        if not actor.has(*roles):
            raise HTTPException(403, f"需要角色之一：{'/'.join(roles)}，当前 {sorted(actor.roles) or '无'}")
        return actor

    return dependency
