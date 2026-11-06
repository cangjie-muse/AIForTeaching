"""通用 Artifact 契约：文件凭证与访问事件。

`agent.artifact_version` 是学生卷/答案卷/量规卷 PDF 的唯一真源，`paper_render`
只保存组卷领域关系（规格 §10.10、§10.12）。这里先固定两个对外形状：

  * `ArtifactMetadata`：注册文件时必须声明的安全元数据（可见性、是否含答案或
    answer_test、媒体类型）。可见性缺省不猜、不公开。
  * `ArtifactVersion`：注册结果，带内容哈希与不可猜测的存储地址；
  * `ArtifactAccessEvent`：每次打开/下载留一条审计，供"谁在什么时候导出了保密卷"
    的回放使用。

注意：本文件只有形状，没有读写实现。对象存储与数据库仓储属于"依赖未就绪"，
见 server/app/artifacts.py 顶部说明。模型统一继承 `common.ContractModel`
（`extra="forbid"`），多传一个字段当场报错。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field

from .common import ArtifactVisibility, ContractModel


class ArtifactMetadata(ContractModel):
    """注册一个 Artifact 版本所需的安全元数据。"""

    release_context_id: UUID
    visibility: ArtifactVisibility
    contains_solution: bool
    contains_answer_test: bool
    mime_type: str = Field(min_length=1, max_length=200)
    media_kind: str = Field(min_length=1, max_length=80)


class ArtifactVersion(ContractModel):
    """一个不可变的 Artifact 版本。`storage_uri` 必须不可预测、不带签名参数。"""

    id: UUID
    artifact_id: UUID
    version_no: int = Field(ge=1)
    release_context_id: UUID
    visibility: ArtifactVisibility
    contains_solution: bool
    contains_answer_test: bool
    media_kind: str
    mime_type: str
    storage_uri: str
    byte_size: int = Field(ge=0)
    output_hash: str = Field(min_length=64, max_length=64)
    created_by: UUID
    created_at: datetime


class ArtifactAccessEvent(ContractModel):
    """打开/下载审计。只记 ID 与动作，不记文件内容。"""

    id: UUID
    artifact_version_id: UUID
    actor_id: UUID
    action: Literal["open", "download"]
    occurred_at: datetime
