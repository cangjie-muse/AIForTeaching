"""模型访问层：控制"哪些供应商可以收到校内内容"。

背景变化（2026-10-20）：
    学院确认数据**可以**出校，使用国内大模型 API。所以不再需要"数据不出内网"的
    硬拦截，但**不能退化成"想发就发"**。真正要防的三件事变成：

      1. 接到来路不明的代理/中转站（很多"便宜 API"其实是转发，日志里能看到内容）
      2. 供应商悄悄换域名，代码还按老地址发
      3. 用了哪个模型、花了多少、发了什么，事后说不清

    所以策略是**允许清单（allowlist）**，不是"外网全禁"也不是"外网全通"：
      · 校内地址       → 一律放行
      · 已批准的供应商 → 放行（DeepSeek / Kimi / 阿里百炼 / 智谱）
      · 其他外部地址   → 拒绝，并提示怎么加白名单

    三种模式由 MODEL_EGRESS 控制：
      allowlist（默认）| any（开发临时用）| internal-only（如果将来要求数据不出校）

判断依据是 **base_url 的主机名**，不是 provider 名字——名字可以随便写，域名才是
数据实际发往的地方。
"""

from __future__ import annotations

import ipaddress
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# 策略
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

INTERNAL_SUFFIXES = (".edu.cn", ".local", ".internal", ".lan")

EGRESS_MODE = os.environ.get("MODEL_EGRESS", "allowlist").lower()
VALID_MODES = {"allowlist", "any", "internal-only"}


class MissingKey(RuntimeError):
    """端点没有可用密钥。这属于配置问题，不该发出去再让供应商回 401。"""


class EgressViolation(RuntimeError):
    """目标供应商未被批准接收校内内容。"""


@dataclass(frozen=True)
class Endpoint:
    name: str           # deepseek / kimi / bailian-qwen / campus-gateway
    base_url: str       # 必须含 scheme，如 https://api.deepseek.com
    model: str
    kind: str           # chat | embedding
    dim: int | None = None   # embedding 维度


def _host(base_url: str) -> str:
    try:
        return (urllib.parse.urlparse(base_url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def is_internal_url(base_url: str) -> bool:
    """校内地址：本机、私网、校内域名。无法判断时返回 False。"""
    host = _host(base_url)
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


def is_approved(base_url: str) -> bool:
    """已批准的供应商：域名相同或是其子域。"""
    host = _host(base_url)
    if not host:
        return False
    return any(host == allowed or host.endswith("." + allowed) for allowed in APPROVED_HOSTS)


def classify(base_url: str) -> str:
    """返回 internal / approved / unapproved / invalid。"""
    if not _host(base_url):
        return "invalid"
    if is_internal_url(base_url):
        return "internal"
    if is_approved(base_url):
        return "approved"
    return "unapproved"


def assert_egress_allowed(endpoint: Endpoint) -> None:
    """按当前模式判断是否允许调用。未知情况一律拒绝（fail closed）。

    判断顺序不能随意调换，这里踩过一次：
      internal-only 模式必须挡在 approved 之前，否则"已批准供应商"会先被放行，
      而这个模式的全部意义就是禁止一切外部调用。
    """
    category = classify(endpoint.base_url)

    if category == "internal":
        return

    # 地址无效时任何模式都拒绝：目标不明 = 不知道数据发去哪
    if category == "invalid":
        raise EgressViolation(
            f"端点地址无效，拒绝调用：{endpoint.name} → {endpoint.base_url or '(空地址)'}"
        )

    if EGRESS_MODE == "internal-only":
        raise EgressViolation(
            f"当前模式为 internal-only，拒绝调用外部模型：{endpoint.name} → {endpoint.base_url}"
        )

    if category == "approved":
        return

    if EGRESS_MODE == "any":
        return

    raise EgressViolation(
        f"供应商未批准：{endpoint.name} → {endpoint.base_url}\n"
        f"请求会包含校内教材片段与题目内容，必须先确认该供应商可接收。\n"
        f"确认后加入白名单：MODEL_ALLOWED_HOSTS={','.join(sorted(APPROVED_HOSTS))},<新域名>\n"
        f"临时调试可用 MODEL_EGRESS=any（生产禁止）。"
    )


# ---------------------------------------------------------------------------
# 端点配置（全部来自环境变量，不硬编码密钥）
# ---------------------------------------------------------------------------

def _db_endpoint(kind: str) -> Endpoint | None:
    """库里启用的端点优先。读不到（没库、没配、连接断了）返回 None，由调用方回落 env。

    为什么让库优先：配置页改一次就该生效，否则"前端填完不生效"会让所有人
    回到登服务器改 .env 的老路，而那正是配置页想解决的事。

    同名优先：`LLM_NAME` 指定了哪个端点就挑哪个；否则取最近更新的那个，
    保证"启用两个端点"这种状态下行为可预测，而不是随插入顺序漂移。
    """
    try:
        from . import db as _db
    except Exception:
        return None

    preferred = os.environ.get("LLM_NAME" if kind == "chat" else "EMBEDDING_NAME", "").strip()
    try:
        if preferred:
            row = _db.query_one(
                """SELECT name, base_url, model, dim FROM model_endpoint
                    WHERE kind = %s AND enabled AND name = %s""",
                (kind, preferred))
            if row:
                return _row_to_endpoint(row, kind)
        row = _db.query_one(
            """SELECT name, base_url, model, dim FROM model_endpoint
                WHERE kind = %s AND enabled
                ORDER BY updated_at DESC, name LIMIT 1""",
            (kind,))
    except Exception:
        # 库不可用不该让问答直接挂掉：安静回落 env，调用方仍会按 env 发请求。
        return None
    return _row_to_endpoint(row, kind) if row else None


def _row_to_endpoint(row: dict, kind: str) -> Endpoint:
    return Endpoint(
        name=row["name"],
        base_url=row["base_url"],
        model=row["model"],
        kind=kind,
        dim=row.get("dim"),
    )


def chat_endpoint() -> Endpoint:
    found = _db_endpoint("chat")
    if found:
        return found
    return Endpoint(
        name=os.environ.get("LLM_NAME", "deepseek"),
        base_url=os.environ.get("LLM_BASE_URL", "https://api.deepseek.com"),
        model=os.environ.get("LLM_MODEL", "deepseek-flash"),
        kind="chat",
    )


def embedding_endpoint() -> Endpoint:
    found = _db_endpoint("embedding")
    if found:
        return found
    return Endpoint(
        name=os.environ.get("EMBEDDING_NAME", "bailian-qwen"),
        base_url=os.environ.get(
            "EMBEDDING_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
        ),
        model=os.environ.get("EMBEDDING_MODEL", "qwen3.7-text-embedding"),
        kind="embedding",
        dim=int(os.environ.get("EMBEDDING_DIM", "1024")),
    )


# ---------------------------------------------------------------------------
# 调用
# ---------------------------------------------------------------------------

def build_chat_request(endpoint: Endpoint, messages: list[dict], temperature: float = 0.2) -> dict:
    """构造 OpenAI 兼容请求体。DeepSeek / Kimi / 百炼都兼容这个格式。"""
    return {
        "model": endpoint.model,
        "messages": messages,
        "temperature": temperature,
        "stream": False,
    }


def _api_key(endpoint: Endpoint) -> str | None:
    """按端点取密钥。

    生成与向量通常来自不同厂商（DeepSeek + 百炼），所以密钥分开配；
    只有一把时回落到 MODEL_API_KEY，方便开发期只测一条链路。
    """
    # 配置页写入的密钥在 secrets/<name>.key；没有才回落环境变量。
    # 顺序是"更具体者优先"：端点专属密钥 > 按类型的环境变量 > 通用 MODEL_API_KEY。
    try:
        from . import keystore
        stored = keystore.read_key(endpoint.name)
    except Exception:
        stored = None
    if stored:
        return stored
    specific = "LLM_API_KEY" if endpoint.kind == "chat" else "EMBEDDING_API_KEY"
    return os.environ.get(specific) or os.environ.get("MODEL_API_KEY")


def require_key(endpoint: Endpoint) -> str:
    """发请求前确认密钥存在。

    供应商的 401 会写 "API key ... is missing or invalid"——"missing" 和 "invalid"
    混在一句里，人只会去怀疑密钥填错，实际往往是根本没带上密钥（文件不存在、权限不对）。
    这里提前拦下来，把话说清楚。
    """
    key = _api_key(endpoint)
    if key:
        return key
    raise MissingKey(
        f"端点 {endpoint.name} 没有可用密钥：secrets/{endpoint.name}.key 不存在或读不到，"
        f"环境变量 {('LLM_API_KEY' if endpoint.kind == 'chat' else 'EMBEDDING_API_KEY')}"
        " 与 MODEL_API_KEY 也没有配。先到「模型配置」里点「设置密钥」。"
    )


def post_json(url: str, payload: dict, api_key: str | None = None, timeout: int = 120) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"模型返回 {exc.code}：{body}") from exc


def chat(messages: list[dict], temperature: float = 0.2) -> dict:
    """同步对话调用。调用前先过供应商检查。"""
    endpoint = chat_endpoint()
    assert_egress_allowed(endpoint)
    payload = build_chat_request(endpoint, messages, temperature)
    result = post_json(
        f"{endpoint.base_url.rstrip('/')}/chat/completions",
        payload,
        api_key=require_key(endpoint),
    )
    return {
        "provider": endpoint.name,
        "model": endpoint.model,
        "data_scope": classify(endpoint.base_url),
        "content": result["choices"][0]["message"]["content"],
        "usage": result.get("usage", {}),
    }


def embed(texts: list[str]) -> list[list[float]]:
    """批量向量化。同样先过供应商检查。"""
    endpoint = embedding_endpoint()
    assert_egress_allowed(endpoint)
    payload = {"model": endpoint.model, "input": texts}
    result = post_json(
        f"{endpoint.base_url.rstrip('/')}/embeddings",
        payload,
        api_key=require_key(endpoint),
        timeout=300,
    )
    vectors = [item["embedding"] for item in result["data"]]
    if endpoint.dim:
        for vec in vectors:
            if len(vec) != endpoint.dim:
                raise ValueError(
                    f"向量维度不符：模型返回 {len(vec)}，配置是 {endpoint.dim}。"
                    f"换模型必须重算全库，且先改 vector({endpoint.dim}) 列类型。"
                )
    return vectors


def describe() -> dict[str, Any]:
    """给 /health 用的配置自检，不泄露密钥。"""
    chat_ep = chat_endpoint()
    emb_ep = embedding_endpoint()
    return {
        "egress_mode": EGRESS_MODE,
        "approved_hosts": sorted(APPROVED_HOSTS),
        "chat": {
            "name": chat_ep.name,
            "model": chat_ep.model,
            "data_scope": classify(chat_ep.base_url),
        },
        "embedding": {
            "name": emb_ep.name,
            "model": emb_ep.model,
            "dim": emb_ep.dim,
            "data_scope": classify(emb_ep.base_url),
        },
    }
