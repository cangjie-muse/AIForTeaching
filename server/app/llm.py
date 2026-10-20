"""模型访问层：把"数据不出校"变成代码里的硬约束，而不是口头约定。

为什么要有这一层：
  学院的要求是数据不出校，但校外可以访问网站。这两件事并不矛盾——
  矛盾的是"随手调用外部 API"。RAG 每次问答都要把教材片段塞进提示词，
  只要 base_url 指向校外，数据就已经出去了，而且没有任何人会发现。

所以判断依据不是"provider 叫什么名字"，而是 **base_url 的主机名**：
  localhost / 127.0.0.1 / 10.x / 172.16-31.x / 192.168.x / *.edu.cn / *.local  → 校内
  其他                                                                        → 校外
校外一律拒绝，除非显式设置 ALLOW_EXTERNAL_MODELS=true（开发调试用，生产禁止）。

未知情况一律拒绝（fail closed），不做"猜"。
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

# 校内域名后缀。学校自己的域名可加进来，如 ".whu.edu.cn"
INTERNAL_SUFFIXES = (".edu.cn", ".local", ".internal", ".lan")

# 开发期允许调用外部模型时置 true。生产环境必须为 false。
ALLOW_EXTERNAL = os.environ.get("ALLOW_EXTERNAL_MODELS", "false").lower() in ("1", "true", "yes")


class EgressViolation(RuntimeError):
    """试图把校内数据发到校外。"""


@dataclass(frozen=True)
class Endpoint:
    name: str           # campus-gateway / local-vllm / bge-m3 ...
    base_url: str       # 必须含 scheme，如 http://10.0.0.8:8000/v1
    model: str
    kind: str           # chat | embedding
    dim: int | None = None   # embedding 维度


def is_internal_url(base_url: str) -> bool:
    """按主机名判断是否校内。无法判断时返回 False（fail closed）。"""
    try:
        host = urllib.parse.urlparse(base_url).hostname
    except ValueError:
        return False
    if not host:
        return False

    host = host.lower().rstrip(".")
    if host in ("localhost", "127.0.0.1", "::1"):
        return True
    if host.endswith(INTERNAL_SUFFIXES):
        return True

    # 私网与链路本地地址
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local


def assert_egress_allowed(endpoint: Endpoint) -> None:
    """校外端点一律拒绝，除非显式放行。"""
    if is_internal_url(endpoint.base_url):
        return
    if ALLOW_EXTERNAL:
        return
    raise EgressViolation(
        f"拒绝调用校外模型：{endpoint.name} → {endpoint.base_url}\n"
        f"学院要求数据不出校。RAG 会把教材片段与题目内容发给该地址。\n"
        f"如果这是校内网关，请确认域名在 INTERNAL_SUFFIXES 里；\n"
        f"如果确实只是开发调试，显式设置 ALLOW_EXTERNAL_MODELS=true。"
    )


def chat_endpoint() -> Endpoint:
    return Endpoint(
        name=os.environ.get("LLM_NAME", "campus-gateway"),
        base_url=os.environ.get("LLM_BASE_URL", "http://127.0.0.1:8001/v1"),
        model=os.environ.get("LLM_MODEL", "qwen2.5-14b-instruct"),
        kind="chat",
    )


def embedding_endpoint() -> Endpoint:
    return Endpoint(
        name=os.environ.get("EMBEDDING_NAME", "local-bge-m3"),
        base_url=os.environ.get("EMBEDDING_BASE_URL", "http://127.0.0.1:8002/v1"),
        model=os.environ.get("EMBEDDING_MODEL", "bge-m3"),
        kind="embedding",
        dim=int(os.environ.get("EMBEDDING_DIM", "1024")),
    )


def build_chat_request(endpoint: Endpoint, messages: list[dict], temperature: float = 0.2) -> dict:
    """构造 OpenAI 兼容请求体。国产模型基本都兼容这个格式。"""
    return {
        "model": endpoint.model,
        "messages": messages,
        "temperature": temperature,
        "stream": False,
    }


def post_json(url: str, payload: dict, timeout: int = 120) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"模型返回 {exc.code}：{body}") from exc


def chat(messages: list[dict], temperature: float = 0.2) -> dict:
    """同步对话调用。调用前先过数据出校检查。"""
    endpoint = chat_endpoint()
    assert_egress_allowed(endpoint)
    payload = build_chat_request(endpoint, messages, temperature)
    result = post_json(f"{endpoint.base_url.rstrip('/')}/chat/completions", payload)
    return {
        "provider": endpoint.name,
        "model": endpoint.model,
        "internal": is_internal_url(endpoint.base_url),
        "content": result["choices"][0]["message"]["content"],
        "usage": result.get("usage", {}),
    }


def embed(texts: list[str]) -> list[list[float]]:
    """批量向量化。同样先过数据出校检查。"""
    endpoint = embedding_endpoint()
    assert_egress_allowed(endpoint)
    payload = {"model": endpoint.model, "input": texts}
    result = post_json(f"{endpoint.base_url.rstrip('/')}/embeddings", payload, timeout=300)
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
        "chat": {
            "name": chat_ep.name,
            "model": chat_ep.model,
            "internal": is_internal_url(chat_ep.base_url),
        },
        "embedding": {
            "name": emb_ep.name,
            "model": emb_ep.model,
            "dim": emb_ep.dim,
            "internal": is_internal_url(emb_ep.base_url),
        },
        "allow_external": ALLOW_EXTERNAL,
    }
