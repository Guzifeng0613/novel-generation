import json
from uuid import uuid4

import pytest

from backend.app.storage import ArtifactStorageError, ArtifactStore


def test_content_addressing_is_deterministic_and_integrity_checked(tmp_path) -> None:
    store = ArtifactStore(tmp_path)
    first = store.put_text("雪落在旧车站。")
    second = store.put_text("雪落在旧车站。")

    assert first.sha256 == second.sha256
    assert first.relative_path == second.relative_path
    assert first.absolute_path.read_text(encoding="utf-8") == "雪落在旧车站。"

    first.absolute_path.write_text("tampered", encoding="utf-8")
    with pytest.raises(ArtifactStorageError, match="integrity"):
        store.read_bytes(first)


def test_json_is_canonical_and_pointer_promotion_is_atomic(tmp_path) -> None:
    store = ArtifactStore(tmp_path)
    left = store.put_json({"b": 2, "a": "中文"})
    right = store.put_json({"a": "中文", "b": 2})
    assert left.sha256 == right.sha256
    assert json.loads(store.read_text(left)) == {"a": "中文", "b": 2}

    store.write_pointer("branches/main", {"artifact_id": "old"})
    store.write_pointer("branches/main", {"artifact_id": "new"})
    assert store.read_pointer("branches/main") == {"artifact_id": "new"}


def test_manifest_uses_relative_content_path(tmp_path) -> None:
    store = ArtifactStore(tmp_path)
    stored = store.put_text("正文")
    manifest = store.build_manifest(stored, project_id=uuid4(), kind="chapter")
    assert manifest.relative_path.startswith("objects/")
    assert manifest.sha256 == stored.sha256


def test_put_file_streams_into_content_addressed_storage(tmp_path) -> None:
    source = tmp_path / "project.zip"
    source.write_bytes((b"novel-project-bundle" * 131_072) + b"tail")
    store = ArtifactStore(tmp_path / "store")

    first = store.put_file(source, media_type="application/zip")
    second = store.put_file(source, media_type="application/zip")

    assert first.relative_path == second.relative_path
    assert first.byte_size == source.stat().st_size
    assert store.read_bytes(first) == source.read_bytes()


def test_resolve_blocks_path_traversal(tmp_path) -> None:
    store = ArtifactStore(tmp_path)
    with pytest.raises(ArtifactStorageError):
        store.resolve("../escape.txt")
