"""FastAPI routes for the local editorial workstation."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
import tempfile
import zipfile
from collections.abc import AsyncIterator, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select

from . import models
from .config import save_provider_api_keys
from .continuator import create_continuation_run as create_continuator_run
from .importer import (
    ImportInspection,
    ImportKind,
    ImportValidationError,
    build_story_state_from_summary,
    inspect_import,
)
from .orchestrator import IndeterminateTaskBlocked, OrchestrationError, PlanHashMismatch
from .presenters import ViewBuilder, safe_download_name
from .providers import (
    DEEPSEEK_DEFAULT_MODEL,
    OPENAI_DEFAULT_MODEL,
    DeepSeekChatProvider,
    OpenAIResponsesProvider,
    ProviderName,
)
from .schemas import StoryStateV1, SummaryDimension
from .storage import ArtifactStorageError
from .summarizer import create_summary_run

router = APIRouter(prefix="/api")

TERMINAL_RUN_STATUSES = {"completed", "failed", "cancelled", "canceled", "needs_human"}
ROUTING_PRESETS = {"openai-only", "deepseek-only", "hybrid-quality"}


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProjectCreateInput(StrictInput):
    name: str = Field(min_length=1, max_length=240)
    description: str = Field(default="", max_length=20_000)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        if not (cleaned := value.strip()):
            raise ValueError("项目名称不能为空")
        return cleaned


class AnalysisRunInput(StrictInput):
    routing_preset: str = "hybrid-quality"
    splitter_enabled: bool = True
    max_cost: float | None = Field(default=None, gt=0)
    max_tokens: int | None = Field(default=None, gt=0)
    max_calls: int | None = Field(default=None, ge=1, le=10_000)
    max_wall_time: int | None = Field(default=None, ge=60, le=604_800)

    @field_validator("routing_preset")
    @classmethod
    def known_preset(cls, value: str) -> str:
        if value not in ROUTING_PRESETS:
            raise ValueError("未知模型路由预设")
        return value


class ContinuationRunInput(AnalysisRunInput):
    target_chars: int = Field(default=40_000, ge=4_000, le=150_000)
    user_requirements: str = Field(default="", max_length=100_000)
    branch_name: str | None = Field(default=None, min_length=1, max_length=120)

    @field_validator("branch_name")
    @classmethod
    def clean_branch_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned or any(character in cleaned for character in "/\\\r\n\t"):
            raise ValueError("分支名不能为空，也不能包含路径或控制字符")
        return cleaned


class OutlineApprovalInput(StrictInput):
    plan_hash: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{64}$")
    allow_canon_overrides: bool = False


class OutlineRevisionInput(StrictInput):
    feedback: str = Field(min_length=1, max_length=100_000)
    chapter_id: str | None = Field(default=None, max_length=120)

    @field_validator("feedback")
    @classmethod
    def clean_feedback(cls, value: str) -> str:
        if not (cleaned := value.strip()):
            raise ValueError("退回意见不能为空")
        return cleaned


class CancelInput(StrictInput):
    force: bool = False


class RetryTaskInput(StrictInput):
    acknowledge_indeterminate: bool = False


class ChapterContentUpdateInput(StrictInput):
    content: str = Field(min_length=1, max_length=2_000_000)
    base_revision: int = Field(ge=0)
    base_sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    reason: str = Field(default="", max_length=2_000)


class ProviderKeysInput(StrictInput):
    openai_api_key: str | None = Field(default=None, max_length=2_000)
    deepseek_api_key: str | None = Field(default=None, max_length=2_000)

    @field_validator("openai_api_key", "deepseek_api_key")
    @classmethod
    def clean_key(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("API 密钥不能为空")
        if any(character in cleaned for character in "\r\n\x00"):
            raise ValueError("API 密钥不能包含换行或空字符")
        return cleaned


def _services(request: Request) -> tuple[Any, ViewBuilder]:
    return request.app.state.session_factory, request.app.state.views


def _uuid(value: str, label: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=f"{label}不存在") from exc


async def _runtime_result(request: Request, method: str, *args: Any, **kwargs: Any) -> tuple[bool, Any]:
    runtime = getattr(request.app.state, "runtime", None)
    callback = getattr(runtime, method, None) if runtime is not None else None
    if callback is None:
        return False, None
    result = callback(*args, **kwargs)
    if inspect.isawaitable(result):
        result = await result
    return True, result


async def _call_runtime(request: Request, method: str, *args: Any, **kwargs: Any) -> bool:
    called, _ = await _runtime_result(request, method, *args, **kwargs)
    return called


def _budget_for_analysis(payload: AnalysisRunInput) -> dict[str, Any]:
    return {
        "module": "summarizer",
        "splitter_enabled": payload.splitter_enabled,
        "max_cost_usd": payload.max_cost or 30.0,
        "max_tokens": payload.max_tokens or 2_000_000,
        "max_calls": payload.max_calls or 250,
        "max_wall_time_seconds": payload.max_wall_time or 86_400,
        "warning_fraction": 0.8,
        "task_retry_limit": 3,
    }


def _budget_for_continuation(payload: ContinuationRunInput) -> dict[str, Any]:
    predicted_upper = max(3.0, round(payload.target_chars * 0.00025, 2))
    return {
        "module": "continuator",
        "predicted_upper_cost_usd": predicted_upper,
        "max_cost_usd": payload.max_cost or round(predicted_upper * 1.5, 2),
        "max_tokens": payload.max_tokens or max(250_000, payload.target_chars * 12),
        "max_calls": payload.max_calls or 300,
        "max_wall_time_seconds": payload.max_wall_time or 86_400,
        "warning_fraction": 0.8,
        "max_revision_rounds": 3,
        "task_retry_limit": 3,
    }


def _normalize_uploaded_inspection(
    inspection: ImportInspection, original_name: str
) -> tuple[ImportInspection, str]:
    if len(inspection.documents) == 1 and Path(original_name).suffix.lower() in {".txt", ".md"}:
        temporary_name = inspection.documents[0].source_name
        inspection.documents[0] = replace(inspection.documents[0], source_name=original_name)
        for dimension, sections in inspection.summaries.items():
            inspection.summaries[dimension] = [
                replace(section, source_name=original_name)
                if section.source_name == temporary_name
                else section
                for section in sections
            ]
    inferred_title = inspection.title
    if inferred_title.startswith("upload-"):
        inferred_title = Path(original_name).stem
    return inspection, inferred_title


def _uploaded_relative_name(upload: UploadFile) -> str:
    """Retain browser-provided relative paths while rejecting unsafe filenames."""

    raw_name = (upload.filename or "upload").replace("\\", "/")
    relative_path = PurePosixPath(raw_name)
    if (
        not raw_name
        or relative_path.is_absolute()
        or not relative_path.parts
        or any(part in {"", ".", ".."} for part in relative_path.parts)
    ):
        raise HTTPException(status_code=422, detail="上传文件路径无效")
    return relative_path.as_posix()


def _natural_name_key(value: str) -> list[str | int]:
    return [int(item) if item.isdigit() else item.casefold() for item in re.split(r"(\d+)", value)]


def _combine_upload_inspections(
    inspections: list[tuple[ImportInspection, str]],
) -> tuple[ImportInspection, str]:
    """Merge independently-uploaded sources into one importable batch."""

    if not inspections:
        raise ImportValidationError("请至少上传一个素材文件")
    recovery_inspections = [item for item in inspections if item[0].project_manifest]
    if recovery_inspections and len(inspections) != 1:
        raise ImportValidationError("可恢复项目包必须单独导入，不能与其他素材合并")
    if recovery_inspections:
        return recovery_inspections[0]

    documents = [document for inspection, _title in inspections for document in inspection.documents]
    duplicate_names = {
        item.source_name
        for item in documents
        if sum(candidate.source_name == item.source_name for candidate in documents) > 1
    }
    if duplicate_names:
        raise ImportValidationError("批量导入存在重名文件：" + "、".join(sorted(duplicate_names)))
    documents.sort(key=lambda item: _natural_name_key(item.source_name))
    summaries = {}
    for inspection, _title in inspections:
        for dimension, sections in inspection.summaries.items():
            summaries.setdefault(dimension, []).extend(sections)
    summary_file_count = len({section.source_name for sections in summaries.values() for section in sections})
    kind = (
        ImportKind.SUMMARY_BUNDLE
        if len(summaries) >= 5 or summary_file_count >= 5
        else ImportKind.RAW_TEXT
    )
    warnings = [warning for inspection, _title in inspections for warning in inspection.warnings]
    if kind == ImportKind.SUMMARY_BUNDLE:
        missing = [item.value for item in SummaryDimension if not summaries.get(item)]
        if missing:
            warnings.append("总结包缺少维度：" + "、".join(missing))
    titles = [title for _inspection, title in inspections if title]
    return (
        ImportInspection(
            kind=kind,
            title=titles[0] if len(titles) == 1 else "批量素材",
            documents=documents,
            summaries=summaries if kind == ImportKind.SUMMARY_BUNDLE else {},
            warnings=list(dict.fromkeys(warnings)),
        ),
        titles[0] if len(titles) == 1 else "批量素材",
    )


def _apply_document_order(inspection: ImportInspection, order: str | None) -> None:
    if not order or inspection.project_manifest:
        return
    try:
        requested = json.loads(order)
    except json.JSONDecodeError as exc:
        raise ImportValidationError("素材排序格式无效") from exc
    if not isinstance(requested, list) or not all(isinstance(item, str) for item in requested):
        raise ImportValidationError("素材排序格式无效")
    by_name = {item.source_name: item for item in inspection.documents}
    if len(by_name) != len(inspection.documents) or set(requested) != set(by_name):
        raise ImportValidationError("素材清单已变化，请重新预检")
    inspection.documents = [by_name[item] for item in requested]


def _inspection_view(inspection: ImportInspection, inferred_title: str) -> dict[str, Any]:
    manifest = inspection.project_manifest or {}
    return {
        "kind": inspection.kind.value,
        "inferred_title": inferred_title,
        "warnings": list(inspection.warnings),
        "document_count": len(inspection.documents),
        "total_bytes": sum(item.byte_size for item in inspection.documents),
        "documents": [
            {
                "source_name": item.source_name,
                "encoding": item.encoding,
                "byte_size": item.byte_size,
                "text": item.text,
            }
            for item in inspection.documents
        ],
        "dimensions_found": [item.value for item in inspection.dimensions_found],
        "recoverable": inspection.kind.value == "project_bundle",
        "recovery_capabilities": manifest.get("recovery_capabilities") or {},
    }


def _store_import(
    request: Request,
    inspection: ImportInspection,
    *,
    project_name: str,
    original_uploads: list[tuple[Path, str]] | None = None,
    original_path: Path | None = None,
    original_name: str | None = None,
) -> str:
    session_factory, _ = _services(request)
    store = request.app.state.artifact_store
    settings = request.app.state.settings
    if original_uploads is None:
        original_uploads = [(original_path, original_name or original_path.name)] if original_path else []
    recovery_manifest = (
        inspection.project_manifest
        if inspection.project_manifest
        and inspection.project_manifest.get("format") == "novel-orchestrator-recovery"
        else None
    )
    recovery = (recovery_manifest or {}).get("recovery") or {}
    recovered_project = recovery.get("project") or {}
    recovered_branch = recovery.get("branch") or {}
    indexed_manifest = (
        {
            "schema_version": recovery_manifest.get("schema_version"),
            "format": recovery_manifest.get("format"),
            "recovery_capabilities": recovery_manifest.get("recovery_capabilities"),
        }
        if recovery_manifest
        else inspection.project_manifest
    )
    with session_factory.begin() as session:
        project = models.Project(
            name=project_name,
            description=str(recovered_project.get("description") or ""),
            status="active",
            active_branch=str(recovered_branch.get("name") or "main"),
            data_path="",
            settings_json={
                **(
                    recovered_project.get("settings")
                    if isinstance(recovered_project.get("settings"), dict)
                    else {}
                ),
                "import_kind": inspection.kind.value,
                "warnings": inspection.warnings,
                "project_manifest": indexed_manifest,
                "restored_history": False,
            },
        )
        session.add(project)
        session.flush()
        project_path = (settings.project_dir / project.id).resolve()
        project_path.mkdir(parents=True, exist_ok=True)
        project.data_path = str(project_path)

        source_ids: list[str] = []
        source_artifact_ids_by_name: dict[str, str] = {}
        documents_by_name = {item.source_name: item for item in inspection.documents}
        recovery_sources = recovery.get("source_artifacts") or []
        source_documents: list[tuple[Any, dict[str, Any]]] = []
        if recovery_manifest:
            for item in recovery_sources:
                archive_path = str(item.get("archive_path") or "")
                document = documents_by_name.get(archive_path)
                if document is None:
                    raise ImportValidationError(f"恢复包缺少源稿成员：{archive_path}")
                source_documents.append((document, item))
        else:
            source_documents = [(item, {}) for item in inspection.documents]

        for index, (document, recovered) in enumerate(source_documents, start=1):
            stored = store.put_text(document.text, suffix=Path(document.source_name).suffix or ".txt")
            expected_sha = str(recovered.get("sha256") or "")
            if expected_sha and stored.sha256 != expected_sha:
                raise ImportValidationError(f"恢复源稿哈希不匹配：{document.source_name}")
            recovered_config = recovered.get("generation_config")
            artifact = models.Artifact(
                project_id=project.id,
                kind="source_document",
                version=1,
                relative_path=stored.relative_path,
                sha256=stored.sha256,
                media_type=stored.media_type,
                byte_size=stored.byte_size,
                source_artifact_ids_json=[],
                generation_config_json={
                    **(recovered_config if isinstance(recovered_config, dict) else {}),
                    "source_name": (
                        (recovered_config or {}).get("source_name")
                        if isinstance(recovered_config, dict)
                        else document.source_name
                    ),
                    "encoding": document.encoding,
                    "original_byte_size": document.byte_size,
                    "sequence_index": index,
                    "restored": bool(recovery_manifest),
                },
                immutable=True,
            )
            session.add(artifact)
            session.flush()
            source_ids.append(artifact.id)
            source_artifact_ids_by_name[document.source_name] = artifact.id

        for original_path, original_name in original_uploads:
            if original_path.suffix.lower() != ".zip":
                continue
            # Recovery packages must retain non-text members as well as normalized documents.
            stored_archive = store.put_file(original_path, suffix=".zip", media_type="application/zip")
            archive = models.Artifact(
                project_id=project.id,
                kind="recovery_archive" if recovery_manifest else "source_archive",
                version=1,
                relative_path=stored_archive.relative_path,
                sha256=stored_archive.sha256,
                media_type=stored_archive.media_type,
                byte_size=stored_archive.byte_size,
                source_artifact_ids_json=[],
                generation_config_json={"source_name": original_name},
                immutable=True,
            )
            session.add(archive)
            session.flush()
            if not recovery_manifest:
                source_ids.append(archive.id)

        restored_story_state_id: str | None = None
        if recovery_manifest:
            branch_name = str(recovered_branch.get("name") or "main")
            state = StoryStateV1.model_validate(recovered_branch.get("story_state"))
            state = StoryStateV1.model_validate(
                state.model_copy(
                    update={
                        "project_id": UUID(project.id),
                        "branch_name": branch_name,
                        "source_artifact_ids": [UUID(item) for item in source_ids],
                    }
                ).model_dump(mode="python")
            )
            stored_state = store.put_json(state.model_dump(mode="json"))
            state_artifact = models.Artifact(
                project_id=project.id,
                kind="story_state",
                version=state.revision,
                relative_path=stored_state.relative_path,
                sha256=stored_state.sha256,
                media_type=stored_state.media_type,
                byte_size=stored_state.byte_size,
                source_artifact_ids_json=source_ids,
                generation_config_json={
                    "branch_name": branch_name,
                    "restored": True,
                    "state_id": str(state.state_id),
                },
                immutable=True,
            )
            session.add(state_artifact)
            session.flush()
            restored_story_state_id = state_artifact.id
            project.active_branch = branch_name
            project.current_story_state_artifact_id = state_artifact.id
            session.add(
                models.BranchHead(
                    project_id=project.id,
                    branch_name=branch_name,
                    artifact_id=state_artifact.id,
                    version=state.revision,
                )
            )

            source_paths = {str(item.get("archive_path") or "") for item in recovery_sources}
            artifact_index = recovery_manifest.get("artifact_index") or []
            with zipfile.ZipFile(original_path) as archive_file:
                member_names = set(archive_file.namelist())
                for item in artifact_index:
                    if not isinstance(item, dict):
                        continue
                    archive_path = str(item.get("archive_path") or "").replace("\\", "/")
                    if (
                        not archive_path
                        or archive_path in source_paths
                        or archive_path not in member_names
                        or archive_path == "recovery-manifest.json"
                    ):
                        continue
                    data = archive_file.read(archive_path)
                    suffix = Path(archive_path).suffix.lower() or ".bin"
                    media_type = {
                        ".md": "text/markdown; charset=utf-8",
                        ".txt": "text/plain; charset=utf-8",
                        ".json": "application/json; charset=utf-8",
                        ".zip": "application/zip",
                    }.get(suffix, "application/octet-stream")
                    stored_delivery = store.put_bytes(data, suffix=suffix, media_type=media_type)
                    expected_sha = str(item.get("sha256") or "")
                    if expected_sha and stored_delivery.sha256 != expected_sha:
                        raise ImportValidationError(f"恢复交付文件哈希不匹配：{archive_path}")
                    session.add(
                        models.Artifact(
                            project_id=project.id,
                            kind="delivery_restored",
                            version=1,
                            relative_path=stored_delivery.relative_path,
                            sha256=stored_delivery.sha256,
                            media_type=stored_delivery.media_type,
                            byte_size=stored_delivery.byte_size,
                            source_artifact_ids_json=[state_artifact.id],
                            generation_config_json={
                                "archive_path": archive_path,
                                "filename": Path(archive_path).name,
                                "restored": True,
                            },
                            immutable=True,
                        )
                    )

        summary_story_state_id: str | None = None
        if inspection.kind == ImportKind.SUMMARY_BUNDLE:
            summary_state = build_story_state_from_summary(
                inspection,
                project_id=UUID(project.id),
                source_artifact_ids=source_artifact_ids_by_name,
                branch_name="main",
            )
            if summary_state is not None:
                stored_state = store.put_json(summary_state.model_dump(mode="json"))
                state_source_ids = [str(item) for item in summary_state.source_artifact_ids]
                state_artifact = models.Artifact(
                    project_id=project.id,
                    kind="story_state",
                    version=summary_state.revision,
                    relative_path=stored_state.relative_path,
                    sha256=stored_state.sha256,
                    media_type=stored_state.media_type,
                    byte_size=stored_state.byte_size,
                    source_artifact_ids_json=state_source_ids,
                    generation_config_json={
                        "branch_name": "main",
                        "imported_from_summary": True,
                        "state_id": str(summary_state.state_id),
                        "dimensions": [item.value for item in inspection.dimensions_found],
                    },
                    immutable=True,
                )
                session.add(state_artifact)
                session.flush()
                summary_story_state_id = state_artifact.id
                project.active_branch = "main"
                project.current_story_state_artifact_id = state_artifact.id
                session.add(
                    models.BranchHead(
                        project_id=project.id,
                        branch_name="main",
                        artifact_id=state_artifact.id,
                        version=summary_state.revision,
                    )
                )

        summaries = {
            dimension.value: [
                {
                    "source_name": section.source_name,
                    "title": section.title,
                    "content": section.content,
                }
                for section in sections
            ]
            for dimension, sections in inspection.summaries.items()
        }
        manifest_value = {
            "schema_version": "1.0",
            "kind": inspection.kind.value,
            "title": inspection.title,
            "warnings": inspection.warnings,
            "documents": [
                {
                    "source_name": item.source_name,
                    "encoding": item.encoding,
                    "byte_size": item.byte_size,
                }
                for item in inspection.documents
            ],
            "summaries": summaries,
            "project_manifest": inspection.project_manifest,
            "source_artifact_ids": source_ids,
            "restored_story_state_artifact_id": restored_story_state_id,
            "summary_story_state_artifact_id": summary_story_state_id,
        }
        stored_manifest = store.put_json(manifest_value)
        session.add(
            models.Artifact(
                project_id=project.id,
                kind="import_manifest",
                version=1,
                relative_path=stored_manifest.relative_path,
                sha256=stored_manifest.sha256,
                media_type=stored_manifest.media_type,
                byte_size=stored_manifest.byte_size,
                source_artifact_ids_json=source_ids,
                generation_config_json={"title": "导入清单"},
                immutable=True,
            )
        )
        return project.id


async def _receive_upload(request: Request, upload: UploadFile) -> Path:
    suffix = Path(upload.filename or "upload").suffix.lower()
    if suffix not in {".txt", ".md", ".zip"}:
        raise HTTPException(status_code=415, detail="仅支持 .txt、.md 和项目 ZIP")
    incoming = request.app.state.settings.data_dir / "incoming"
    incoming.mkdir(parents=True, exist_ok=True)
    size = 0
    temporary: tempfile.NamedTemporaryFile[Any] | None = None
    try:
        temporary = tempfile.NamedTemporaryFile(
            mode="wb", delete=False, suffix=suffix, dir=incoming, prefix="upload-"
        )
        while chunk := await upload.read(1024 * 1024):
            size += len(chunk)
            if size > request.app.state.settings.max_upload_bytes:
                raise HTTPException(status_code=413, detail="上传文件超过大小限制")
            temporary.write(chunk)
        temporary.flush()
        path = Path(temporary.name)
        temporary.close()
        temporary = None
        return path
    except Exception:
        if temporary is not None:
            path = Path(temporary.name)
            temporary.close()
            path.unlink(missing_ok=True)
        raise
    finally:
        await upload.close()


async def _inspect_uploaded_batch(
    request: Request,
    uploads: list[UploadFile],
) -> tuple[ImportInspection, str, list[tuple[Path, str]]]:
    if not uploads:
        raise HTTPException(status_code=422, detail="请至少上传一个素材文件")
    temporary_uploads: list[tuple[Path, str]] = []
    try:
        for upload in uploads:
            original_name = _uploaded_relative_name(upload)
            temporary_uploads.append((await _receive_upload(request, upload), original_name))
        inspections = []
        for temporary_path, original_name in temporary_uploads:
            inspection = inspect_import(
                temporary_path,
                max_total_bytes=request.app.state.settings.max_upload_bytes,
                max_members=request.app.state.settings.max_archive_members,
            )
            inspections.append(_normalize_uploaded_inspection(inspection, original_name))
        inspection, inferred_title = _combine_upload_inspections(inspections)
        return inspection, inferred_title, temporary_uploads
    except Exception:
        for temporary_path, _original_name in temporary_uploads:
            temporary_path.unlink(missing_ok=True)
        raise


def _uploaded_files(
    files: list[UploadFile] | None,
    file: UploadFile | None,
) -> list[UploadFile]:
    return [*(files or []), *([file] if file is not None else [])]


@router.get("/health")
async def health(request: Request) -> dict[str, Any]:
    return {
        "status": "ok",
        "service": request.app.state.settings.app_name,
        "time": datetime.now(UTC).isoformat(),
    }


@router.get("/providers/status")
async def provider_status(request: Request) -> dict[str, Any]:
    settings = request.app.state.settings
    openai = settings.openai_api_key is not None
    deepseek = settings.deepseek_api_key is not None
    return {
        "providers": [
            {
                "id": "openai",
                "name": "OpenAI",
                "configured": openai,
                "available": None if openai else False,
                "model": OPENAI_DEFAULT_MODEL,
                "message": "已配置，尚未探测连接" if openai else "未设置 OPENAI_API_KEY",
            },
            {
                "id": "deepseek",
                "name": "DeepSeek",
                "configured": deepseek,
                "available": None if deepseek else False,
                "model": DEEPSEEK_DEFAULT_MODEL,
                "message": "已配置，尚未探测连接" if deepseek else "未设置 DEEPSEEK_API_KEY",
            },
        ]
    }


@router.post("/providers/keys")
async def save_provider_keys(request: Request, payload: ProviderKeysInput) -> dict[str, Any]:
    if payload.openai_api_key is None and payload.deepseek_api_key is None:
        raise HTTPException(status_code=422, detail="至少需要提供一个 API 密钥")

    try:
        settings = save_provider_api_keys(
            request.app.state.settings,
            openai_api_key=payload.openai_api_key,
            deepseek_api_key=payload.deepseek_api_key,
        )
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=500, detail=f"无法保存 API 密钥：{exc}") from exc

    request.app.state.settings = settings
    registry = getattr(getattr(request.app.state, "runtime", None), "providers", None)
    provider_map = getattr(registry, "providers", None)
    if isinstance(provider_map, dict):
        if payload.openai_api_key is not None:
            provider_map[ProviderName.OPENAI] = OpenAIResponsesProvider(payload.openai_api_key)
        if payload.deepseek_api_key is not None:
            provider_map[ProviderName.DEEPSEEK] = DeepSeekChatProvider(payload.deepseek_api_key)

    return await provider_status(request)


@router.get("/prompts/versions")
async def prompt_versions(request: Request) -> dict[str, Any]:
    manifest = request.app.state.prompt_store.version_manifest()
    return {"prompts": [{"file": filename, **details} for filename, details in sorted(manifest.items())]}


@router.get("/projects")
async def list_projects(request: Request) -> dict[str, Any]:
    return {"projects": request.app.state.views.list_projects()}


@router.post("/projects", status_code=status.HTTP_201_CREATED)
async def create_project(request: Request, payload: ProjectCreateInput) -> dict[str, Any]:
    session_factory, views = _services(request)
    with session_factory.begin() as session:
        project = models.Project(
            name=payload.name,
            description=payload.description,
            status="active",
            active_branch="main",
            data_path="",
            settings_json={},
        )
        session.add(project)
        session.flush()
        project_path = (request.app.state.settings.project_dir / project.id).resolve()
        project_path.mkdir(parents=True, exist_ok=True)
        project.data_path = str(project_path)
        project_id = project.id
    return {"project": views.project_detail(project_id)}


@router.post("/projects/import/inspect")
async def inspect_project_import(
    request: Request,
    files: Annotated[list[UploadFile] | None, File()] = None,
    file: Annotated[UploadFile | None, File()] = None,
) -> dict[str, Any]:
    temporary_uploads: list[tuple[Path, str]] = []
    try:
        inspection, inferred_title, temporary_uploads = await _inspect_uploaded_batch(
            request, _uploaded_files(files, file)
        )
        return {"inspection": _inspection_view(inspection, inferred_title)}
    except ImportValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        for temporary_path, _original_name in temporary_uploads:
            temporary_path.unlink(missing_ok=True)


@router.post("/projects/import", status_code=status.HTTP_201_CREATED)
async def import_project(
    request: Request,
    files: Annotated[list[UploadFile] | None, File()] = None,
    file: Annotated[UploadFile | None, File()] = None,
    name: Annotated[str | None, Form()] = None,
    order: Annotated[str | None, Form()] = None,
) -> dict[str, Any]:
    temporary_uploads: list[tuple[Path, str]] = []
    try:
        inspection, inferred_title, temporary_uploads = await _inspect_uploaded_batch(
            request, _uploaded_files(files, file)
        )
        _apply_document_order(inspection, order)
        project_name = (name or inferred_title or "未命名小说").strip()
        if not project_name:
            project_name = "未命名小说"
        if len(project_name) > 240:
            raise HTTPException(status_code=422, detail="项目名称不能超过 240 个字符")
        project_id = _store_import(
            request,
            inspection,
            project_name=project_name,
            original_uploads=temporary_uploads,
        )
    except ImportValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        for temporary_path, _original_name in temporary_uploads:
            temporary_path.unlink(missing_ok=True)
    return {"project": request.app.state.views.project_detail(project_id)}


@router.get("/projects/{project_id}")
async def get_project(request: Request, project_id: str) -> dict[str, Any]:
    _uuid(project_id, "项目")
    try:
        return {"project": request.app.state.views.project_detail(project_id)}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="项目不存在") from exc


def _ensure_project(session_factory: Any, project_id: str) -> models.Project:
    with session_factory() as session:
        project = session.get(models.Project, project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="项目不存在")
        session.expunge(project)
        return project


async def _start_summarizer_run(
    request: Request,
    project_id: str,
    payload: AnalysisRunInput | None = None,
) -> dict[str, Any]:
    payload = payload or AnalysisRunInput()
    project_uuid = _uuid(project_id, "项目")
    session_factory, views = _services(request)
    project = _ensure_project(session_factory, project_id)
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is not None and getattr(runtime, "create_analysis_run", None) is not None:
        snapshot = await create_summary_run(
            runtime,
            project_id=project_uuid,
            branch_name=project.active_branch,
            provider_preset=payload.routing_preset,
            budget=_budget_for_analysis(payload),
            splitter_enabled=payload.splitter_enabled,
        )
    else:
        snapshot = request.app.state.repository.create_run(
            project_id=project_uuid,
            kind="analysis",
            branch_name=project.active_branch,
            target_chars=None,
            provider_preset=payload.routing_preset,
            user_requirements="",
            budget=_budget_for_analysis(payload),
        )
        request.app.state.repository.update_run(
            snapshot.id,
            status="queued",
            stage="summarizer_split",
            started_at=datetime.now(UTC),
        )
        await _call_runtime(request, "enqueue", snapshot.id)
    return {"run": views.run_detail(str(snapshot.id))}


@router.post("/summarizer/projects/{project_id}/runs", status_code=status.HTTP_202_ACCEPTED)
async def start_summarizer(
    request: Request,
    project_id: str,
    payload: AnalysisRunInput | None = None,
) -> dict[str, Any]:
    return await _start_summarizer_run(request, project_id, payload)


@router.post("/projects/{project_id}/analysis-runs", status_code=status.HTTP_202_ACCEPTED)
async def start_analysis(
    request: Request,
    project_id: str,
    payload: AnalysisRunInput | None = None,
) -> dict[str, Any]:
    return await _start_summarizer_run(request, project_id, payload)


async def _start_continuator_run(
    request: Request,
    project_id: str,
    payload: ContinuationRunInput,
) -> dict[str, Any]:
    project_uuid = _uuid(project_id, "项目")
    session_factory, views = _services(request)
    project = _ensure_project(session_factory, project_id)
    branch_name = payload.branch_name or project.active_branch
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is not None and getattr(runtime, "create_continuation_run", None) is not None:
        snapshot = await create_continuator_run(
            runtime,
            project_id=project_uuid,
            target_chars=payload.target_chars,
            branch_name=branch_name,
            provider_preset=payload.routing_preset,
            user_requirements=payload.user_requirements,
            budget=_budget_for_continuation(payload),
        )
    else:
        snapshot = request.app.state.repository.create_run(
            project_id=project_uuid,
            kind="continuation",
            branch_name=branch_name,
            target_chars=payload.target_chars,
            provider_preset=payload.routing_preset,
            user_requirements=payload.user_requirements,
            budget=_budget_for_continuation(payload),
        )
        request.app.state.repository.update_run(
            snapshot.id,
            status="queued",
            stage="continuator_outline",
            started_at=datetime.now(UTC),
        )
        await _call_runtime(request, "enqueue", snapshot.id)
    return {"run": views.run_detail(str(snapshot.id))}


@router.post("/continuator/projects/{project_id}/runs", status_code=status.HTTP_202_ACCEPTED)
async def start_continuator(
    request: Request,
    project_id: str,
    payload: ContinuationRunInput,
) -> dict[str, Any]:
    return await _start_continuator_run(request, project_id, payload)


@router.post("/projects/{project_id}/continuation-runs", status_code=status.HTTP_202_ACCEPTED)
async def start_continuation(
    request: Request,
    project_id: str,
    payload: ContinuationRunInput,
) -> dict[str, Any]:
    return await _start_continuator_run(request, project_id, payload)


@router.get("/runs/{run_id}")
async def get_run(request: Request, run_id: str) -> dict[str, Any]:
    _uuid(run_id, "运行")
    try:
        return {"run": request.app.state.views.run_detail(run_id)}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="运行不存在") from exc


def _get_run_for_update(session: Any, run_id: str) -> models.Run:
    run = session.get(models.Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="运行不存在")
    return run


@router.post("/runs/{run_id}/outline/approve")
async def approve_outline(
    request: Request,
    run_id: str,
    payload: OutlineApprovalInput | None = None,
) -> dict[str, Any]:
    payload = payload or OutlineApprovalInput()
    run_uuid = _uuid(run_id, "运行")
    session_factory, views = _services(request)
    with session_factory() as session:
        run = _get_run_for_update(session, run_id)
        if run.kind != "continuation":
            raise HTTPException(status_code=409, detail="只有续写运行可以批准章纲")
        if not run.plan_artifact_id:
            raise HTTPException(status_code=409, detail="章纲尚未生成")
        plan_artifact = session.get(models.Artifact, run.plan_artifact_id)
        if plan_artifact is None:
            raise HTTPException(status_code=409, detail="章纲 artifact 不存在")
        project = session.get(models.Project, run.project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="项目不存在")
        frozen_hash = run.plan_hash or plan_artifact.sha256
        if payload.plan_hash and payload.plan_hash.lower() != frozen_hash.lower():
            raise HTTPException(status_code=409, detail="章纲已变化，请刷新后重新批准")
        if payload.allow_canon_overrides and run.branch_name == project.active_branch:
            raise HTTPException(
                status_code=409,
                detail="Canon 覆盖必须在创建续写运行时指定新的分支名",
            )
    called = await _call_runtime(
        request,
        "approve_outline",
        run_uuid,
        frozen_hash,
        allow_canon_overrides=payload.allow_canon_overrides,
    )
    if not called:
        with session_factory.begin() as session:
            run = _get_run_for_update(session, run_id)
            run.plan_hash = frozen_hash
            run.approved_at = datetime.now(UTC)
            run.status = "queued"
            run.stage = "writing_queued"
            run.pause_requested = False
        await _call_runtime(request, "enqueue", run_uuid)
    return {"run": views.run_detail(run_id)}


@router.post("/runs/{run_id}/outline/revise", status_code=status.HTTP_202_ACCEPTED)
async def revise_outline(
    request: Request,
    run_id: str,
    payload: OutlineRevisionInput,
) -> dict[str, Any]:
    run_uuid = _uuid(run_id, "运行")
    session_factory, views = _services(request)
    with session_factory() as session:
        run = _get_run_for_update(session, run_id)
        if run.kind != "continuation":
            raise HTTPException(status_code=409, detail="只有续写运行可以退回章纲")
        if not run.plan_artifact_id:
            raise HTTPException(status_code=409, detail="章纲尚未生成")
        project_id = run.project_id
        plan_artifact_id = run.plan_artifact_id
    called = await _call_runtime(
        request,
        "revise_outline",
        run_uuid,
        payload.feedback,
        chapter_id=payload.chapter_id,
    )
    if not called:
        feedback_value = {
            "schema_version": "1.0",
            "run_id": run_id,
            "plan_artifact_id": plan_artifact_id,
            "chapter_id": payload.chapter_id,
            "feedback": payload.feedback,
            "created_at": datetime.now(UTC).isoformat(),
        }
        stored = request.app.state.artifact_store.put_json(feedback_value)
        with session_factory.begin() as session:
            session.add(
                models.Artifact(
                    project_id=project_id,
                    run_id=run_id,
                    kind="outline_feedback",
                    version=1,
                    relative_path=stored.relative_path,
                    sha256=stored.sha256,
                    media_type=stored.media_type,
                    byte_size=stored.byte_size,
                    source_artifact_ids_json=[plan_artifact_id],
                    generation_config_json={
                        "title": "章纲退回意见",
                        "chapter_id": payload.chapter_id,
                    },
                    immutable=True,
                )
            )
            run = _get_run_for_update(session, run_id)
            run.plan_artifact_id = None
            run.plan_hash = None
            run.approved_at = None
            run.status = "queued"
            run.stage = "plan_revision_queued"
        await _call_runtime(request, "enqueue", run_uuid)
    return {"run": views.run_detail(run_id)}


@router.post("/runs/{run_id}/pause")
async def pause_run(request: Request, run_id: str) -> dict[str, Any]:
    run_uuid = _uuid(run_id, "运行")
    session_factory, views = _services(request)
    with session_factory() as session:
        run = _get_run_for_update(session, run_id)
        if run.status not in {"draft", "approved", "queued", "running"}:
            raise HTTPException(status_code=409, detail="当前运行状态不能暂停")
    called = await _call_runtime(request, "pause", run_uuid)
    if not called:
        with session_factory.begin() as session:
            run = _get_run_for_update(session, run_id)
            if run.status in TERMINAL_RUN_STATUSES:
                raise HTTPException(status_code=409, detail="已结束的运行不能暂停")
            run.pause_requested = True
            run.status = "paused"
            run.stage = "paused"
    return {"run": views.run_detail(run_id)}


@router.post("/runs/{run_id}/resume")
async def resume_run(request: Request, run_id: str) -> dict[str, Any]:
    run_uuid = _uuid(run_id, "运行")
    session_factory, views = _services(request)
    with session_factory() as session:
        run = _get_run_for_update(session, run_id)
        if run.status not in {"paused", "pausing"} and not run.pause_requested:
            raise HTTPException(status_code=409, detail="运行当前未暂停")
    called = await _call_runtime(request, "resume", run_uuid)
    if not called:
        with session_factory.begin() as session:
            run = _get_run_for_update(session, run_id)
            if run.status != "paused" and not run.pause_requested:
                raise HTTPException(status_code=409, detail="运行当前未暂停")
            run.pause_requested = False
            run.status = "queued"
            run.stage = "resume_queued"
        await _call_runtime(request, "enqueue", run_uuid)
    return {"run": views.run_detail(run_id)}


@router.post("/runs/{run_id}/cancel")
async def cancel_run(
    request: Request,
    run_id: str,
    payload: CancelInput | None = None,
) -> dict[str, Any]:
    payload = payload or CancelInput()
    run_uuid = _uuid(run_id, "运行")
    session_factory, views = _services(request)
    called = await _call_runtime(request, "cancel", run_uuid, force=payload.force)
    if not called:
        with session_factory.begin() as session:
            run = _get_run_for_update(session, run_id)
            if run.status in {"completed", "cancelled", "canceled"}:
                raise HTTPException(status_code=409, detail="运行已经结束")
            run.cancel_requested = True
            run.status = "cancelled"
            run.stage = "cancelled"
            run.finished_at = datetime.now(UTC)
    return {"run": views.run_detail(run_id)}


@router.post("/runs/{run_id}/tasks/{task_id}/retry", status_code=status.HTTP_202_ACCEPTED)
async def retry_task(
    request: Request,
    run_id: str,
    task_id: str,
    payload: RetryTaskInput | None = None,
) -> dict[str, Any]:
    payload = payload or RetryTaskInput()
    run_uuid = _uuid(run_id, "运行")
    task_uuid = _uuid(task_id, "任务")
    session_factory, views = _services(request)
    with session_factory() as session:
        task = session.get(models.Task, task_id)
        if task is None or task.run_id != run_id:
            raise HTTPException(status_code=404, detail="任务不存在")
    called = await _call_runtime(
        request,
        "retry_task",
        task_uuid,
        acknowledge_indeterminate=payload.acknowledge_indeterminate,
    )
    if not called:
        request.app.state.repository.retry_task(
            task_uuid,
            acknowledge_indeterminate=payload.acknowledge_indeterminate,
        )
    request.app.state.repository.update_run(
        run_uuid,
        status="queued",
        stage="retry_queued",
        pause_requested=False,
        cancel_requested=False,
        finished_at=None,
    )
    await _call_runtime(request, "enqueue", run_uuid)
    return {"run": views.run_detail(run_id)}


@router.patch("/runs/{run_id}/chapters/{chapter_id}/content")
async def update_chapter_content(
    request: Request,
    run_id: str,
    chapter_id: str,
    payload: ChapterContentUpdateInput,
) -> dict[str, Any]:
    """Create an immutable human-edited chapter version with optimistic locking."""
    _uuid(run_id, "运行")
    session_factory, views = _services(request)
    content = payload.content.replace("\r\n", "\n").strip()
    if not content:
        raise HTTPException(status_code=422, detail="正文不能为空")
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    now = datetime.now(UTC)
    with session_factory.begin() as session:
        run = _get_run_for_update(session, run_id)
        editable = {"paused", "completed", "failed", "needs_human", "cancelled", "canceled"}
        if run.status not in editable:
            raise HTTPException(status_code=409, detail="运行中不能编辑正文，请先暂停运行")
        project = session.get(models.Project, run.project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="项目不存在")
        artifacts = session.scalars(
            select(models.Artifact)
            .where(models.Artifact.run_id == run_id)
            .order_by(models.Artifact.created_at.asc())
        ).all()
        candidates = []
        for artifact in artifacts:
            config = artifact.generation_config_json or {}
            key = str(config.get("chapter_id") or config.get("chapter_number") or "")
            if (
                key == chapter_id
                and "chapter" in artifact.kind
                and artifact.media_type.startswith("text/")
            ):
                candidates.append(artifact)
        if not candidates:
            raise HTTPException(status_code=404, detail="章节正文不存在")
        current = max(candidates, key=lambda item: (
            int((item.generation_config_json or {}).get("revision", 0) or 0),
            item.created_at,
        ))
        current_config = current.generation_config_json or {}
        current_revision = int(current_config.get("revision", 0) or 0)
        current_sha = current.sha256
        if (
            payload.base_revision != current_revision
            or payload.base_sha256.lower() != current_sha.lower()
        ):
            raise HTTPException(status_code=409, detail="章节已被其他版本更新，请刷新后重试")
        if digest == current_sha:
            return {"run": views.run_detail(run_id), "unchanged": True}
        stored = request.app.state.artifact_store.put_text(content)
        chapter_number = current_config.get("chapter_number", chapter_id)
        title = str(current_config.get("title") or f"第{chapter_number}章")
        next_config = {
            **current_config,
            "chapter_id": chapter_id,
            "revision": current_revision + 1,
            "status": "manual_edited",
            "source": "human",
            "edited_at": now.isoformat(),
            "reason": payload.reason.strip(),
            "title": title,
            "base_revision": current_revision,
            "base_sha256": current_sha,
        }
        session.add(models.Artifact(
            project_id=run.project_id,
            run_id=run_id,
            kind="chapter_manual",
            version=current_revision + 2,
            relative_path=stored.relative_path,
            sha256=stored.sha256,
            media_type=stored.media_type,
            byte_size=stored.byte_size,
            source_artifact_ids_json=[current.id],
            generation_config_json=next_config,
            immutable=True,
        ))
        usage = dict(run.usage_json or {})
        usage.update({
            "content_revision_invalidated": True,
            "content_invalidated_at": now.isoformat(),
            "content_invalidated_chapters": sorted(set([
                *(usage.get("content_invalidated_chapters") or []), chapter_id,
            ])),
            "content_last_edited_chapter": chapter_id,
        })
        run.usage_json = usage
        run.status = "needs_human"
        run.stage = "content_revision_invalidated"
    broker = request.app.state.event_broker
    publish = getattr(broker, "publish", None)
    if publish is not None:
        published = publish(
            _broker_key(broker, run_id),
            "content_invalidated",
            message="正文已保存，审校与交付已失效",
        )
        if inspect.isawaitable(published):
            await published
    return {"run": views.run_detail(run_id), "artifact_sha256": digest, "revision": current_revision + 1}


@router.get("/artifacts/{artifact_id}/download")
async def download_artifact(request: Request, artifact_id: str) -> FileResponse:
    _uuid(artifact_id, "产物")
    session_factory, _ = _services(request)
    with session_factory() as session:
        artifact = session.get(models.Artifact, artifact_id)
        if artifact is None:
            raise HTTPException(status_code=404, detail="产物不存在")
        if artifact.kind.startswith("delivery_") and artifact.run_id:
            run = session.get(models.Run, artifact.run_id)
            if run and (run.usage_json or {}).get("content_revision_invalidated"):
                raise HTTPException(status_code=409, detail="正文已人工修改，交付物已失效，请重新审校")
        try:
            path = request.app.state.artifact_store.resolve(artifact.relative_path)
        except ArtifactStorageError as exc:
            raise HTTPException(status_code=410, detail="产物路径无效") from exc
        if not path.is_file():
            raise HTTPException(status_code=410, detail="产物文件已丢失")
        filename = safe_download_name(artifact)
        media_type = artifact.media_type.split(";", 1)[0]
    return FileResponse(path, media_type=media_type, filename=filename)


@router.get("/artifacts/{artifact_id}/preview")
async def preview_artifact(request: Request, artifact_id: str) -> FileResponse:
    _uuid(artifact_id, "产物")
    session_factory, _ = _services(request)
    with session_factory() as session:
        artifact = session.get(models.Artifact, artifact_id)
        if artifact is None:
            raise HTTPException(status_code=404, detail="产物不存在")
        try:
            path = request.app.state.artifact_store.resolve(artifact.relative_path)
        except ArtifactStorageError as exc:
            raise HTTPException(status_code=410, detail="产物路径无效") from exc
        if not path.is_file():
            raise HTTPException(status_code=410, detail="产物文件已丢失")
        filename = safe_download_name(artifact)
        media_type = artifact.media_type.split(";", 1)[0]
    return FileResponse(
        path,
        media_type=media_type,
        filename=filename,
        content_disposition_type="inline",
    )


def _sse(event: str, data: Mapping[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


def _broker_key(broker: Any, run_id: str) -> str | UUID:
    return run_id if broker.__class__.__module__.endswith(".events") else UUID(run_id)


def _browser_event_name(event: str) -> str:
    if event in {"run_completed", "analysis_completed", "delivery_ready"}:
        return "complete"
    if event in {"run_failed", "analysis_failed"}:
        return "failed"
    if event.startswith("task_"):
        return "task"
    if event.startswith("usage_") or event.startswith("budget_"):
        return "usage"
    return "stage"


async def _event_stream(request: Request, run_id: str) -> AsyncIterator[str]:
    views: ViewBuilder = request.app.state.views
    yield _sse(
        "snapshot",
        {
            "event": "snapshot",
            "type": "snapshot",
            "run": views.run_detail(run_id),
            "message": "运行事件流已连接",
        },
    )
    broker = request.app.state.event_broker
    iterator = broker.subscribe(_broker_key(broker, run_id)).__aiter__()
    pending: asyncio.Task[Any] | None = asyncio.create_task(anext(iterator))
    try:
        while True:
            done, _ = await asyncio.wait({pending}, timeout=15)
            if not done:
                yield ": keep-alive\n\n"
                continue
            try:
                item = pending.result()
            except StopAsyncIteration:
                return
            pending = asyncio.create_task(anext(iterator))
            if isinstance(item, str):
                yield item
                continue
            if not isinstance(item, Mapping):
                yield _sse("message", {"event": "message", "payload": item})
                continue
            event = str(item.get("event", "message"))
            yield _sse(_browser_event_name(event), {"type": event, **dict(item)})
    finally:
        if pending is not None:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        aclose = getattr(iterator, "aclose", None)
        if aclose is not None:
            await aclose()


@router.get("/runs/{run_id}/events")
async def run_events(request: Request, run_id: str) -> StreamingResponse:
    _uuid(run_id, "运行")
    session_factory, _ = _services(request)
    with session_factory() as session:
        if session.get(models.Run, run_id) is None:
            raise HTTPException(status_code=404, detail="运行不存在")
    return StreamingResponse(
        _event_stream(request, run_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def register_exception_handlers(app: Any) -> None:
    @app.exception_handler(PlanHashMismatch)
    async def plan_hash_mismatch(_request: Request, exc: PlanHashMismatch) -> Any:
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(IndeterminateTaskBlocked)
    async def indeterminate_task(_request: Request, exc: IndeterminateTaskBlocked) -> Any:
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=409, content={"detail": str(exc), "code": exc.code})

    @app.exception_handler(OrchestrationError)
    async def orchestration_error(_request: Request, exc: OrchestrationError) -> Any:
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=409, content={"detail": str(exc), "code": exc.code})
