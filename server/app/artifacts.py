"""通用 Artifact 边界：注册、确定性寻址、授权打开、访问审计。

阶段 A 需要给三类打印卷（学生卷/答案卷/量规卷）一个统一的文件凭证，`paper_render`
只保存组卷关系，文件本身落在 `agent.artifact_version`（规格 §10.10、§10.12）。

**依赖未就绪的部分（本次只建表与契约，不实现读写）：**
  * 对象存储适配器（S3 / 本地磁盘）尚未接线，`MemoryObjectStore` 只用于测试与端口演示；
  * 数据库仓储（写 `agent.artifact` / `artifact_version` / `artifact_access_event`）
    尚未实现，`InMemoryArtifactRepository` 也只有内存语义；
  * 生产授权适配器（接前置身份 PDP、按 offering / Release Context 判定 class_shared
    与 course_published）未实现，这里只有测试用的 `StaticAuthorizer`。

也就是说：本模块固定的是**语义**（权限动作、内容哈希、不可猜测寻址、访问审计），
不是持久化。接存储和数据库时替换 `ObjectStore` / `ArtifactRepository` 两个端口即可，
`ArtifactService` 的签名不变。

学生卷渲染输入在数据层排除答案字段这件事由 `app.contracts.student` 保证，不在这里做。
"""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from typing import Callable, Protocol
from uuid import UUID, uuid4

from .contracts import ArtifactAccessEvent, ArtifactMetadata, ArtifactVersion

BUCKET = "private-artifacts"


class ObjectStore(Protocol):
    """对象存储端口：S3 适配器实现同样的 put/get 即可。"""

    def put(self, key: str, body: bytes) -> None: ...

    def get(self, key: str) -> bytes: ...


class MemoryObjectStore:
    """内存对象存储：仅测试/端口演示，进程退出即丢。"""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put(self, key: str, body: bytes) -> None:
        self.objects[key] = body

    def get(self, key: str) -> bytes:
        return self.objects[key]


class Authorizer(Protocol):
    """授权端口：生产实现调用身份 Policy Decision Point。"""

    def require(self, actor_id: UUID, action: str, **scope: object) -> None: ...

    def is_human(self, actor_id: UUID) -> bool: ...


class StaticAuthorizer:
    """静态授权表：只给测试用，不接真实身份系统。

    权限动作：`artifact.write`（注册）、`artifact.read`（打开/下载）。
    生产适配器还要按 `visibility` 核对 offering 与 Release Context，这里不做。
    """

    def __init__(self, grants: dict[UUID, set[str]]) -> None:
        self.grants = grants
        self.service_accounts: set[UUID] = set()

    def require(self, actor_id: UUID, action: str, **scope: object) -> None:
        if action not in self.grants.get(actor_id, set()):
            raise PermissionError(action)

    def is_human(self, actor_id: UUID) -> bool:
        return actor_id not in self.service_accounts


class ArtifactRepository(Protocol):
    """元数据仓储端口：生产实现写 agent.artifact_version 与访问事件表。"""

    def find_version(self, output_hash: str, media_kind: str,
                     created_by: UUID) -> ArtifactVersion | None: ...

    def get_version(self, artifact_version_id: UUID) -> ArtifactVersion: ...

    def save_version(self, version: ArtifactVersion) -> None: ...

    def record_access(self, event: ArtifactAccessEvent) -> None: ...


class InMemoryArtifactRepository:
    """内存仓储：与 `UNIQUE(output_hash, media_kind, created_by)` 语义一致。"""

    def __init__(self) -> None:
        self.versions: dict[UUID, ArtifactVersion] = {}
        self.by_output: dict[tuple[str, str, UUID], ArtifactVersion] = {}
        self.access_events: list[ArtifactAccessEvent] = []

    def find_version(self, output_hash: str, media_kind: str,
                     created_by: UUID) -> ArtifactVersion | None:
        return self.by_output.get((output_hash, media_kind, created_by))

    def get_version(self, artifact_version_id: UUID) -> ArtifactVersion:
        try:
            return self.versions[artifact_version_id]
        except KeyError:
            raise KeyError(f"artifact version not found: {artifact_version_id}") from None

    def save_version(self, version: ArtifactVersion) -> None:
        self.versions[version.id] = version
        self.by_output[(version.output_hash, version.media_kind, version.created_by)] = version

    def record_access(self, event: ArtifactAccessEvent) -> None:
        self.access_events.append(event)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ArtifactService:
    """注册与打开 Artifact 的应用服务。

    设计要点：
      * 内容寻址：`<media_kind>/<sha256(body)>`，同样的字节永远落到同一个 key，
        但地址来自哈希，不能由题号或卷号猜出来；
      * 重复注册复用同一版本（对应数据库 `UNIQUE(output_hash, media_kind, created_by)`），
        重试不会产生第二份文件；
      * 打开必须同时具备 `artifact.read`，并留下一条访问审计；
      * 存储地址是 `s3://<bucket>/<key>`，不含签名参数——签名 URL 由下载接口临时签发，
        不能写进数据库。
    """

    def __init__(
        self,
        store: ObjectStore,
        authorizer: Authorizer,
        repository: ArtifactRepository | None = None,
        bucket: str = BUCKET,
        id_factory: Callable[[], UUID] = uuid4,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.store = store
        self.auth = authorizer
        self.repository: ArtifactRepository = repository or InMemoryArtifactRepository()
        self.bucket = bucket
        self._new_id = id_factory
        self._now = clock

    @staticmethod
    def object_key(media_kind: str, output_hash: str) -> str:
        return f"{media_kind}/{output_hash}"

    def register(self, actor_id: UUID, body: bytes, metadata: ArtifactMetadata) -> ArtifactVersion:
        self.auth.require(
            actor_id, "artifact.write", release_context_id=metadata.release_context_id
        )
        digest = sha256(body).hexdigest()
        existing = self.repository.find_version(digest, metadata.media_kind, actor_id)
        if existing is not None:
            return existing

        key = self.object_key(metadata.media_kind, digest)
        self.store.put(key, body)
        version = ArtifactVersion(
            id=self._new_id(),
            artifact_id=self._new_id(),
            version_no=1,
            release_context_id=metadata.release_context_id,
            visibility=metadata.visibility,
            contains_solution=metadata.contains_solution,
            contains_answer_test=metadata.contains_answer_test,
            media_kind=metadata.media_kind,
            mime_type=metadata.mime_type,
            storage_uri=f"s3://{self.bucket}/{key}",
            byte_size=len(body),
            output_hash=digest,
            created_by=actor_id,
            created_at=self._now(),
        )
        self.repository.save_version(version)
        return version

    def open(self, actor_id: UUID, artifact_version_id: UUID) -> bytes:
        self.auth.require(actor_id, "artifact.read")
        version = self.repository.get_version(artifact_version_id)
        body = self.store.get(self.object_key(version.media_kind, version.output_hash))
        self.repository.record_access(
            ArtifactAccessEvent(
                id=self._new_id(),
                artifact_version_id=version.id,
                actor_id=actor_id,
                action="open",
                occurred_at=self._now(),
            )
        )
        return body
