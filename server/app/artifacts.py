"""通用 Artifact 边界：注册、确定性寻址、授权打开、访问审计。

阶段 A 需要给三类打印卷（学生卷/答案卷/量规卷）一个统一的文件凭证，`paper_render`
只保存组卷关系，文件本身落在 `agent.artifact_version`（规格 §10.10、§10.12）。

本模块固定的是**语义**（权限动作、内容哈希、不可猜测寻址、访问审计），并通过两个
端口接持久化：

  * `ObjectStore`：`MemoryObjectStore`（测试）与 `LocalObjectStore`（本地磁盘）。
    本地实现落在仓库既有的 `UPLOAD_DIR` 约定下（默认 `uploads/artifacts/`），
    不新开顶层产物目录；写入是内容寻址 + 同目录临时文件 + `os.replace` 原子替换。
    `storage_uri` 保持逻辑地址 `s3://<bucket>/<media_kind>/<sha256>`：S3 适配器
    直接用它，本地适配器按 `<bucket>` 对应的根目录解析同一段 key，因此数据库里
    不会出现本机绝对路径，也不会出现可猜测的卷号/题号。
  * `ArtifactRepository`：`InMemoryArtifactRepository`（测试）与
    `rendering.PostgresArtifactRepository`（写 `agent.artifact*`，与
    `assessment.paper_render` 共用 `db.transaction(assessment_service=True)`）。

生产授权适配器（接前置身份 PDP、按 offering / Release Context 判定 class_shared
与 course_published）仍未实现，这里只有测试用的 `StaticAuthorizer`；任务 6 的
`rendering.PaperRenderAuthorizer` 只做"教师 + 课程范围"这一层。

学生卷渲染输入在数据层排除答案字段这件事由 `app.contracts.student` 保证，不在这里做。
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Callable, Protocol
from uuid import UUID, uuid4

from .contracts import ArtifactAccessEvent, ArtifactMetadata, ArtifactVersion

BUCKET = "private-artifacts"

# 仓库既有的产物约定：UPLOAD_DIR（默认 <repo>/uploads）下按用途分目录，
# 例如 uploads/courseware/*.pptx。打印卷落 uploads/artifacts/<media_kind>/<sha256>，
# 不再新开第五个顶层目录（course-packs / uploads / data / courseware 已各司其职）。
ROOT = Path(__file__).resolve().parents[2]


def default_artifact_root() -> Path:
    """本地产物根目录：与 courseware.py / generation.py 共用 UPLOAD_DIR 环境变量。"""
    return Path(os.environ.get("UPLOAD_DIR", ROOT / "uploads")) / "artifacts"


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


class LocalObjectStore:
    """本地文件系统对象存储：内容寻址 + 原子写入。

    为什么不是直接 `open(path, "wb").write(body)`：
      * 进程在写一半时被杀（部署重启、OOM、Ctrl+C）会留下半截文件，而它的名字
        是内容哈希——下次读会得到"哈希对但内容坏"的卷子，比缺文件更难查；
      * 两个 worker 同时注册同一份内容（重试/并发）会交错写同一个文件。

    做法：先写到同目录的 `.tmp-*.part`，`flush` + `fsync` 后 `os.replace` 到目标。
    `os.replace` 在同一个文件系统内是原子的，读方要么看到旧文件，要么看到完整新文件。
    内容寻址保证同一个 key 的字节永远相同，所以后写覆盖前写也不会产生"两个版本"。

    key 只接受相对 POSIX 路径（`<media_kind>/<sha256>`）。任何 `..`、绝对路径、
    盘符或反斜杠都当场拒绝：对象 key 一旦能穿越目录，删除/覆盖的范围就不再受控。
    """

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root) if root is not None else default_artifact_root()

    def _resolve(self, key: str) -> Path:
        if not isinstance(key, str) or not key.strip():
            raise ValueError("对象 key 不能为空")
        if "\x00" in key or "\\" in key or ":" in key:
            raise ValueError(f"对象 key 含非法字符：{key!r}")
        pure = PurePosixPath(key)
        if pure.is_absolute() or any(part in ("", ".", "..") for part in pure.parts):
            raise ValueError(f"对象 key 必须是相对路径且不含 ..：{key!r}")
        root = self.root.resolve()
        target = (root / pure).resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"对象 key 越界：{key!r}") from exc
        return target

    def path_for(self, key: str) -> Path:
        """返回 key 对应的本地路径（不保证文件存在）。"""
        return self._resolve(key)

    def put(self, key: str, body: bytes) -> None:
        if not isinstance(body, (bytes, bytearray, memoryview)):
            raise TypeError(f"对象内容必须是 bytes，收到 {type(body).__name__}")
        target = self._resolve(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            dir=str(target.parent), prefix=".tmp-", suffix=".part"
        )
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(bytes(body))
                handle.flush()
                # fsync 后再 rename：否则断电后可能 rename 成功但内容是空的
                os.fsync(handle.fileno())
            os.replace(temp_path, target)
            temp_path = None  # type: ignore[assignment]
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink()
                except FileNotFoundError:
                    pass

    def get(self, key: str) -> bytes:
        path = self._resolve(key)
        try:
            return path.read_bytes()
        except FileNotFoundError:
            raise KeyError(key) from None

    def exists(self, key: str) -> bool:
        return self._resolve(key).is_file()


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
        # 并发注册同一 (output_hash, media_kind, created_by) 时，唯一键会让其中一个
        # INSERT 变成 no-op。以数据库里真正存在的版本为准，避免返回一个只存在于
        # 内存、id 与 paper_render.artifact_version_id 对不上的"幽灵版本"。
        persisted = self.repository.find_version(digest, metadata.media_kind, actor_id)
        return persisted or version

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
