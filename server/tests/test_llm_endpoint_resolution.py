#!/usr/bin/env python
"""端点解析：库里启用的端点优先，读不到再回落 .env（不连库）。

这块的失败模式都很安静：配置页填完了却仍在用 .env 的模型，界面上没有任何提示，
只有账单和回答风格会露馅。所以把四条路径都钉住：
有库用库、没库用 env、库报错用 env、密钥文件优先于环境变量。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(root / "server"))

from app import db, keystore, llm  # noqa: E402

checks = 0
failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    global checks
    checks += 1
    print(f"  {'✓' if ok else '✗'} {name}{' — ' + detail if detail else ''}")
    if not ok:
        failures.append(name)


real_query_one = db.query_one
real_secrets = keystore.SECRETS_DIR
saved_env = {k: os.environ.get(k) for k in
             ("LLM_NAME", "LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY",
              "EMBEDDING_API_KEY", "MODEL_API_KEY", "EMBEDDING_DIM")}

try:
    print("库里有一个启用中的 chat 端点：用它，不看 env")
    def row_ok(sql, params=()):
        return {"name": "deepseek-prod", "base_url": "https://api.deepseek.com/v1",
                "model": "deepseek-chat", "dim": None}

    db.query_one = row_ok
    os.environ["LLM_BASE_URL"] = "https://env.example.com"
    os.environ["LLM_MODEL"] = "env-model"
    endpoint = llm.chat_endpoint()
    check("名称来自库", endpoint.name == "deepseek-prod", endpoint.name)
    check("地址来自库", endpoint.base_url == "https://api.deepseek.com/v1")
    check("模型来自库", endpoint.model == "deepseek-chat")

    print("\n库里没有端点：回落 .env")
    db.query_one = lambda sql, params=(): None
    endpoint = llm.chat_endpoint()
    check("用 env 的地址", endpoint.base_url == "https://env.example.com", endpoint.base_url)
    check("用 env 的模型", endpoint.model == "env-model")

    print("\n库连不上（raised）：回落 .env，且不把异常抛给调用方")
    def boom(sql, params=()):
        raise RuntimeError("connection closed")

    db.query_one = boom
    try:
        endpoint = llm.chat_endpoint()
        check("没有抛异常", True)
        check("回落到了 env", endpoint.model == "env-model", endpoint.model)
    except Exception as error:
        check("没有抛异常", False, repr(error))

    print("\nLLM_NAME 指定了哪个端点就优先挑哪个")
    seen = []

    def capture(sql, params=()):
        seen.append(params)
        return {"name": params[1], "base_url": "https://api.moonshot.cn/v1",
                "model": "kimi-k2", "dim": None} if len(params) > 1 else None

    db.query_one = capture
    os.environ["LLM_NAME"] = "kimi"
    llm.chat_endpoint()
    check("SQL 里带上了指定名称", ("chat", "kimi") in seen, str(seen[:1]))

    print("\nembedding 的维度从库里取")
    db.query_one = lambda sql, params=(): {"name": "bailian", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                                           "model": "qwen3.7-text-embedding", "dim": 1024} if params[0] == "embedding" else None
    os.environ["EMBEDDING_DIM"] = "512"
    endpoint = llm.embedding_endpoint()
    check("dim 用库里的 1024 而不是 env 的 512", endpoint.dim == 1024, str(endpoint.dim))

    print("\n密钥：secrets/<name>.key 优先于环境变量")
    with tempfile.TemporaryDirectory() as tmp:
        keystore.SECRETS_DIR = Path(tmp).resolve()
        keystore.write_key("deepseek-prod", "sk-from-file")
        os.environ["LLM_API_KEY"] = "sk-from-env"
        db.query_one = row_ok
        endpoint = llm.chat_endpoint()
        check("读到文件里的密钥", llm._api_key(endpoint) == "sk-from-file", str(llm._api_key(endpoint))[:14])

        print("\n没有密钥文件：回落环境变量，再回落 MODEL_API_KEY")
        os.environ["LLM_NAME"] = "nofile"
        db.query_one = lambda sql, params=(): {"name": "nofile", "base_url": "https://api.deepseek.com",
                                               "model": "m", "dim": None}
        endpoint = llm.chat_endpoint()
        check("用 LLM_API_KEY", llm._api_key(endpoint) == "sk-from-env")
        os.environ.pop("LLM_API_KEY", None)
        os.environ["MODEL_API_KEY"] = "sk-generic"
        check("用 MODEL_API_KEY", llm._api_key(endpoint) == "sk-generic")
        os.environ.pop("MODEL_API_KEY", None)
        check("都没有时返回 None（调用方会报'没配密钥'）", llm._api_key(endpoint) is None)
finally:
    db.query_one = real_query_one
    keystore.SECRETS_DIR = real_secrets
    for key, value in saved_env.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value

print("")
if failures:
    print(f"失败 {len(failures)} 项：{', '.join(failures)}")
    sys.exit(1)
print(f"端点解析测试全部通过（{checks} 项）。")
