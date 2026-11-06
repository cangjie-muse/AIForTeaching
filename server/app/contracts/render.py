"""打印渲染契约：`assessment.paper_render` 的一行 + 渲染请求/响应形状。

规格 §10.10：`paper_render` 只保存组卷领域关系，文件本身在
`agent.artifact_version`。这里把两者之间的桥写成一个不可变模型：

  * `render_kind`：student / answer / rubric，三者分别注册 artifact；
  * `paper_manifest_hash`：这份渲染对应哪一版卷面。草稿一改，manifest 就变，
    旧预览不再是"当前可用预览"（任务 7 的提交门禁按它筛）；
  * `security_manifest_hash`：渲染输入的哈希。学生卷的输入只有白名单字段，
    所以这个哈希里不可能出现答案——它同时是"这份学生卷到底看到了什么"的证据；
  * `template_version` / `renderer_version` / `font_manifest_hash`：版式与字体
    变化会产生新 render，但不必重审数学内容（规格 §13.1）；
  * `output_hash`：文件字节的 SHA-256，与 `artifact_version.output_hash` 相同。

本文件只有形状，没有读写实现；读写见 `server/app/rendering.py`。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field

from .common import ContractModel, RenderKind


class PaperRenderRecord(ContractModel):
    """`assessment.paper_render` 的一行（含 0027 的全部 NOT NULL 列）。"""

    id: UUID
    paper_version_id: UUID
    render_kind: RenderKind
    paper_manifest_hash: str = Field(min_length=64, max_length=64)
    template_version: str = Field(min_length=1, max_length=120)
    language_code: str = Field(min_length=1, max_length=32)
    artifact_version_id: UUID
    renderer_version: str = Field(min_length=1, max_length=120)
    font_manifest_hash: str = Field(min_length=64, max_length=64)
    security_manifest_hash: str = Field(min_length=64, max_length=64)
    output_hash: str = Field(min_length=64, max_length=64)
    status: Literal["ready", "failed", "blocked"] = "ready"
    created_by: UUID
    created_at: datetime


class RenderRequest(ContractModel):
    """`POST /paper-versions/{id}/renders` 的请求体：一次请求只产出一个卷种。"""

    render_kind: RenderKind


class RenderKindStatus(ContractModel):
    """某一卷种当前 manifest 下的可用渲染状态（供教师工作台展示）。"""

    render_kind: RenderKind
    ready: bool
    render_id: UUID | None = None
    artifact_version_id: UUID | None = None
    output_hash: str | None = Field(default=None, min_length=64, max_length=64)
    stale: bool = False
