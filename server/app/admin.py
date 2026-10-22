"""模型接口配置 API：管理员可改，审核员只读。

三个安全决定，都不是形式主义：

1. **密钥不进数据库。** 存服务器文件（0600），库里只存路径。数据库备份、
   只读副本、临时 dump 都不会把密钥带走。代价是迁移时要单独搬 secrets/ 目录
   ——这条已写进部署指南。

2. **密钥永不回显。** 读取接口只给 key_configured 布尔值。前端再怎么问，
   服务器也不会把密钥发出去；日志里也不打印。

3. **改配置必须 admin。** 审核员能看（知道现在用的是哪个模型），不能改。
   所有写操作都写 audit_log——包括"谁把这家供应商启用了"。

另外做了一件产品上的小事：保存时校验 data_scope 与地址是否自相矛盾。
标成"校内"却填着 api.deepseek.com，是最容易发生的一种配置错误，
而且它不会报错，只会让"这条数据到底出没出校"永远说不清。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from . import db, keystore, llm, settings
from .auth import Actor, current_actor, require

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])

# 密钥目录。开发默认 ./secrets，生产建议 /srv/whu/secrets 且属于服务账号。
SECRETS_DIR = Path(os.environ.get("SECRETS_DIR", "secrets")).resolve()

# 名称会变成文件名，必须严格限制字符集，否则 ".." 就能写出目录
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

_SHARED_COLUMNS = """
  name, kind, base_url, model, data_scope, dim, max_context, enabled,
  notes, approved_by, approved_at, updated_by, updated_at,
  api_key_updated_at, last_test_at, last_test_ok, last_test_note
"""

# 读：走视图。视图不含 api_key_path，所以"读到密钥路径"这件事在 SQL 层就不可能发生。
VIEW_COLUMNS = f"{_SHARED_COLUMNS}, key_configured"

# 写：INSERT/UPDATE ... RETURNING 不能引用视图，只能在表上算这一个布尔值。
# 注意它算出来的是"是否配置过"，不是密钥本身。
RETURNING_COLUMNS = f"{_SHARED_COLUMNS}, (api_key_path IS NOT NULL) AS key_configured"


# ---------------------------------------------------------------------------
# 入参
# ---------------------------------------------------------------------------

class EndpointIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    kind: str = Field(..., pattern="^(chat|embedding)$")
    base_url: str = Field(..., min_length=1)
    model: str = Field(..., min_length=1, max_length=200)
    data_scope: str = Field("external", pattern="^(internal|external)$")
    dim: int | None = None
    max_context: int | None = None
    notes: str | None = None
    enabled: bool = True


class EndpointPatch(BaseModel):
    base_url: str | None = None
    model: str | None = None
    data_scope: str | None = Field(None, pattern="^(internal|external)$")
    dim: int | None = None
    max_context: int | None = None
    notes: str | None = None
    enabled: bool | None = None


class KeyIn(BaseModel):
    api_key: str = Field(..., min_length=8, max_length=500)


class QaFallbackIn(BaseModel):
    enabled: bool


# ---------------------------------------------------------------------------
# 校验与密钥文件
# ---------------------------------------------------------------------------

def _validate_scope(base_url: str, data_scope: str) -> None:
    """地址与"是否出校"必须一致。标错不会报错，只会让审计永远说不清。"""
    category = llm.classify(base_url)
    if category == "invalid":
        raise HTTPException(400, f"地址无法解析：{base_url}")

    actual = "internal" if category == "internal" else "external"
    if actual != data_scope:
        hint = (
            "该地址是校内地址（私网/localhost/*.edu.cn），data_scope 应填 internal"
            if actual == "internal"
            else "该地址在校园网之外，data_scope 应填 external"
        )
        raise HTTPException(400, f"数据范围与地址不一致：{hint}")


def _key_file(name: str) -> Path:
    """路径规则在 keystore 里只有一份：写密钥（这里）与读密钥（llm）必须一致。"""
    if not NAME_RE.match(name):
        raise HTTPException(400, "端点名称只能包含小写字母、数字、点、下划线、短横线")
    try:
        return keystore.key_path(name)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


def _write_key(name: str, value: str) -> None:
    path = _key_file(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    # 先写临时文件再替换，避免写到一半断电留下空密钥
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, value.encode("utf-8"))
    finally:
        os.close(fd)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        # Windows 上 chmod 语义有限，失败不算致命
        pass


def _read_key(name: str) -> str | None:
    _key_file(name)  # 名称不合法时先报 400
    return keystore.read_key(name)


def _delete_key(name: str) -> None:
    _key_file(name)
    keystore.clear_key(name)


def _audit(actor: Actor, action: str, name: str, note: str) -> None:
    db.query(
        """INSERT INTO audit_log (actor, action, entity, entity_key, detail)
           VALUES (%s, %s, 'model_endpoint', %s, jsonb_build_object('note', %s::text))""",
        (actor.id, action, name, note),
    )


# ---------------------------------------------------------------------------
# 接口
# ---------------------------------------------------------------------------

@router.get("/whoami")
def whoami(actor: Actor = Depends(current_actor)) -> dict:
    """前端据此决定按钮是否可点。真正的拦截仍在服务端。"""
    return {
        "actor": actor.id,
        "roles": sorted(actor.roles),
        "can_view": actor.has("reviewer", "teacher", "admin"),
        "can_edit": actor.has("admin"),
    }


@router.get("/model-endpoints")
def list_endpoints(actor: Actor = Depends(require("reviewer", "teacher", "admin"))) -> dict:
    rows = db.query(f"SELECT {VIEW_COLUMNS} FROM v_model_endpoint_public ORDER BY kind, name")
    return {"items": rows, "count": len(rows)}


@router.post("/model-endpoints", status_code=201)
def create_endpoint(body: EndpointIn, actor: Actor = Depends(require("admin"))) -> dict:
    if not NAME_RE.match(body.name):
        raise HTTPException(400, "端点名称只能包含小写字母、数字、点、下划线、短横线")
    _validate_scope(body.base_url, body.data_scope)
    if body.data_scope == "external" and not (body.notes or "").strip():
        raise HTTPException(400, "外部端点必须填写说明：为什么允许它接收校内内容")
    if body.kind == "embedding" and not body.dim:
        raise HTTPException(400, "向量端点必须填写维度（当前库为 1024）")
    if body.dim and body.dim != 1024:
        raise HTTPException(
            400, f"维度必须是 1024：textbook_chunk.embedding 是 vector(1024)，改了要重建整列"
        )

    try:
        row = db.query_one(
            f"""INSERT INTO model_endpoint
                  (name, kind, base_url, model, data_scope, dim, max_context, notes,
                   enabled, approved_by, approved_at, updated_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now(), %s)
                RETURNING {RETURNING_COLUMNS}""",
            (body.name, body.kind, body.base_url, body.model, body.data_scope,
             body.dim, body.max_context, body.notes, body.enabled,
             actor.id, actor.id),
        )
    except Exception as exc:
        if "model_endpoint_pkey" in str(exc):
            raise HTTPException(409, f"端点名称已存在：{body.name}") from exc
        if "one_enabled" in str(exc):
            raise HTTPException(409, "已有一个启用的同类端点，请先停用原来那个") from exc
        raise

    _audit(actor, "create-model-endpoint", body.name, f"{body.kind} {body.model} {body.base_url}")
    return row or {}


@router.patch("/model-endpoints/{name}")
def update_endpoint(name: str, body: EndpointPatch, actor: Actor = Depends(require("admin"))) -> dict:
    current = db.query_one(
        "SELECT base_url, data_scope, kind, dim FROM model_endpoint WHERE name = %s", (name,)
    )
    if not current:
        raise HTTPException(404, f"端点不存在：{name}")

    base_url = body.base_url or current["base_url"]
    data_scope = body.data_scope or current["data_scope"]
    _validate_scope(base_url, data_scope)

    if body.dim is not None and body.dim != 1024:
        raise HTTPException(400, "维度必须是 1024：textbook_chunk.embedding 是 vector(1024)")

    fields, values = [], []
    for column, value in [
        ("base_url", body.base_url),
        ("model", body.model),
        ("data_scope", body.data_scope),
        ("dim", body.dim),
        ("max_context", body.max_context),
        ("notes", body.notes),
        ("enabled", body.enabled),
    ]:
        if value is not None:
            fields.append(f"{column} = %s")
            values.append(value)
    if not fields:
        raise HTTPException(400, "没有要修改的字段")

    fields += ["updated_by = %s", "updated_at = now()"]
    values += [actor.id, name]

    try:
        row = db.query_one(
            f"UPDATE model_endpoint SET {', '.join(fields)} WHERE name = %s RETURNING {RETURNING_COLUMNS}",
            tuple(values),
        )
    except Exception as exc:
        if "one_enabled" in str(exc):
            raise HTTPException(409, "已有一个启用的同类端点，请先停用原来那个") from exc
        raise

    _audit(actor, "update-model-endpoint", name, f"字段：{', '.join(f.split(' =')[0] for f in fields)}")
    return row or {}


@router.put("/model-endpoints/{name}/key")
def set_key(name: str, body: KeyIn, actor: Actor = Depends(require("admin"))) -> dict:
    if not db.query_one("SELECT 1 AS ok FROM model_endpoint WHERE name = %s", (name,)):
        raise HTTPException(404, f"端点不存在：{name}")

    _write_key(name, body.api_key.strip())
    db.query(
        """UPDATE model_endpoint
           SET api_key_path = %s, api_key_updated_at = now(), updated_by = %s, updated_at = now()
           WHERE name = %s""",
        (f"{name}.key", actor.id, name),
    )
    # 审计只记"配了密钥"，绝不记密钥内容
    _audit(actor, "set-model-key", name, "密钥已更新（内容不入库、不入日志）")
    return {"name": name, "key_configured": True}


@router.delete("/model-endpoints/{name}/key")
def clear_key(name: str, actor: Actor = Depends(require("admin"))) -> dict:
    _delete_key(name)
    db.query(
        """UPDATE model_endpoint
           SET api_key_path = NULL, api_key_updated_at = now(), updated_by = %s, updated_at = now()
           WHERE name = %s""",
        (actor.id, name),
    )
    _audit(actor, "clear-model-key", name, "密钥已删除")
    return {"name": name, "key_configured": False}


@router.post("/model-endpoints/{name}/test")
def test_endpoint(name: str, actor: Actor = Depends(require("admin"))) -> dict:
    row = db.query_one("SELECT * FROM model_endpoint WHERE name = %s", (name,))
    if not row:
        raise HTTPException(404, f"端点不存在：{name}")

    endpoint = llm.Endpoint(
        name=row["name"],
        base_url=row["base_url"],
        model=row["model"],
        kind=row["kind"],
        dim=row["dim"],
    )

    # 自检也要过出校策略：不能借"测试"这个入口绕过提醒
    try:
        llm.assert_egress_allowed(endpoint)
    except llm.EgressViolation as exc:
        raise HTTPException(400, str(exc)) from exc

    key = _read_key(name) or os.environ.get(
        "LLM_API_KEY" if row["kind"] == "chat" else "EMBEDDING_API_KEY"
    ) or os.environ.get("MODEL_API_KEY")

    # 没有密钥就不要发请求。发出去的结果是供应商回 401「missing or invalid」，
    # 那句话听起来像"密钥填错了"，实际是"根本没带密钥"——排查方向会被带偏。
    if not key:
        raise HTTPException(
            400,
            f"端点 {name} 还没有可用密钥。先点「设置密钥」填 API Key"
            f"（会存到 secrets/{name}.key）；"
            "或者在 .env 里配 LLM_API_KEY / MODEL_API_KEY 并重启服务。",
        )

    ok, note = False, ""
    try:
        if row["kind"] == "chat":
            result = llm.post_json(
                f"{row['base_url'].rstrip('/')}/chat/completions",
                {"model": row["model"], "messages": [{"role": "user", "content": "ping"}], "max_tokens": 8},
                api_key=key,
                timeout=20,
            )
            reply = result["choices"][0]["message"]["content"][:40]
            ok, note = True, f"返回：{reply}"
        else:
            result = llm.post_json(
                f"{row['base_url'].rstrip('/')}/embeddings",
                {"model": row["model"], "input": ["ping"]},
                api_key=key,
                timeout=20,
            )
            dim = len(result["data"][0]["embedding"])
            ok = dim == (row["dim"] or 1024)
            note = f"向量维度：{dim}" + ("" if ok else f"（与配置的 {row['dim']} 不符）")
    except Exception as exc:
        note = str(exc)[:300]

    db.query(
        """UPDATE model_endpoint
           SET last_test_at = now(), last_test_ok = %s, last_test_note = %s, updated_at = now()
           WHERE name = %s""",
        (ok, note, name),
    )
    return {"name": name, "ok": ok, "note": note}


# ---------------------------------------------------------------------------
# 平台开关
# ---------------------------------------------------------------------------

@router.get("/settings/qa-fallback")
def get_qa_fallback(actor: Actor = Depends(require("admin"))) -> dict:
    """读「知识库没依据时用通用模型回答」这个开关的当前状态。

    返回里带 source（env/db/default）与 forced：前端要能说清
    "为什么我关不掉"——否则运维在 .env 里改了一票否决，页面上的开关
    会看起来像坏了。
    """
    state = settings.qa_fallback()
    state["note"] = (
        "已被服务器 .env 的 QA_FALLBACK=off 强制关闭，页面上改不动"
        if state["forced"] else ""
    )
    return state


@router.put("/settings/qa-fallback")
def set_qa_fallback(body: QaFallbackIn, actor: Actor = Depends(require("admin"))) -> dict:
    if settings.qa_fallback()["forced"]:
        raise HTTPException(
            409,
            "服务器 .env 里设了 QA_FALLBACK=off，这个开关被强制关闭。"
            "要在这里控制，先删掉 .env 里那一行并重启服务。",
        )
    settings.set_value("qa_fallback", body.enabled, actor.id,
                       note="知识库无依据时使用通用模型回答" if body.enabled
                       else "严格模式：无依据即拒答")
    return get_qa_fallback(actor)
