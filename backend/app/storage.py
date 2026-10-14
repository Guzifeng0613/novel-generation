"""Immutable, content-addressed artifact storage."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import UUID

from .schemas import ArtifactManifestV1


class ArtifactStorageError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class StoredObject:
    sha256: str
    relative_path: str
    absolute_path: Path
    byte_size: int
    media_type: str


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _safe_suffix(suffix: str) -> str:
    if not suffix:
        return ".bin"
    if not suffix.startswith("."):
        suffix = f".{suffix}"
    suffix = suffix.lower()
    if not re.fullmatch(r"\.[a-z0-9][a-z0-9._-]{0,15}", suffix):
        raise ValueError("unsafe artifact suffix")
    return suffix


def _safe_relative_path(path: str | Path) -> PurePosixPath:
    normalized = str(path).replace("\\", "/")
    candidate = PurePosixPath(normalized)
    if candidate.is_absolute() or not candidate.parts:
        raise ArtifactStorageError("path must be relative")
    if any(part in {"", ".", ".."} for part in candidate.parts):
        raise ArtifactStorageError("path contains an unsafe segment")
    if re.match(r"^[A-Za-z]:", normalized):
        raise ArtifactStorageError("drive-qualified paths are not allowed")
    return candidate


def _fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def _atomic_replace_bytes(destination: Path, data: bytes) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", delete=False, dir=destination.parent, prefix=".tmp-"
        ) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
            temporary_path = Path(handle.name)
        os.replace(temporary_path, destination)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


class ArtifactStore:
    """Store immutable objects by SHA-256 and mutable heads as small pointers."""

    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()
        self.objects_dir = self.root / "objects"
        self.pointers_dir = self.root / "pointers"
        self.objects_dir.mkdir(parents=True, exist_ok=True)
        self.pointers_dir.mkdir(parents=True, exist_ok=True)

    def object_path(self, digest: str, suffix: str = ".bin") -> Path:
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("invalid SHA-256 digest")
        safe_suffix = _safe_suffix(suffix)
        return self.objects_dir / digest[:2] / digest[2:4] / f"{digest}{safe_suffix}"

    def resolve(self, relative_path: str | Path) -> Path:
        relative = _safe_relative_path(relative_path)
        candidate = (self.root.joinpath(*relative.parts)).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise ArtifactStorageError("artifact path escapes storage root") from exc
        return candidate

    def put_bytes(
        self,
        data: bytes,
        *,
        suffix: str = ".bin",
        media_type: str = "application/octet-stream",
    ) -> StoredObject:
        digest = sha256_bytes(data)
        destination = self.object_path(digest, suffix)
        destination.parent.mkdir(parents=True, exist_ok=True)

        if destination.exists():
            if destination.stat().st_size != len(data) or sha256_bytes(destination.read_bytes()) != digest:
                raise ArtifactStorageError(f"content-address collision at {destination}")
        else:
            temporary_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="wb", delete=False, dir=destination.parent, prefix=".object-"
                ) as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                    temporary_path = Path(handle.name)
                try:
                    # Hard-link creation is atomic and never overwrites a winner
                    # from a concurrent writer.
                    os.link(temporary_path, destination)
                except FileExistsError:
                    pass
                except OSError:
                    # Some Windows/network filesystems disallow hard links. The
                    # digest makes an atomic replace safe even under a race.
                    if not destination.exists():
                        os.replace(temporary_path, destination)
                        temporary_path = None
            finally:
                if temporary_path is not None:
                    temporary_path.unlink(missing_ok=True)

        relative_path = destination.relative_to(self.root).as_posix()
        return StoredObject(
            sha256=digest,
            relative_path=relative_path,
            absolute_path=destination,
            byte_size=len(data),
            media_type=media_type,
        )

    def put_text(
        self,
        text: str,
        *,
        suffix: str = ".txt",
        media_type: str = "text/plain; charset=utf-8",
    ) -> StoredObject:
        return self.put_bytes(text.encode("utf-8"), suffix=suffix, media_type=media_type)

    def put_file(
        self,
        source: str | Path,
        *,
        suffix: str | None = None,
        media_type: str = "application/octet-stream",
    ) -> StoredObject:
        """Stream a file into content-addressed storage without loading it in memory."""

        source_path = Path(source).expanduser().resolve()
        if not source_path.is_file():
            raise ArtifactStorageError(f"source file does not exist: {source_path}")
        safe_suffix = _safe_suffix(suffix or source_path.suffix or ".bin")
        digest = sha256_file(source_path)
        byte_size = source_path.stat().st_size
        destination = self.object_path(digest, safe_suffix)
        destination.parent.mkdir(parents=True, exist_ok=True)

        if destination.exists():
            if destination.stat().st_size != byte_size or sha256_file(destination) != digest:
                raise ArtifactStorageError(f"content-address collision at {destination}")
        else:
            temporary_path: Path | None = None
            try:
                with (
                    tempfile.NamedTemporaryFile(
                        mode="wb", delete=False, dir=destination.parent, prefix=".object-"
                    ) as target,
                    source_path.open("rb") as source_handle,
                ):
                    temporary_path = Path(target.name)
                    shutil.copyfileobj(source_handle, target, length=1024 * 1024)
                    target.flush()
                    os.fsync(target.fileno())
                try:
                    os.link(temporary_path, destination)
                except FileExistsError:
                    pass
                except OSError:
                    if not destination.exists():
                        os.replace(temporary_path, destination)
                        temporary_path = None
            finally:
                if temporary_path is not None:
                    temporary_path.unlink(missing_ok=True)

        return StoredObject(
            sha256=digest,
            relative_path=destination.relative_to(self.root).as_posix(),
            absolute_path=destination,
            byte_size=byte_size,
            media_type=media_type,
        )

    def put_json(self, value: Any, *, suffix: str = ".json") -> StoredObject:
        return self.put_bytes(
            canonical_json_bytes(value), suffix=suffix, media_type="application/json; charset=utf-8"
        )

    def read_bytes(self, stored: StoredObject | str | Path, *, verify: bool = True) -> bytes:
        if isinstance(stored, StoredObject):
            path = stored.absolute_path
            expected_digest = stored.sha256
        else:
            path = self.resolve(stored)
            match = re.match(r"^([0-9a-f]{64})(?:\..*)?$", path.name)
            expected_digest = match.group(1) if match else None
        data = path.read_bytes()
        if verify and expected_digest and sha256_bytes(data) != expected_digest:
            raise ArtifactStorageError(f"artifact integrity check failed: {path}")
        return data

    def read_text(self, stored: StoredObject | str | Path, *, verify: bool = True) -> str:
        return self.read_bytes(stored, verify=verify).decode("utf-8")

    def materialize(self, stored: StoredObject, relative_destination: str | Path) -> Path:
        """Atomically create/rebuild a human-readable view of an object."""

        destination = self.resolve(relative_destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(delete=False, dir=destination.parent, prefix=".view-") as handle:
                temporary_path = Path(handle.name)
            shutil.copyfile(stored.absolute_path, temporary_path)
            _fsync_file(temporary_path)
            os.replace(temporary_path, destination)
            temporary_path = None
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
        return destination

    def write_pointer(self, name: str | Path, value: dict[str, Any]) -> Path:
        """Atomically promote a branch/run head after all validation passes."""

        relative = _safe_relative_path(name)
        if relative.suffix != ".json":
            relative = PurePosixPath(f"{relative.as_posix()}.json")
        destination = self.pointers_dir.joinpath(*relative.parts)
        _atomic_replace_bytes(destination, canonical_json_bytes(value))
        return destination

    def read_pointer(self, name: str | Path) -> dict[str, Any]:
        relative = _safe_relative_path(name)
        if relative.suffix != ".json":
            relative = PurePosixPath(f"{relative.as_posix()}.json")
        value = json.loads(self.pointers_dir.joinpath(*relative.parts).read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ArtifactStorageError("pointer must contain a JSON object")
        return value

    def build_manifest(
        self,
        stored: StoredObject,
        *,
        project_id: UUID,
        kind: str,
        run_id: UUID | None = None,
        task_id: UUID | None = None,
        version: int = 1,
        source_artifact_ids: list[UUID] | None = None,
        generation_config: dict[str, Any] | None = None,
    ) -> ArtifactManifestV1:
        return ArtifactManifestV1(
            project_id=project_id,
            run_id=run_id,
            task_id=task_id,
            kind=kind,
            version=version,
            relative_path=stored.relative_path,
            sha256=stored.sha256,
            media_type=stored.media_type,
            byte_size=stored.byte_size,
            source_artifact_ids=source_artifact_ids or [],
            generation_config=generation_config or {},
        )
