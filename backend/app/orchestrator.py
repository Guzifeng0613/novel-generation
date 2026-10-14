"""Persistent multi-agent novel pipeline.

The orchestrator is the only component allowed to schedule work or promote a
branch memory head.  LLMs receive explicit envelopes and return text/JSON;
they never receive filesystem or scheduling capabilities.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import time
from collections import defaultdict
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, sessionmaker

from . import models
from .exports import ChapterExport, build_delivery_bundle
from .prompting import PromptStore
from .providers import (
    ROUTING_PRESETS,
    AgentRole,
    AuthenticationProviderError,
    IndeterminateProviderError,
    LLMProviderError,
    LLMRequest,
    LLMResponse,
    ModelRoute,
    ModelUsage,
    ProviderName,
    ProviderRegistry,
    RetryableProviderError,
    StructuredOutputError,
    redacted_request_record,
)
from .quality import (
    DIMENSION_WEIGHTS,
    REVIEW_GROUPS,
    QualityGateResult,
    build_revision_manifest,
    evaluate_quality,
    has_repeated_blockers,
    merge_review_reports,
    run_deterministic_checks,
)
from .schemas import (
    SUMMARY_DIMENSIONS,
    ChapterBriefV1,
    ChapterDeltaV1,
    ChapterStateLedgerV1,
    ChapterWorkingStateV1,
    ChunkAnalysisV1,
    DimensionAnalysisV1,
    DimensionReviewV1,
    IssueSeverity,
    ReviewDimension,
    ReviewIssueV1,
    ReviewReportV1,
    RunPlanV1,
    SourceSpanV1,
    StoryFactV1,
    StoryStateV1,
    SummaryDimension,
    TaskBudgetV1,
)
from .splitter import split_archive_chunks, split_model_chunks
from .storage import ArtifactStore, StoredObject, canonical_json_bytes


class OrchestrationError(RuntimeError):
    code = "orchestration_error"


class PlanApprovalRequired(OrchestrationError):
    code = "plan_approval_required"


class PlanHashMismatch(OrchestrationError):
    code = "plan_hash_mismatch"


class RunPaused(OrchestrationError):
    code = "run_paused"


class RunCancelled(OrchestrationError):
    code = "run_cancelled"


class RunNeedsHuman(OrchestrationError):
    code = "needs_human"


class IndeterminateTaskBlocked(RunNeedsHuman):
    code = "indeterminate_task"


class BudgetExhausted(OrchestrationError):
    code = "budget_exhausted"


class BranchHeadConflict(RunNeedsHuman):
    code = "branch_head_conflict"


class ImmutableCanonViolation(RunNeedsHuman):
    code = "immutable_canon_violation"


@dataclass(frozen=True, slots=True)
class AnalysisChunk:
    chunk_id: str
    text: str
    source_name: str
    start_offset: int
    end_offset: int
    sequence_index: int
    source_artifact_id: UUID | None = None
    previous_tail: str = ""


@dataclass(slots=True)
class RunSnapshot:
    id: UUID
    project_id: UUID
    kind: str
    status: str
    stage: str
    branch_name: str
    target_chars: int | None
    provider_preset: str
    user_requirements: str
    plan_artifact_id: UUID | None
    plan_hash: str | None
    budget: dict[str, Any]
    usage: dict[str, Any]
    pause_requested: bool
    cancel_requested: bool


@dataclass(slots=True)
class TaskRecord:
    id: UUID
    run_id: UUID
    logical_key: str
    operation: str
    status: str
    attempt_count: int
    max_attempts: int
    output_artifact_id: UUID | None
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    id: UUID
    relative_path: str
    media_type: str
    sha256: str


@dataclass(frozen=True, slots=True)
class PreparedArtifact:
    id: UUID
    kind: str
    stored: StoredObject
    source_artifact_ids: tuple[UUID, ...] = ()
    generation_config: Mapping[str, Any] | None = None
    task_id: UUID | None = None


@dataclass(slots=True)
class TaskResult:
    task_id: UUID
    artifact_id: UUID
    value: Any
    response: LLMResponse | None = None


class ReviewGroupIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    issue_id: str = Field(default_factory=lambda: f"issue_{uuid4().hex}")
    severity: str
    dimension: str
    title: str = ""
    description: str = ""
    rule: str = ""
    evidence: str = ""
    location: str = ""
    start_offset: int | None = Field(default=None, ge=0)
    end_offset: int | None = Field(default=None, ge=0)
    canon_fact_ids: list[str] = Field(default_factory=list)
    suspense_ids: list[str] = Field(default_factory=list)
    revision_instruction: str = ""

    @field_validator("severity")
    @classmethod
    def validate_severity(cls, value: str) -> str:
        normalized = value.lower().strip()
        if normalized not in {item.value for item in IssueSeverity}:
            raise ValueError(f"unknown issue severity: {value}")
        return normalized

    @field_validator("dimension")
    @classmethod
    def validate_dimension(cls, value: str) -> str:
        normalized = value.strip()
        if normalized not in {item.value for item in ReviewDimension}:
            raise ValueError(f"unknown review dimension: {value}")
        return normalized

    @model_validator(mode="after")
    def validate_offsets(self) -> ReviewGroupIssue:
        if self.start_offset is None and self.end_offset is not None:
            raise ValueError("start_offset is required when end_offset is present")
        if (
            self.start_offset is not None
            and self.end_offset is not None
            and self.end_offset <= self.start_offset
        ):
            raise ValueError("end_offset must be greater than start_offset")
        return self


class ReviewGroupDimension(BaseModel):
    model_config = ConfigDict(extra="forbid")

    score: float = Field(ge=0, le=100)
    summary: str = ""
    issues: list[ReviewGroupIssue] = Field(default_factory=list)


class ReviewGroupOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dimension_results: dict[str, ReviewGroupDimension]
    summary: str = ""


class ArbitrationOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["revise", "needs_human"]
    revision_manifest: list[dict[str, Any]] = Field(default_factory=list)
    unresolved_issue_ids: list[str] = Field(default_factory=list)


class GlobalReviewOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dimension_results: dict[str, ReviewGroupDimension]
    passed: bool = False
    summary: str = ""
    issues: list[str] = Field(default_factory=list)
    invalidated_chapter_ids: list[UUID] = Field(default_factory=list)
    blocker_count: int = Field(default=0, ge=0)
    major_count: int = Field(default=0, ge=0)
    weighted_score: float = Field(default=0, ge=0, le=100)

    @model_validator(mode="after")
    def calculate_global_gate(self) -> GlobalReviewOutput:
        expected = {item.value for item in ReviewDimension}
        actual = set(self.dimension_results)
        if actual != expected:
            raise ValueError(
                "global review must cover all nine dimensions; "
                f"missing={sorted(expected - actual)}, extra={sorted(actual - expected)}"
            )
        review_issues = [issue for result in self.dimension_results.values() for issue in result.issues]
        blocker_count = sum(issue.severity == IssueSeverity.BLOCKER.value for issue in review_issues)
        major_count = sum(issue.severity == IssueSeverity.MAJOR.value for issue in review_issues)
        total_weight = sum(DIMENSION_WEIGHTS[key] for key in expected)
        weighted_score = round(
            sum(self.dimension_results[key].score * DIMENSION_WEIGHTS[key] for key in expected)
            / total_weight,
            2,
        )
        passed = (
            blocker_count == 0
            and major_count == 0
            and all(result.score >= 85 for result in self.dimension_results.values())
            and weighted_score >= 90
            and not self.invalidated_chapter_ids
        )
        object.__setattr__(self, "blocker_count", blocker_count)
        object.__setattr__(self, "major_count", major_count)
        object.__setattr__(self, "weighted_score", weighted_score)
        object.__setattr__(self, "passed", passed)
        return self


def _mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if isinstance(value, BaseModel):
        return value.model_dump(mode="python")
    return {}


def _review_reference_ids(story_state: Any, ledger: ChapterStateLedgerV1) -> tuple[set[str], set[str]]:
    state = _mapping(story_state)

    def collect(items: Any, *keys: str) -> set[str]:
        result: set[str] = set()
        if not isinstance(items, list):
            return result
        for item in items:
            source = _mapping(item)
            for key in keys:
                value = source.get(key)
                if value:
                    result.add(str(value))
                    break
        return result

    canon_ids = collect(state.get("facts"), "fact_id", "id")
    canon_ids.update(collect(ledger.working_state.active_facts, "fact_id", "id"))
    suspense_ids = collect(state.get("mysteries"), "mystery_id", "id")
    suspense_ids.update(collect(state.get("foreshadowing"), "foreshadowing_id", "id"))
    suspense_ids.update(collect(ledger.working_state.mystery_states, "mystery_id", "id"))
    suspense_ids.update(collect(ledger.working_state.foreshadowing_states, "foreshadowing_id", "id"))
    return canon_ids, suspense_ids


def _valid_issue_offsets(
    start: int | None,
    end: int | None,
    text_length: int,
) -> tuple[int | None, int | None]:
    if start is None or end is None:
        return None, None
    if 0 <= start < end <= text_length:
        return start, end
    return None, None


class MemorySnapshotOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    end_state_summary: str
    character_states: list[dict[str, Any]] = Field(default_factory=list)
    location_states: list[dict[str, Any]] = Field(default_factory=list)
    relationship_states: list[dict[str, Any]] = Field(default_factory=list)
    conflict_states: list[dict[str, Any]] = Field(default_factory=list)
    mystery_states: list[dict[str, Any]] = Field(default_factory=list)
    foreshadowing_states: list[dict[str, Any]] = Field(default_factory=list)
    next_start_point: str


@dataclass(slots=True)
class ChapterRunResult:
    brief: ChapterBriefV1
    text: str
    text_artifact_id: UUID
    delta: ChapterDeltaV1
    report: ReviewReportV1
    gate: QualityGateResult
    revision_rounds: int


class OrchestrationRepository(Protocol):
    artifact_store: ArtifactStore

    def create_run(
        self,
        *,
        project_id: UUID,
        kind: str,
        branch_name: str,
        target_chars: int | None,
        provider_preset: str,
        user_requirements: str,
        budget: Mapping[str, Any],
    ) -> RunSnapshot: ...

    def get_run(self, run_id: UUID) -> RunSnapshot: ...

    def update_run(self, run_id: UUID, **changes: Any) -> None: ...

    def update_usage(self, run_id: UUID, usage: dict[str, Any]) -> None: ...

    def ensure_task(
        self,
        *,
        run_id: UUID,
        logical_key: str,
        operation: str,
        route: ModelRoute,
        sequence_index: int,
        context: dict[str, Any],
        input_artifact_ids: Sequence[UUID],
        dependencies: Sequence[UUID],
        budget: dict[str, Any],
        max_attempts: int,
    ) -> TaskRecord: ...

    def start_attempt(self, task_id: UUID, *, lease_owner: str) -> tuple[TaskRecord, UUID]: ...

    def mark_request_dispatched(
        self,
        task_id: UUID,
        attempt_id: UUID,
        *,
        request_hash: str,
    ) -> None: ...

    def mark_response_landed(
        self,
        task_id: UUID,
        attempt_id: UUID,
        artifact_id: UUID,
        response: LLMResponse,
        *,
        request: LLMRequest,
        input_hash: str,
        output_hash: str,
    ) -> None: ...

    def heartbeat_attempt(
        self,
        task_id: UUID,
        attempt_id: UUID,
        *,
        lease_owner: str,
    ) -> None: ...

    def finish_task(
        self, task_id: UUID, attempt_id: UUID, artifact_id: UUID, response: LLMResponse
    ) -> None: ...

    def fail_task(
        self,
        task_id: UUID,
        attempt_id: UUID,
        error: Exception,
        *,
        status: str,
    ) -> None: ...

    def retry_task(self, task_id: UUID, *, acknowledge_indeterminate: bool = False) -> None: ...

    def store_text(
        self,
        *,
        project_id: UUID,
        run_id: UUID,
        task_id: UUID | None,
        kind: str,
        text: str,
        source_artifact_ids: Sequence[UUID] = (),
        generation_config: Mapping[str, Any] | None = None,
        suffix: str = ".txt",
    ) -> ArtifactRecord: ...

    def store_json(
        self,
        *,
        project_id: UUID,
        run_id: UUID,
        task_id: UUID | None,
        kind: str,
        value: Any,
        source_artifact_ids: Sequence[UUID] = (),
        generation_config: Mapping[str, Any] | None = None,
    ) -> ArtifactRecord: ...

    def store_file(
        self,
        *,
        project_id: UUID,
        run_id: UUID,
        task_id: UUID | None,
        kind: str,
        path: Path | str,
        source_artifact_ids: Sequence[UUID] = (),
        generation_config: Mapping[str, Any] | None = None,
    ) -> ArtifactRecord: ...

    def read_text(self, artifact_id: UUID) -> str: ...

    def read_json(self, artifact_id: UUID) -> Any: ...

    def set_plan(self, run_id: UUID, artifact_id: UUID, plan_hash: str) -> None: ...

    def set_project_story_state(self, project_id: UUID, artifact_id: UUID) -> None: ...

    def ensure_branch_head(self, project_id: UUID, branch_name: str) -> UUID | None: ...

    def get_branch_head(self, project_id: UUID, branch_name: str) -> UUID | None: ...

    def acquire_branch_finalization_lock(
        self,
        project_id: UUID,
        *,
        branch_name: str,
        expected_artifact_id: UUID | None,
        run_id: UUID,
        lease_seconds: int = 900,
    ) -> UUID | None: ...

    def release_branch_finalization_lock(
        self,
        project_id: UUID,
        *,
        branch_name: str,
        run_id: UUID,
        lock_token: UUID,
    ) -> bool: ...

    def renew_branch_finalization_lock(
        self,
        project_id: UUID,
        *,
        branch_name: str,
        run_id: UUID,
        lock_token: UUID,
        lease_seconds: int = 900,
    ) -> bool: ...

    def promote_story_state(
        self,
        project_id: UUID,
        *,
        branch_name: str,
        expected_artifact_id: UUID | None,
        artifact_id: UUID,
        run_id: UUID,
        finalization_lock_token: UUID | None = None,
        publish_artifacts: Sequence[PreparedArtifact] = (),
    ) -> bool: ...

    def recover_interrupted(self) -> list[UUID]: ...


def _uuid(value: str | UUID | None) -> UUID | None:
    return UUID(str(value)) if value else None


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class _SceneSlice:
    index: int
    count: int
    target_chars: int
    required_beats: tuple[str, ...]


def _writer_output_limit(route: ModelRoute) -> int:
    provider_key = f"NOVEL_{route.provider.value.upper()}_WRITER_MAX_OUTPUT_TOKENS"
    default = 16_000 if route.provider == ProviderName.OPENAI else 8_000
    raw = os.getenv(provider_key, os.getenv("NOVEL_WRITER_MAX_OUTPUT_TOKENS", str(default)))
    try:
        return max(256, int(raw))
    except (TypeError, ValueError):
        return default


def _scene_slices(brief: ChapterBriefV1, max_output_tokens: int) -> list[_SceneSlice]:
    # Keep a conservative output reserve for Chinese prose and scene endings.
    max_scene_chars = max(500, math.floor(max_output_tokens * 1.05))
    count = max(1, math.ceil(brief.target_chars / max_scene_chars))
    count = min(count, max(1, brief.target_chars // 500))
    base, remainder = divmod(brief.target_chars, count)
    beats = tuple(brief.required_beats)
    slices: list[_SceneSlice] = []
    for index in range(count):
        start = math.floor(index * len(beats) / count)
        end = math.floor((index + 1) * len(beats) / count)
        slices.append(
            _SceneSlice(
                index=index + 1,
                count=count,
                target_chars=base + (1 if index < remainder else 0),
                required_beats=beats[start:end],
            )
        )
    return slices


def _scene_append_delta(existing: str, incoming: str, *, max_overlap: int = 2_000) -> tuple[str, int]:
    """Return only the new boundary text and the exact overlap length."""

    if not existing:
        return incoming, 0
    limit = min(len(existing), len(incoming), max_overlap)
    overlap = 0
    for size in range(limit, 11, -1):
        if existing.endswith(incoming[:size]):
            overlap = size
            break
    if overlap:
        return incoming[overlap:], overlap

    candidate = incoming.lstrip("\r\n")
    existing_title = next((line.strip() for line in existing.splitlines() if line.strip()), "")
    incoming_lines = candidate.splitlines(keepends=True)
    if incoming_lines and incoming_lines[0].strip() == existing_title and existing_title.startswith("#"):
        candidate = "".join(incoming_lines[1:]).lstrip("\r\n")
    separator = "" if existing.endswith(("\n", "\r")) or candidate.startswith(("\n", "\r")) else "\n\n"
    return separator + candidate, 0


def _safe_stream_chunks(delta: str, *, chunk_size: int = 4_096) -> list[str]:
    cleaned = "".join(char for char in delta if char in "\n\r\t" or ord(char) >= 32)
    return [cleaned[index : index + chunk_size] for index in range(0, len(cleaned), chunk_size)]


class _SceneStreamJoiner:
    """Buffer one scene boundary until overlap can be removed before publication."""

    def __init__(
        self,
        existing: str,
        emit: Callable[[str], Awaitable[None]],
        *,
        probe_chars: int = 2_000,
    ) -> None:
        self.existing = existing
        self.emit = emit
        self.probe_chars = probe_chars if existing else 0
        self.buffer = ""
        self.released = False
        self.appended_parts: list[str] = []
        self.overlap_chars = 0

    async def feed(self, delta: str) -> None:
        if self.released:
            self.appended_parts.append(delta)
            await self.emit(delta)
            return
        self.buffer += delta
        if len(self.buffer) >= self.probe_chars:
            await self._release()

    async def finish(self) -> None:
        if not self.released:
            await self._release()

    async def _release(self) -> None:
        appended, self.overlap_chars = _scene_append_delta(self.existing, self.buffer)
        self.released = True
        self.buffer = ""
        if appended:
            self.appended_parts.append(appended)
            await self.emit(appended)

    @property
    def appended_text(self) -> str:
        return "".join(self.appended_parts)


class SqlAlchemyOrchestrationRepository:
    """Small transactional index over the content-addressed artifact store."""

    def __init__(self, session_factory: sessionmaker[Session], artifact_store: ArtifactStore) -> None:
        self.session_factory = session_factory
        self.artifact_store = artifact_store

    @staticmethod
    def _snapshot(run: models.Run) -> RunSnapshot:
        return RunSnapshot(
            id=UUID(run.id),
            project_id=UUID(run.project_id),
            kind=run.kind,
            status=run.status,
            stage=run.stage,
            branch_name=run.branch_name,
            target_chars=run.target_chars,
            provider_preset=run.provider_preset,
            user_requirements=run.user_requirements,
            plan_artifact_id=_uuid(run.plan_artifact_id),
            plan_hash=run.plan_hash,
            budget=dict(run.budget_json or {}),
            usage=dict(run.usage_json or {}),
            pause_requested=run.pause_requested,
            cancel_requested=run.cancel_requested,
        )

    @staticmethod
    def _task_record(task: models.Task) -> TaskRecord:
        return TaskRecord(
            id=UUID(task.id),
            run_id=UUID(task.run_id),
            logical_key=task.logical_key,
            operation=task.operation,
            status=task.status,
            attempt_count=task.attempt_count,
            max_attempts=task.max_attempts,
            output_artifact_id=_uuid(task.output_artifact_id),
            error_code=task.error_code,
        )

    def get_run(self, run_id: UUID) -> RunSnapshot:
        with self.session_factory() as session:
            run = session.get(models.Run, str(run_id))
            if run is None:
                raise KeyError(f"run not found: {run_id}")
            return self._snapshot(run)

    def create_run(
        self,
        *,
        project_id: UUID,
        kind: str,
        branch_name: str,
        target_chars: int | None,
        provider_preset: str,
        user_requirements: str,
        budget: Mapping[str, Any],
    ) -> RunSnapshot:
        with self.session_factory.begin() as session:
            if session.get(models.Project, str(project_id)) is None:
                raise KeyError(f"project not found: {project_id}")
            run = models.Run(
                project_id=str(project_id),
                kind=kind,
                status="draft",
                stage="created",
                branch_name=branch_name,
                target_chars=target_chars,
                provider_preset=provider_preset,
                user_requirements=user_requirements,
                budget_json=_jsonable(budget),
                usage_json={},
            )
            session.add(run)
            session.flush()
            return self._snapshot(run)

    def update_run(self, run_id: UUID, **changes: Any) -> None:
        with self.session_factory.begin() as session:
            run = session.get(models.Run, str(run_id))
            if run is None:
                raise KeyError(f"run not found: {run_id}")
            for key, value in changes.items():
                if not hasattr(run, key):
                    raise AttributeError(f"Run has no field {key}")
                setattr(run, key, value)

    def update_usage(self, run_id: UUID, usage: dict[str, Any]) -> None:
        with self.session_factory.begin() as session:
            run = session.get(models.Run, str(run_id))
            if run is None:
                raise KeyError(f"run not found: {run_id}")
            merged = dict(run.usage_json or {})
            merged.update(_jsonable(usage))
            run.usage_json = merged

    def ensure_task(
        self,
        *,
        run_id: UUID,
        logical_key: str,
        operation: str,
        route: ModelRoute,
        sequence_index: int,
        context: dict[str, Any],
        input_artifact_ids: Sequence[UUID],
        dependencies: Sequence[UUID],
        budget: dict[str, Any],
        max_attempts: int,
    ) -> TaskRecord:
        with self.session_factory.begin() as session:
            task = session.scalar(
                select(models.Task).where(
                    models.Task.run_id == str(run_id), models.Task.logical_key == logical_key
                )
            )
            if task is None:
                task = models.Task(
                    run_id=str(run_id),
                    logical_key=logical_key,
                    operation=operation,
                    status="pending",
                    sequence_index=sequence_index,
                    provider=route.provider.value,
                    model=route.model,
                    input_artifact_ids_json=[str(item) for item in input_artifact_ids],
                    context_json=_jsonable(context),
                    dependency_task_ids_json=[str(item) for item in dependencies],
                    budget_json=_jsonable(budget),
                    max_attempts=max_attempts,
                )
                session.add(task)
                session.flush()
            return self._task_record(task)

    def start_attempt(self, task_id: UUID, *, lease_owner: str) -> tuple[TaskRecord, UUID]:
        now = datetime.now(UTC)
        with self.session_factory.begin() as session:
            task = session.get(models.Task, str(task_id))
            if task is None:
                raise KeyError(f"task not found: {task_id}")
            if task.status == "indeterminate":
                raise IndeterminateTaskBlocked("indeterminate task requires explicit acknowledgement")
            if task.status != "pending":
                raise OrchestrationError(f"task is not claimable: {task.status}")
            if task.attempt_count >= task.max_attempts:
                raise OrchestrationError("task retry limit reached")
            task.attempt_count += 1
            task.status = "running"
            task.lease_owner = lease_owner
            task.lease_expires_at = now + timedelta(minutes=5)
            task.heartbeat_at = now
            task.error_code = None
            task.error_message = None
            attempt = models.Attempt(
                task_id=task.id,
                attempt_no=task.attempt_count,
                status="started",
                phase="claimed",
            )
            session.add(attempt)
            session.flush()
            return self._task_record(task), UUID(attempt.id)

    def mark_request_dispatched(
        self,
        task_id: UUID,
        attempt_id: UUID,
        *,
        request_hash: str,
    ) -> None:
        with self.session_factory.begin() as session:
            task = session.get(models.Task, str(task_id))
            attempt = session.get(models.Attempt, str(attempt_id))
            if task is None or attempt is None:
                raise KeyError("task or attempt not found")
            if attempt.task_id != task.id or task.status != "running":
                raise OrchestrationError("attempt does not own the running task")
            if attempt.phase not in {"claimed", "request_dispatched"}:
                raise OrchestrationError(f"attempt cannot dispatch from phase: {attempt.phase}")
            now = datetime.now(UTC)
            attempt.phase = "request_dispatched"
            attempt.status = "request_dispatched"
            attempt.request_hash = request_hash
            attempt.dispatched_at = attempt.dispatched_at or now
            task.heartbeat_at = now
            task.lease_expires_at = now + timedelta(minutes=5)

    def mark_response_landed(
        self,
        task_id: UUID,
        attempt_id: UUID,
        artifact_id: UUID,
        response: LLMResponse,
        *,
        request: LLMRequest,
        input_hash: str,
        output_hash: str,
    ) -> None:
        with self.session_factory.begin() as session:
            task = session.get(models.Task, str(task_id))
            attempt = session.get(models.Attempt, str(attempt_id))
            artifact = session.get(models.Artifact, str(artifact_id))
            if task is None or attempt is None or artifact is None:
                raise KeyError("task, attempt or artifact not found")
            if attempt.task_id != task.id or task.status != "running":
                raise OrchestrationError("attempt does not own the running task")
            if artifact.task_id != task.id:
                raise OrchestrationError("response artifact does not belong to the task")
            if attempt.phase not in {"request_dispatched", "response_landed"}:
                raise OrchestrationError(f"attempt cannot land a response from phase: {attempt.phase}")
            now = datetime.now(UTC)
            attempt.phase = "response_landed"
            attempt.status = "response_landed"
            attempt.response_landed_at = now
            attempt.response_artifact_id = artifact.id
            attempt.provider_request_id = response.request_id
            attempt.finish_reason = response.finish_status
            attempt.input_tokens = response.usage.input_tokens
            attempt.output_tokens = response.usage.output_tokens
            attempt.cached_tokens = response.usage.cached_input_tokens
            attempt.cost_usd = response.cost_usd
            attempt.duration_ms = round(response.elapsed_seconds * 1000)
            task.output_artifact_id = artifact.id
            task.heartbeat_at = now
            task.lease_expires_at = now + timedelta(minutes=5)
            existing_call = session.scalar(
                select(models.ModelCall).where(models.ModelCall.attempt_id == attempt.id).limit(1)
            )
            if existing_call is None:
                session.add(
                    models.ModelCall(
                        run_id=task.run_id,
                        task_id=task.id,
                        attempt_id=attempt.id,
                        provider=response.provider.value,
                        model=response.model,
                        provider_request_id=response.request_id,
                        status="completed",
                        input_hash=input_hash,
                        output_hash=output_hash,
                        finish_status=response.finish_status,
                        input_tokens=response.usage.input_tokens,
                        output_tokens=response.usage.output_tokens,
                        cached_tokens=response.usage.cached_input_tokens,
                        cache_hit=response.usage.cached_input_tokens > 0,
                        cost_usd=response.cost_usd,
                        duration_ms=round(response.elapsed_seconds * 1000),
                        call_details_json=redacted_request_record(request, response),
                    )
                )

    def heartbeat_attempt(
        self,
        task_id: UUID,
        attempt_id: UUID,
        *,
        lease_owner: str,
    ) -> None:
        with self.session_factory.begin() as session:
            task = session.get(models.Task, str(task_id))
            attempt = session.get(models.Attempt, str(attempt_id))
            if task is None or attempt is None:
                raise KeyError("task or attempt not found")
            if (
                attempt.task_id != task.id
                or task.status != "running"
                or task.lease_owner != lease_owner
                or attempt.status not in {"started", "request_dispatched", "response_landed"}
            ):
                raise OrchestrationError("attempt lease is no longer owned by this worker")
            now = datetime.now(UTC)
            task.heartbeat_at = now
            task.lease_expires_at = now + timedelta(minutes=5)

    def finish_task(self, task_id: UUID, attempt_id: UUID, artifact_id: UUID, response: LLMResponse) -> None:
        with self.session_factory.begin() as session:
            task = session.get(models.Task, str(task_id))
            attempt = session.get(models.Attempt, str(attempt_id))
            if task is None or attempt is None:
                raise KeyError("task or attempt not found")
            if attempt.task_id != task.id or task.status != "running":
                raise OrchestrationError("attempt does not own the running task")
            task.status = "completed"
            task.output_artifact_id = str(artifact_id)
            task.lease_owner = None
            task.lease_expires_at = None
            attempt.status = "completed"
            attempt.phase = "completed"
            attempt.finished_at = datetime.now(UTC)
            attempt.response_artifact_id = str(artifact_id)
            attempt.provider_request_id = response.request_id
            attempt.finish_reason = response.finish_status
            attempt.input_tokens = response.usage.input_tokens
            attempt.output_tokens = response.usage.output_tokens
            attempt.cached_tokens = response.usage.cached_input_tokens
            attempt.cost_usd = response.cost_usd
            attempt.duration_ms = round(response.elapsed_seconds * 1000)

    def fail_task(
        self,
        task_id: UUID,
        attempt_id: UUID,
        error: Exception,
        *,
        status: str,
    ) -> None:
        with self.session_factory.begin() as session:
            task = session.get(models.Task, str(task_id))
            attempt = session.get(models.Attempt, str(attempt_id))
            if task is None or attempt is None:
                raise KeyError("task or attempt not found")
            if attempt.task_id != task.id or task.status != "running":
                raise OrchestrationError("attempt does not own the running task")
            code = getattr(error, "code", type(error).__name__)
            task.status = status
            task.error_code = str(code)
            task.error_message = str(error)
            task.lease_owner = None
            task.lease_expires_at = None
            attempt.status = status
            if attempt.phase == "claimed":
                attempt.phase = "failed_before_dispatch"
            attempt.finished_at = datetime.now(UTC)
            attempt.error_code = str(code)
            attempt.error_message = str(error)

    def retry_task(self, task_id: UUID, *, acknowledge_indeterminate: bool = False) -> None:
        with self.session_factory.begin() as session:
            task = session.get(models.Task, str(task_id))
            if task is None:
                raise KeyError(f"task not found: {task_id}")
            if task.status == "indeterminate" and not acknowledge_indeterminate:
                raise IndeterminateTaskBlocked("重试可能重复计费，必须显式确认")
            if task.status not in {"failed", "indeterminate", "needs_retry"}:
                raise OrchestrationError(f"task status cannot be retried: {task.status}")
            if task.attempt_count >= task.max_attempts:
                raise OrchestrationError("task retry limit reached")
            task.status = "pending"
            task.error_code = None
            task.error_message = None

    def _insert_artifact(
        self,
        *,
        project_id: UUID,
        run_id: UUID,
        task_id: UUID | None,
        kind: str,
        stored: Any,
        source_artifact_ids: Sequence[UUID],
        generation_config: Mapping[str, Any] | None,
    ) -> ArtifactRecord:
        with self.session_factory.begin() as session:
            previous_version = session.scalar(
                select(models.Artifact.version)
                .where(
                    models.Artifact.project_id == str(project_id),
                    models.Artifact.run_id == str(run_id),
                    models.Artifact.kind == kind,
                )
                .order_by(models.Artifact.version.desc())
                .limit(1)
            )
            configured_version = (generation_config or {}).get("artifact_version")
            artifact = models.Artifact(
                project_id=str(project_id),
                run_id=str(run_id),
                task_id=str(task_id) if task_id else None,
                kind=kind,
                version=(
                    max(1, int(configured_version))
                    if configured_version is not None
                    else int(previous_version or 0) + 1
                ),
                relative_path=stored.relative_path,
                sha256=stored.sha256,
                media_type=stored.media_type,
                byte_size=stored.byte_size,
                source_artifact_ids_json=[str(item) for item in source_artifact_ids],
                generation_config_json=_jsonable(generation_config or {}),
                immutable=True,
            )
            session.add(artifact)
            session.flush()
            return ArtifactRecord(
                id=UUID(artifact.id),
                relative_path=artifact.relative_path,
                media_type=artifact.media_type,
                sha256=artifact.sha256,
            )

    def store_text(
        self,
        *,
        project_id: UUID,
        run_id: UUID,
        task_id: UUID | None,
        kind: str,
        text: str,
        source_artifact_ids: Sequence[UUID] = (),
        generation_config: Mapping[str, Any] | None = None,
        suffix: str = ".txt",
    ) -> ArtifactRecord:
        media = "text/markdown; charset=utf-8" if suffix == ".md" else "text/plain; charset=utf-8"
        stored = self.artifact_store.put_text(text, suffix=suffix, media_type=media)
        return self._insert_artifact(
            project_id=project_id,
            run_id=run_id,
            task_id=task_id,
            kind=kind,
            stored=stored,
            source_artifact_ids=source_artifact_ids,
            generation_config=generation_config,
        )

    def store_json(
        self,
        *,
        project_id: UUID,
        run_id: UUID,
        task_id: UUID | None,
        kind: str,
        value: Any,
        source_artifact_ids: Sequence[UUID] = (),
        generation_config: Mapping[str, Any] | None = None,
    ) -> ArtifactRecord:
        stored = self.artifact_store.put_json(_jsonable(value))
        return self._insert_artifact(
            project_id=project_id,
            run_id=run_id,
            task_id=task_id,
            kind=kind,
            stored=stored,
            source_artifact_ids=source_artifact_ids,
            generation_config=generation_config,
        )

    def store_file(
        self,
        *,
        project_id: UUID,
        run_id: UUID,
        task_id: UUID | None,
        kind: str,
        path: Path | str,
        source_artifact_ids: Sequence[UUID] = (),
        generation_config: Mapping[str, Any] | None = None,
    ) -> ArtifactRecord:
        source = Path(path)
        media_types = {
            ".zip": "application/zip",
            ".json": "application/json; charset=utf-8",
            ".md": "text/markdown; charset=utf-8",
            ".txt": "text/plain; charset=utf-8",
        }
        stored = self.artifact_store.put_file(
            source,
            suffix=source.suffix or ".bin",
            media_type=media_types.get(source.suffix.lower(), "application/octet-stream"),
        )
        return self._insert_artifact(
            project_id=project_id,
            run_id=run_id,
            task_id=task_id,
            kind=kind,
            stored=stored,
            source_artifact_ids=source_artifact_ids,
            generation_config=generation_config,
        )

    def _artifact(self, artifact_id: UUID) -> models.Artifact:
        with self.session_factory() as session:
            artifact = session.get(models.Artifact, str(artifact_id))
            if artifact is None:
                raise KeyError(f"artifact not found: {artifact_id}")
            session.expunge(artifact)
            return artifact

    def read_text(self, artifact_id: UUID) -> str:
        return self.artifact_store.read_text(self._artifact(artifact_id).relative_path)

    def read_json(self, artifact_id: UUID) -> Any:
        return json.loads(self.read_text(artifact_id))

    def set_plan(self, run_id: UUID, artifact_id: UUID, plan_hash: str) -> None:
        self.update_run(
            run_id,
            plan_artifact_id=str(artifact_id),
            plan_hash=plan_hash,
            status=_APPROVAL_STATUS,
            stage="continuator_outline",
        )

    def set_project_story_state(self, project_id: UUID, artifact_id: UUID) -> None:
        with self.session_factory.begin() as session:
            project = session.get(models.Project, str(project_id))
            if project is None:
                raise KeyError(f"project not found: {project_id}")
            project.current_story_state_artifact_id = str(artifact_id)

    def ensure_branch_head(self, project_id: UUID, branch_name: str) -> UUID | None:
        with self.session_factory.begin() as session:
            project = session.get(models.Project, str(project_id))
            if project is None:
                raise KeyError(f"project not found: {project_id}")
            initial = project.current_story_state_artifact_id
            statement = (
                sqlite_insert(models.BranchHead)
                .values(
                    project_id=str(project_id),
                    branch_name=branch_name,
                    artifact_id=initial,
                    version=0,
                )
                .on_conflict_do_nothing(index_elements=["project_id", "branch_name"])
            )
            session.execute(statement)
            head = session.scalar(
                select(models.BranchHead).where(
                    models.BranchHead.project_id == str(project_id),
                    models.BranchHead.branch_name == branch_name,
                )
            )
            assert head is not None
            return _uuid(head.artifact_id)

    def get_branch_head(self, project_id: UUID, branch_name: str) -> UUID | None:
        self.ensure_branch_head(project_id, branch_name)
        with self.session_factory() as session:
            artifact_id = session.scalar(
                select(models.BranchHead.artifact_id).where(
                    models.BranchHead.project_id == str(project_id),
                    models.BranchHead.branch_name == branch_name,
                )
            )
            return _uuid(artifact_id)

    def acquire_branch_finalization_lock(
        self,
        project_id: UUID,
        *,
        branch_name: str,
        expected_artifact_id: UUID | None,
        run_id: UUID,
        lease_seconds: int = 900,
    ) -> UUID | None:
        """Claim a branch lease only while its head still matches the run base."""

        self.ensure_branch_head(project_id, branch_name)
        expected = str(expected_artifact_id) if expected_artifact_id else None
        now = datetime.now(UTC)
        expires_at = now + timedelta(seconds=max(1, int(lease_seconds)))
        token = uuid4()
        with self.session_factory.begin() as session:
            unlocked_or_expired = (
                models.BranchHead.finalization_owner_run_id.is_(None)
                | models.BranchHead.finalization_lock_token.is_(None)
                | models.BranchHead.finalization_lease_expires_at.is_(None)
                | (models.BranchHead.finalization_lease_expires_at <= now)
            )
            statement = (
                update(models.BranchHead)
                .where(
                    models.BranchHead.project_id == str(project_id),
                    models.BranchHead.branch_name == branch_name,
                    models.BranchHead.artifact_id == expected,
                    unlocked_or_expired,
                )
                .values(
                    finalization_owner_run_id=str(run_id),
                    finalization_lock_token=str(token),
                    finalization_lease_expires_at=expires_at,
                )
            )
            if session.execute(statement).rowcount == 1:
                return token
            return None

    def release_branch_finalization_lock(
        self,
        project_id: UUID,
        *,
        branch_name: str,
        run_id: UUID,
        lock_token: UUID,
    ) -> bool:
        with self.session_factory.begin() as session:
            statement = (
                update(models.BranchHead)
                .where(
                    models.BranchHead.project_id == str(project_id),
                    models.BranchHead.branch_name == branch_name,
                    models.BranchHead.finalization_owner_run_id == str(run_id),
                    models.BranchHead.finalization_lock_token == str(lock_token),
                )
                .values(
                    finalization_owner_run_id=None,
                    finalization_lock_token=None,
                    finalization_lease_expires_at=None,
                )
            )
            return session.execute(statement).rowcount == 1

    def renew_branch_finalization_lock(
        self,
        project_id: UUID,
        *,
        branch_name: str,
        run_id: UUID,
        lock_token: UUID,
        lease_seconds: int = 900,
    ) -> bool:
        now = datetime.now(UTC)
        expires_at = now + timedelta(seconds=max(1, int(lease_seconds)))
        with self.session_factory.begin() as session:
            statement = (
                update(models.BranchHead)
                .where(
                    models.BranchHead.project_id == str(project_id),
                    models.BranchHead.branch_name == branch_name,
                    models.BranchHead.finalization_owner_run_id == str(run_id),
                    models.BranchHead.finalization_lock_token == str(lock_token),
                    models.BranchHead.finalization_lease_expires_at > now,
                )
                .values(finalization_lease_expires_at=expires_at)
            )
            return session.execute(statement).rowcount == 1

    def promote_story_state(
        self,
        project_id: UUID,
        *,
        branch_name: str,
        expected_artifact_id: UUID | None,
        artifact_id: UUID,
        run_id: UUID,
        finalization_lock_token: UUID | None = None,
        publish_artifacts: Sequence[PreparedArtifact] = (),
    ) -> bool:
        """Compare-and-swap the formal memory head for a branch.

        SQLite performs the predicate and write in one statement, so two runs
        created from the same head cannot both win final promotion.
        """

        self.ensure_branch_head(project_id, branch_name)
        expected = str(expected_artifact_id) if expected_artifact_id else None
        now = datetime.now(UTC)
        with self.session_factory.begin() as session:
            if finalization_lock_token is None:
                lock_clause = (
                    models.BranchHead.finalization_owner_run_id.is_(None)
                    | models.BranchHead.finalization_lock_token.is_(None)
                    | models.BranchHead.finalization_lease_expires_at.is_(None)
                    | (models.BranchHead.finalization_lease_expires_at <= now)
                )
            else:
                lock_clause = (
                    (models.BranchHead.finalization_owner_run_id == str(run_id))
                    & (models.BranchHead.finalization_lock_token == str(finalization_lock_token))
                    & (models.BranchHead.finalization_lease_expires_at > now)
                )
            statement = (
                update(models.BranchHead)
                .where(
                    models.BranchHead.project_id == str(project_id),
                    models.BranchHead.branch_name == branch_name,
                    models.BranchHead.artifact_id == expected,
                    lock_clause,
                )
                .values(
                    artifact_id=str(artifact_id),
                    version=models.BranchHead.version + 1,
                    promoted_by_run_id=str(run_id),
                    finalization_owner_run_id=None,
                    finalization_lock_token=None,
                    finalization_lease_expires_at=None,
                )
            )
            if session.execute(statement).rowcount != 1:
                head = session.scalar(
                    select(models.BranchHead).where(
                        models.BranchHead.project_id == str(project_id),
                        models.BranchHead.branch_name == branch_name,
                    )
                )
                if (
                    head is None
                    or head.artifact_id != str(artifact_id)
                    or head.promoted_by_run_id != str(run_id)
                ):
                    return False
                return True
            prepared_ids: set[UUID] = set()
            latest_versions: dict[str, int] = {}
            for prepared in publish_artifacts:
                if prepared.id in prepared_ids:
                    raise OrchestrationError(f"duplicate prepared artifact id: {prepared.id}")
                if prepared.kind != "delivery_manifest" and not prepared.kind.startswith("delivery_"):
                    raise OrchestrationError(f"invalid final delivery kind: {prepared.kind}")
                prepared_ids.add(prepared.id)
                if prepared.kind not in latest_versions:
                    previous_version = session.scalar(
                        select(models.Artifact.version)
                        .where(
                            models.Artifact.project_id == str(project_id),
                            models.Artifact.run_id == str(run_id),
                            models.Artifact.kind == prepared.kind,
                        )
                        .order_by(models.Artifact.version.desc())
                        .limit(1)
                    )
                    latest_versions[prepared.kind] = int(previous_version or 0)
                configured_version = (prepared.generation_config or {}).get("artifact_version")
                version = (
                    max(1, int(configured_version))
                    if configured_version is not None
                    else latest_versions[prepared.kind] + 1
                )
                latest_versions[prepared.kind] = max(latest_versions[prepared.kind], version)
                session.add(
                    models.Artifact(
                        id=str(prepared.id),
                        project_id=str(project_id),
                        run_id=str(run_id),
                        task_id=str(prepared.task_id) if prepared.task_id else None,
                        kind=prepared.kind,
                        version=version,
                        relative_path=prepared.stored.relative_path,
                        sha256=prepared.stored.sha256,
                        media_type=prepared.stored.media_type,
                        byte_size=prepared.stored.byte_size,
                        source_artifact_ids_json=[str(item) for item in prepared.source_artifact_ids],
                        generation_config_json=_jsonable(prepared.generation_config or {}),
                        immutable=True,
                    )
                )
            project = session.get(models.Project, str(project_id))
            assert project is not None
            project.current_story_state_artifact_id = str(artifact_id)
            project.active_branch = branch_name
        self.artifact_store.write_pointer(
            Path("branches") / str(project_id) / branch_name,
            {
                "schema_version": "1.0",
                "artifact_id": str(artifact_id),
                "previous_artifact_id": expected,
                "run_id": str(run_id),
                "promoted_at": datetime.now(UTC).isoformat(),
            },
        )
        return True

    def record_model_call(
        self,
        *,
        run_id: UUID,
        task_id: UUID,
        attempt_id: UUID,
        request: LLMRequest,
        response: LLMResponse,
        input_hash: str,
        output_hash: str,
    ) -> None:
        details = redacted_request_record(request, response)
        with self.session_factory.begin() as session:
            session.add(
                models.ModelCall(
                    run_id=str(run_id),
                    task_id=str(task_id),
                    attempt_id=str(attempt_id),
                    provider=response.provider.value,
                    model=response.model,
                    provider_request_id=response.request_id,
                    status="completed",
                    input_hash=input_hash,
                    output_hash=output_hash,
                    finish_status=response.finish_status,
                    input_tokens=response.usage.input_tokens,
                    output_tokens=response.usage.output_tokens,
                    cached_tokens=response.usage.cached_input_tokens,
                    cache_hit=response.usage.cached_input_tokens > 0,
                    cost_usd=response.cost_usd,
                    duration_ms=round(response.elapsed_seconds * 1000),
                    call_details_json=details,
                )
            )

    def recover_interrupted(self) -> list[UUID]:
        resumable: list[UUID] = []
        with self.session_factory.begin() as session:
            running_tasks = session.scalars(select(models.Task).where(models.Task.status == "running")).all()
            indeterminate_runs: set[str] = set()
            for task in running_tasks:
                attempt = session.scalar(
                    select(models.Attempt)
                    .where(models.Attempt.task_id == task.id)
                    .order_by(models.Attempt.attempt_no.desc())
                    .limit(1)
                )
                landed_artifact = None
                candidate_id = (
                    attempt.response_artifact_id if attempt is not None else None
                ) or task.output_artifact_id
                if candidate_id:
                    landed_artifact = session.get(models.Artifact, candidate_id)
                if landed_artifact is None and attempt is not None:
                    landed_artifact = session.scalar(
                        select(models.Artifact)
                        .where(
                            models.Artifact.task_id == task.id,
                            models.Artifact.created_at >= attempt.started_at,
                        )
                        .order_by(models.Artifact.created_at.desc(), models.Artifact.id.desc())
                        .limit(1)
                    )

                if landed_artifact is not None:
                    task.status = "completed"
                    task.output_artifact_id = landed_artifact.id
                    task.error_code = None
                    task.error_message = None
                    if attempt is not None:
                        config = landed_artifact.generation_config_json or {}
                        attempt.status = "completed"
                        attempt.phase = "completed"
                        attempt.response_artifact_id = landed_artifact.id
                        attempt.provider_request_id = attempt.provider_request_id or config.get("request_id")
                        attempt.finish_reason = attempt.finish_reason or config.get("finish_status")
                        attempt.input_tokens = int(config.get("input_tokens", attempt.input_tokens) or 0)
                        attempt.output_tokens = int(config.get("output_tokens", attempt.output_tokens) or 0)
                        attempt.cached_tokens = int(config.get("cached_tokens", attempt.cached_tokens) or 0)
                        attempt.cost_usd = float(config.get("cost_usd", attempt.cost_usd) or 0)
                        attempt.duration_ms = int(config.get("duration_ms", attempt.duration_ms or 0) or 0)
                        attempt.finished_at = attempt.finished_at or datetime.now(UTC)
                        existing_call = session.scalar(
                            select(models.ModelCall).where(models.ModelCall.attempt_id == attempt.id).limit(1)
                        )
                        if existing_call is None:
                            provider = str(config.get("provider") or task.provider or "unknown")
                            model = str(config.get("model") or task.model or "unknown")
                            session.add(
                                models.ModelCall(
                                    run_id=task.run_id,
                                    task_id=task.id,
                                    attempt_id=attempt.id,
                                    provider=provider,
                                    model=model,
                                    provider_request_id=attempt.provider_request_id,
                                    status="recovered_response",
                                    input_hash=attempt.request_hash or ("0" * 64),
                                    output_hash=landed_artifact.sha256,
                                    finish_status=attempt.finish_reason,
                                    input_tokens=attempt.input_tokens,
                                    output_tokens=attempt.output_tokens,
                                    cached_tokens=attempt.cached_tokens,
                                    cache_hit=attempt.cached_tokens > 0,
                                    cost_usd=attempt.cost_usd,
                                    duration_ms=attempt.duration_ms,
                                    call_details_json={
                                        "recovered_from_artifact": True,
                                        "prompt_name": config.get("prompt_name"),
                                        "prompt_version": config.get("prompt_version"),
                                        "prompt_sha256": config.get("prompt_sha256"),
                                        "provider_attempts": config.get("provider_attempts"),
                                    },
                                )
                            )
                elif attempt is not None and attempt.phase == "claimed":
                    task.status = "pending"
                    task.error_code = None
                    task.error_message = None
                    attempt.status = "interrupted_before_dispatch"
                    attempt.finished_at = datetime.now(UTC)
                else:
                    task.status = "indeterminate"
                    task.error_code = "process_interrupted"
                    task.error_message = "进程在模型调用期间中断；是否计费无法确定"
                    indeterminate_runs.add(task.run_id)
                    if attempt is not None:
                        attempt.status = "indeterminate"
                        attempt.finished_at = datetime.now(UTC)
                task.lease_owner = None
                task.lease_expires_at = None
            runs = session.scalars(
                select(models.Run).where(
                    models.Run.status.in_(["approved", "queued", "running", "pausing", "cancelling"])
                )
            ).all()
            for run in runs:
                if run.cancel_requested or run.status == "cancelling":
                    run.status = "cancelled"
                    run.stage = "cancelled"
                    run.finished_at = run.finished_at or datetime.now(UTC)
                elif run.pause_requested or run.status == "pausing":
                    run.status = "paused"
                    run.stage = "paused"
                elif run.id in indeterminate_runs:
                    run.status = "needs_human"
                    run.stage = "indeterminate_task"
                else:
                    run.status = "queued"
                    resumable.append(UUID(run.id))
        return resumable


@dataclass(frozen=True, slots=True)
class BudgetLimits:
    max_cost_usd: float
    max_tokens: int
    max_calls: int
    max_wall_time_seconds: int

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> BudgetLimits:
        raw_calls = value.get("max_calls")
        return cls(
            max_cost_usd=float(value.get("max_cost_usd", value.get("max_cost", 0)) or 0),
            max_tokens=int(value.get("max_tokens", 0) or 0),
            max_calls=max(1, int(200 if raw_calls is None else raw_calls)),
            max_wall_time_seconds=int(
                value.get("max_wall_time_seconds", value.get("max_wall_time", 86_400)) or 86_400
            ),
        )


@dataclass(frozen=True, slots=True)
class BudgetReservation:
    reservation_id: str
    calls: int
    tokens: int
    cost_usd: float


class BudgetManager:
    """Transaction-like in-process reservation for parallel reviewer calls."""

    def __init__(
        self,
        limits: BudgetLimits,
        initial: Mapping[str, Any] | None = None,
        *,
        persist: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        state = dict(initial or {})
        self.limits = limits
        self.used_calls = int(state.get("used_calls", state.get("calls", 0)) or 0)
        self.used_tokens = int(state.get("used_tokens", state.get("tokens", 0)) or 0)
        self.used_cost_usd = float(state.get("used_cost_usd", state.get("cost_usd", 0)) or 0)
        # A persisted reservation has no live owner after process recovery.  It
        # represents a possibly billed call, so conservatively convert it to
        # usage instead of leaking an un-releasable reservation or replaying it.
        self.used_calls += int(state.get("reserved_calls", 0) or 0)
        self.used_tokens += int(state.get("reserved_tokens", 0) or 0)
        self.used_cost_usd += float(state.get("reserved_cost_usd", 0) or 0)
        self.reserved_calls = 0
        self.reserved_tokens = 0
        self.reserved_cost_usd = 0.0
        self.initial_elapsed_seconds = float(state.get("elapsed_wall_time_seconds", 0.0) or 0.0)
        self.started_monotonic = time.monotonic()
        self._persist = persist
        self._lock = asyncio.Lock()
        self._reservations: dict[str, BudgetReservation] = {}

    def snapshot(self) -> dict[str, Any]:
        return {
            "used_calls": self.used_calls,
            "used_tokens": self.used_tokens,
            "used_cost_usd": round(self.used_cost_usd, 8),
            "reserved_calls": self.reserved_calls,
            "reserved_tokens": self.reserved_tokens,
            "reserved_cost_usd": round(self.reserved_cost_usd, 8),
            "elapsed_wall_time_seconds": round(
                self.initial_elapsed_seconds + time.monotonic() - self.started_monotonic,
                3,
            ),
        }

    def _save(self) -> None:
        if self._persist:
            self._persist(self.snapshot())

    async def reserve(self, *, tokens: int, cost_usd: float, calls: int = 1) -> BudgetReservation:
        async with self._lock:
            elapsed = self.initial_elapsed_seconds + time.monotonic() - self.started_monotonic
            if elapsed > self.limits.max_wall_time_seconds:
                raise BudgetExhausted("运行超过 max_wall_time")
            requested_calls = max(1, calls)
            calls_total = self.used_calls + self.reserved_calls + requested_calls
            token_total = self.used_tokens + self.reserved_tokens + max(0, tokens)
            cost_total = self.used_cost_usd + self.reserved_cost_usd + max(0.0, cost_usd)
            if calls_total > self.limits.max_calls:
                raise BudgetExhausted("模型调用次数预算已耗尽")
            if self.limits.max_tokens and token_total > self.limits.max_tokens:
                raise BudgetExhausted("Token 预算已耗尽")
            if self.limits.max_cost_usd and cost_total > self.limits.max_cost_usd:
                raise BudgetExhausted("费用预算已耗尽")
            reservation = BudgetReservation(
                uuid4().hex,
                requested_calls,
                max(0, tokens),
                max(0.0, cost_usd),
            )
            self._reservations[reservation.reservation_id] = reservation
            self.reserved_calls += reservation.calls
            self.reserved_tokens += reservation.tokens
            self.reserved_cost_usd += reservation.cost_usd
            self._save()
            return reservation

    async def commit(
        self,
        reservation: BudgetReservation,
        *,
        tokens: int,
        cost_usd: float,
        calls: int = 1,
    ) -> None:
        async with self._lock:
            if self._reservations.pop(reservation.reservation_id, None) is None:
                return
            self.reserved_calls -= reservation.calls
            self.reserved_tokens -= reservation.tokens
            self.reserved_cost_usd -= reservation.cost_usd
            self.used_calls += max(1, calls)
            self.used_tokens += max(0, tokens)
            self.used_cost_usd += max(0.0, cost_usd)
            self._save()

    async def release(self, reservation: BudgetReservation) -> None:
        async with self._lock:
            if self._reservations.pop(reservation.reservation_id, None) is None:
                return
            self.reserved_calls -= reservation.calls
            self.reserved_tokens -= reservation.tokens
            self.reserved_cost_usd -= reservation.cost_usd
            self._save()

    @property
    def usage_fraction(self) -> float:
        fractions = [
            (self.used_calls + self.reserved_calls) / self.limits.max_calls,
        ]
        if self.limits.max_tokens:
            fractions.append((self.used_tokens + self.reserved_tokens) / self.limits.max_tokens)
        if self.limits.max_cost_usd:
            fractions.append((self.used_cost_usd + self.reserved_cost_usd) / self.limits.max_cost_usd)
        return max(fractions)


class EventBroker:
    def __init__(self) -> None:
        self._subscribers: dict[UUID, set[asyncio.Queue[dict[str, Any]]]] = defaultdict(set)
        self._sequences: dict[UUID, int] = defaultdict(int)

    async def publish(self, run_id: UUID, event: str, **payload: Any) -> None:
        self._sequences[run_id] += 1
        message = {
            "sequence": self._sequences[run_id],
            "run_id": str(run_id),
            "event": event,
            "at": datetime.now(UTC).isoformat(),
            "payload": _jsonable(payload),
        }
        for queue in tuple(self._subscribers[run_id]):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(message)

    async def subscribe(self, run_id: UUID) -> AsyncIterator[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=200)
        self._subscribers[run_id].add(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            self._subscribers[run_id].discard(queue)


_TERMINAL_RUN_STATUSES = {"completed", "cancelled", "needs_human", "failed"}
_APPROVAL_STATUS = "awaiting_outline_approval"
_WORKER_STOP = object()


def _plan_digest(plan: RunPlanV1 | Mapping[str, Any]) -> str:
    value = plan.model_dump(mode="json") if isinstance(plan, BaseModel) else _jsonable(plan)
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _validated_branch_name(value: str) -> str:
    normalized = value.strip()
    if (
        normalized in {"", ".", ".."}
        or len(normalized) > 120
        or re.fullmatch(r"[\w.-]+", normalized, flags=re.UNICODE) is None
    ):
        raise ValueError("branch_name may contain only letters, numbers, underscore, dot and hyphen")
    return normalized


def _model_cost_usd(route: ModelRoute, usage: ModelUsage) -> float:
    """Estimate cost deterministically when vendors do not return billed cost.

    Values are deliberately configurable.  The defaults are conservative
    planning rates per million tokens, not a claim about a vendor invoice.
    """

    prefix = "NOVEL_OPENAI" if route.provider == ProviderName.OPENAI else "NOVEL_DEEPSEEK"
    default_input = 15.0 if route.provider == ProviderName.OPENAI else 3.0
    default_output = 60.0 if route.provider == ProviderName.OPENAI else 12.0
    input_rate = float(os.getenv(f"{prefix}_INPUT_USD_PER_MILLION", default_input))
    output_rate = float(os.getenv(f"{prefix}_OUTPUT_USD_PER_MILLION", default_output))
    uncached = max(0, usage.input_tokens - usage.cached_input_tokens)
    cached = max(0, usage.cached_input_tokens)
    # Cached tokens use a conservative 50% estimate unless configured by a
    # future provider-specific billing adapter.
    return round(
        (uncached * input_rate + cached * input_rate * 0.5 + usage.output_tokens * output_rate) / 1_000_000,
        8,
    )


def _serialize_routes(routes: Mapping[AgentRole, ModelRoute]) -> dict[str, dict[str, str]]:
    return {
        role.value: {"provider": route.provider.value, "model": route.model} for role, route in routes.items()
    }


def _plan_route_manifest(routes: Mapping[AgentRole, ModelRoute]) -> dict[str, str]:
    return {role.value: f"{route.provider.value}:{route.model}" for role, route in routes.items()}


def _deserialize_route(value: Any) -> ModelRoute:
    if isinstance(value, Mapping):
        provider = ProviderName(str(value.get("provider") or ""))
        model = str(value.get("model") or "")
    else:
        provider_value, separator, model = str(value).partition(":")
        if not separator:
            raise ValueError(f"invalid frozen model route: {value}")
        provider = ProviderName(provider_value)
    if not model:
        raise ValueError(f"invalid frozen model route: {value}")
    return ModelRoute(provider, model)


def _immutable_canon_violations(
    previous: StoryStateV1,
    candidate: StoryStateV1,
    *,
    allowed_fact_ids: set[str],
) -> list[str]:
    protected = {
        fact.fact_id: fact
        for fact in previous.facts
        if fact.immutable_canon and fact.fact_id not in allowed_fact_ids
    }
    candidate_facts = {fact.fact_id: fact for fact in candidate.facts}
    violations: list[str] = []
    semantic_fields = ("category", "statement", "assertion_kind", "immutable_canon")
    for fact_id, old_fact in protected.items():
        new_fact = candidate_facts.get(fact_id)
        if new_fact is None:
            violations.append(f"不可变事实被删除：{fact_id}")
            continue
        if any(getattr(old_fact, field) != getattr(new_fact, field) for field in semantic_fields):
            violations.append(f"不可变事实被改写：{fact_id}")

    protected_ids = set(protected)
    old_entities = {
        entity.entity_id: entity
        for entity in previous.entities
        if protected_ids.intersection(entity.fact_ids)
    }
    new_entities = {entity.entity_id: entity for entity in candidate.entities}
    for entity_id, old_entity in old_entities.items():
        new_entity = new_entities.get(entity_id)
        if new_entity is None:
            violations.append(f"不可变事实关联实体被删除：{entity_id}")
        elif (old_entity.kind, old_entity.canonical_name) != (
            new_entity.kind,
            new_entity.canonical_name,
        ):
            violations.append(f"不可变事实关联实体被改写：{entity_id}")

    old_rules = {
        rule.rule_id: rule for rule in previous.world_rules if protected_ids.intersection(rule.fact_ids)
    }
    new_rules = {rule.rule_id: rule for rule in candidate.world_rules}
    for rule_id, old_rule in old_rules.items():
        new_rule = new_rules.get(rule_id)
        if new_rule is None:
            violations.append(f"不可变世界规则被删除：{rule_id}")
        elif (
            old_rule.statement,
            old_rule.limitations,
            old_rule.costs,
            old_rule.exceptions,
        ) != (
            new_rule.statement,
            new_rule.limitations,
            new_rule.costs,
            new_rule.exceptions,
        ):
            violations.append(f"不可变世界规则被改写：{rule_id}")
    return violations


def render_story_state_views(state: StoryStateV1) -> dict[str, tuple[str, str]]:
    """Render full and compact nine-dimension views from one canonical state."""

    payloads: dict[SummaryDimension, Any] = {
        SummaryDimension.CORE_SUMMARY: state.facts,
        SummaryDimension.STORY_OUTLINE: state.timeline,
        SummaryDimension.CHARACTERS: {
            "entities": [item for item in state.entities if item.kind == "character"],
            "states": state.characters,
        },
        SummaryDimension.CONFLICTS: state.conflicts,
        SummaryDimension.CORE_MYSTERIES: state.mysteries,
        SummaryDimension.WORLDBUILDING: {
            "rules": state.world_rules,
            "locations": state.locations,
            "entities": [item for item in state.entities if item.kind != "character"],
        },
        SummaryDimension.NARRATIVE_POV: state.narrative_profile,
        SummaryDimension.PACING: state.pacing_profile,
        SummaryDimension.FORESHADOWING: state.foreshadowing,
    }

    def compact(value: Any) -> Any:
        value = _jsonable(value)
        if isinstance(value, list):
            return [compact(item) for item in value]
        if isinstance(value, dict):
            identity_keys = (
                "fact_id",
                "entity_id",
                "event_id",
                "conflict_id",
                "mystery_id",
                "foreshadowing_id",
                "rule_id",
                "location_id",
                "fact_ids",
            )
            descriptive_keys = (
                "statement",
                "canonical_name",
                "event",
                "description",
                "label",
                "name",
                "status",
                "summary",
                "person",
                "dominant_rhythm",
            )
            selected = {
                key: value[key]
                for key in (*identity_keys, *descriptive_keys)
                if key in value and value[key] not in (None, "", [], {})
            }
            if selected:
                return selected
            return {key: compact(item) for key, item in value.items()}
        return value

    rendered: dict[str, tuple[str, str]] = {}
    for dimension in SUMMARY_DIMENSIONS:
        payload = _jsonable(payloads[dimension])
        title = dimension.value
        full = (
            f"# {title}（完全版）\n\n"
            + "```json\n"
            + json.dumps(payload, ensure_ascii=False, indent=2)
            + "\n```\n"
        )
        brief = (
            f"# {title}（精简版）\n\n"
            + "```json\n"
            + json.dumps(compact(payload), ensure_ascii=False, indent=2)
            + "\n```\n"
        )
        rendered[dimension.value] = (full, brief)
    return rendered


def _advance_chapter_state_ledger(
    ledger: ChapterStateLedgerV1,
    delta: ChapterDeltaV1,
    *,
    previous_ledger_artifact_id: UUID,
) -> ChapterStateLedgerV1:
    """Fold one accepted delta without asking an LLM to rewrite continuity."""

    current = ledger.working_state
    subject_fields = {subject_id: dict(fields) for subject_id, fields in current.subject_fields.items()}
    for change in delta.state_changes:
        subject_fields.setdefault(change.subject_id, {})[change.field] = change.after

    timeline_by_id = {item.event_id: item for item in current.timeline_events}
    for event in delta.timeline_events:
        timeline_by_id[event.event_id] = event

    resolved_fact_ids = sorted(set(current.resolved_fact_ids) | set(delta.resolved_fact_ids))
    active_facts = {item.fact_id: item for item in current.active_facts}
    for fact in delta.new_facts:
        active_facts[fact.fact_id] = fact
    for fact_id in resolved_fact_ids:
        active_facts.pop(fact_id, None)

    mysteries = {item.mystery_id: item for item in current.mystery_states}
    for mystery in delta.mystery_updates:
        mysteries[mystery.mystery_id] = mystery

    foreshadowing = {item.foreshadowing_id: item for item in current.foreshadowing_states}
    for item in delta.foreshadowing_updates:
        foreshadowing[item.foreshadowing_id] = item

    working_state = ChapterWorkingStateV1(
        subject_fields=subject_fields,
        timeline_events=list(timeline_by_id.values()),
        active_facts=list(active_facts.values()),
        resolved_fact_ids=resolved_fact_ids,
        mystery_states=list(mysteries.values()),
        foreshadowing_states=list(foreshadowing.values()),
        end_state_summaries=[*current.end_state_summaries, delta.end_state_summary],
    )
    return ChapterStateLedgerV1(
        run_id=ledger.run_id,
        project_id=ledger.project_id,
        branch_name=ledger.branch_name,
        revision=ledger.revision + 1,
        base_story_state_artifact_id=ledger.base_story_state_artifact_id,
        previous_ledger_artifact_id=previous_ledger_artifact_id,
        chapter_deltas=[*ledger.chapter_deltas, delta],
        working_state=working_state,
    )


class OrchestratorRuntime:
    """Deterministic, resumable owner of the multi-agent execution loop."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        artifact_store: ArtifactStore,
        *,
        provider_registry: ProviderRegistry | None = None,
        prompt_store: PromptStore | None = None,
        demo_mode: bool | None = None,
        delivery_root: Path | str | None = None,
        worker_count: int = 1,
    ) -> None:
        self.repository = SqlAlchemyOrchestrationRepository(session_factory, artifact_store)
        self.session_factory = session_factory
        self.artifact_store = artifact_store
        self.providers = provider_registry or ProviderRegistry()
        self.prompts = prompt_store or PromptStore()
        self.demo_mode = (
            bool(demo_mode)
            if demo_mode is not None
            else provider_registry is None and os.getenv("NOVEL_TEST_FAKE_PROVIDER") == "1"
        )
        self.delivery_root = Path(delivery_root) if delivery_root else artifact_store.root / "deliveries"
        self.worker_count = max(1, worker_count)
        self.events = EventBroker()
        self._queue: asyncio.Queue[UUID | object] = asyncio.Queue()
        self._queued: set[UUID] = set()
        self._queue_lock = asyncio.Lock()
        self._workers: list[asyncio.Task[None]] = []
        self._active: dict[UUID, asyncio.Task[None]] = {}
        self._budget_managers: dict[UUID, BudgetManager] = {}
        self._budget_warned: set[UUID] = set()
        self._execution_config_cache: dict[UUID, dict[str, Any]] = {}
        self._frozen_prompt_stores: dict[UUID, PromptStore] = {}
        self._finalization_lock_tokens: dict[UUID, UUID] = {}
        self._force_cancelled: set[UUID] = set()
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        resumable = self.repository.recover_interrupted()
        self._workers = [
            asyncio.create_task(self._worker_loop(index), name=f"novel-worker-{index}")
            for index in range(self.worker_count)
        ]
        for run_id in resumable:
            await self.enqueue(run_id)

    async def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        active = tuple(self._active.values())
        for task in active:
            task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)
        for _ in self._workers:
            await self._queue.put(_WORKER_STOP)
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()

    async def create_continuation_run(
        self,
        *,
        project_id: UUID,
        target_chars: int = 40_000,
        branch_name: str = "main",
        provider_preset: str = "hybrid-quality",
        user_requirements: str = "",
        budget: Mapping[str, Any] | None = None,
    ) -> RunSnapshot:
        if not 4_000 <= target_chars <= 150_000:
            raise ValueError("target_chars must be between 4,000 and 150,000")
        branch_name = _validated_branch_name(branch_name)
        if provider_preset not in ROUTING_PRESETS:
            raise ValueError(f"unknown provider preset: {provider_preset}")
        limits = dict(
            budget
            or {
                "max_cost_usd": 0.0,
                "max_tokens": 1_500_000,
                "max_calls": 240,
                "max_wall_time_seconds": 86_400,
                "max_task_attempts": 3,
                "max_revision_rounds": 3,
            }
        )
        snapshot = self.repository.create_run(
            project_id=project_id,
            kind="continuation",
            branch_name=branch_name,
            target_chars=target_chars,
            provider_preset=provider_preset,
            user_requirements=user_requirements,
            budget=limits,
        )
        base = self.repository.get_branch_head(project_id, branch_name)
        self.repository.update_usage(
            snapshot.id,
            {
                "module": "continuator",
                "base_story_state_artifact_id": str(base) if base else None,
                "outline_revision": 0,
            },
        )
        self.repository.update_run(snapshot.id, status="queued", stage="continuator_outline")
        await self.enqueue(snapshot.id)
        return self.repository.get_run(snapshot.id)

    async def create_analysis_run(
        self,
        *,
        project_id: UUID,
        branch_name: str = "main",
        provider_preset: str = "hybrid-quality",
        budget: Mapping[str, Any] | None = None,
        splitter_enabled: bool = True,
    ) -> RunSnapshot:
        branch_name = _validated_branch_name(branch_name)
        if provider_preset not in ROUTING_PRESETS:
            raise ValueError(f"unknown provider preset: {provider_preset}")
        limits = dict(
            budget
            or {
                "max_cost_usd": 0.0,
                "max_tokens": 2_000_000,
                "max_calls": 250,
                "max_wall_time_seconds": 86_400,
                "max_task_attempts": 3,
            }
        )
        snapshot = self.repository.create_run(
            project_id=project_id,
            kind="analysis",
            branch_name=branch_name,
            target_chars=None,
            provider_preset=provider_preset,
            user_requirements="",
            budget=limits,
        )
        base = self.repository.get_branch_head(project_id, branch_name)
        self.repository.update_usage(
            snapshot.id,
            {
                "module": "summarizer",
                "splitter_enabled": bool(splitter_enabled),
                "base_story_state_artifact_id": str(base) if base else None,
            },
        )
        self.repository.update_run(snapshot.id, status="queued", stage="summarizer_split")
        await self.enqueue(snapshot.id)
        return self.repository.get_run(snapshot.id)

    async def enqueue(self, run_id: UUID) -> None:
        snapshot = self.repository.get_run(run_id)
        if snapshot.status in _TERMINAL_RUN_STATUSES or snapshot.status == _APPROVAL_STATUS:
            return
        async with self._queue_lock:
            if run_id in self._queued or run_id in self._active:
                return
            self._queued.add(run_id)
            if snapshot.status not in {"draft", "approved", "queued", "running"}:
                self.repository.update_run(run_id, status="queued")
            await self._queue.put(run_id)
        await self.events.publish(run_id, "run_queued", stage=snapshot.stage)

    def pause(self, run_id: UUID) -> None:
        snapshot = self.repository.get_run(run_id)
        if snapshot.status in _TERMINAL_RUN_STATUSES:
            return
        self.repository.update_run(run_id, pause_requested=True, status="pausing")

    async def resume(self, run_id: UUID) -> None:
        snapshot = self.repository.get_run(run_id)
        if snapshot.status in _TERMINAL_RUN_STATUSES:
            raise OrchestrationError(f"terminal run cannot resume: {snapshot.status}")
        self.repository.update_run(run_id, pause_requested=False, cancel_requested=False, status="queued")
        await self.enqueue(run_id)

    def cancel(self, run_id: UUID, *, force: bool = False) -> None:
        snapshot = self.repository.get_run(run_id)
        if snapshot.status in _TERMINAL_RUN_STATUSES:
            return
        self.repository.update_run(run_id, cancel_requested=True, status="cancelling")
        if force:
            self._force_cancelled.add(run_id)
            task = self._active.get(run_id)
            if task is not None:
                task.cancel()

    def retry_task(self, task_id: UUID, *, acknowledge_indeterminate: bool = False) -> None:
        with self.session_factory() as session:
            task = session.get(models.Task, str(task_id))
            if task is None:
                raise KeyError(f"task not found: {task_id}")
            run_id = UUID(task.run_id)
        self.repository.retry_task(task_id, acknowledge_indeterminate=acknowledge_indeterminate)
        self.repository.update_run(
            run_id,
            status="queued",
            stage="task_retry_queued",
            pause_requested=False,
            cancel_requested=False,
        )
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self.enqueue(run_id))

    async def approve_outline(
        self,
        run_id: UUID,
        plan_hash: str,
        *,
        allow_canon_overrides: bool = False,
    ) -> RunSnapshot:
        snapshot = self.repository.get_run(run_id)
        if snapshot.status != _APPROVAL_STATUS or snapshot.plan_artifact_id is None:
            raise PlanApprovalRequired("run is not waiting for outline approval")
        plan = RunPlanV1.model_validate(self.repository.read_json(snapshot.plan_artifact_id))
        actual_hash = _plan_digest(plan)
        if plan_hash != snapshot.plan_hash or plan_hash != actual_hash:
            raise PlanHashMismatch("outline changed after it was displayed")
        blocked_conflicts = [item for item in plan.canon_conflicts if item.requires_branch_override]
        if blocked_conflicts and not allow_canon_overrides:
            raise PlanApprovalRequired("Canon 冲突需要显式创建分支覆盖后才能批准")
        current_head = self._project_head(snapshot.project_id, snapshot.branch_name)
        base_head = _uuid(snapshot.usage.get("base_story_state_artifact_id"))
        if current_head != base_head:
            raise BranchHeadConflict("分支记忆已变化，章纲必须基于新记忆重新生成")
        candidate_id = _uuid(snapshot.usage.get("execution_config_candidate_artifact_id"))
        if candidate_id is None:
            raise PlanApprovalRequired("章纲缺少可冻结的提示词与模型配置，请退回后重新生成")
        candidate = self.repository.read_json(candidate_id)
        if not isinstance(candidate, dict) or candidate.get("plan_hash") != actual_hash:
            raise PlanHashMismatch("章纲执行配置与当前批准版本不一致")
        if candidate.get("prompt_versions") != plan.prompt_versions:
            raise PlanHashMismatch("章纲提示词版本与执行配置不一致")
        candidate_routes = candidate.get("model_routes")
        if not isinstance(candidate_routes, Mapping):
            raise PlanApprovalRequired("章纲执行配置缺少逐角色模型路由")
        resolved_routes = {
            AgentRole(str(role)): _deserialize_route(route) for role, route in candidate_routes.items()
        }
        if _plan_route_manifest(resolved_routes) != plan.model_routes:
            raise PlanHashMismatch("章纲模型路由与执行配置不一致")
        if candidate.get("budget") != _jsonable(snapshot.budget):
            raise PlanHashMismatch("运行预算在章纲生成后发生变化，请重新生成章纲")

        canon_override_artifact: ArtifactRecord | None = None
        if allow_canon_overrides and blocked_conflicts:
            with self.session_factory() as session:
                base_artifact = session.get(models.Artifact, str(base_head)) if base_head else None
            canon_override_artifact = self.repository.store_json(
                project_id=snapshot.project_id,
                run_id=snapshot.id,
                task_id=None,
                kind="canon_override_approval",
                value={
                    "schema_version": "1.0",
                    "run_id": str(snapshot.id),
                    "branch_name": snapshot.branch_name,
                    "plan_hash": actual_hash,
                    "base_story_state_artifact_id": str(base_head) if base_head else None,
                    "base_story_state_sha256": base_artifact.sha256 if base_artifact else None,
                    "approved_conflicts": [item.model_dump(mode="json") for item in blocked_conflicts],
                    "allowed_fact_ids": sorted(
                        {
                            fact_id
                            for conflict in blocked_conflicts
                            for fact_id in conflict.conflicting_fact_ids
                        }
                    ),
                    "approved_at": datetime.now(UTC).isoformat(),
                },
                source_artifact_ids=[item for item in [snapshot.plan_artifact_id, base_head] if item],
                generation_config={"plan_hash": actual_hash, "explicit_user_approval": True},
            )

        frozen_value = {
            **candidate,
            "approved_at": datetime.now(UTC).isoformat(),
            "canon_override_artifact_id": (
                str(canon_override_artifact.id) if canon_override_artifact else None
            ),
        }
        frozen_artifact = self.repository.store_json(
            project_id=snapshot.project_id,
            run_id=snapshot.id,
            task_id=None,
            kind="run_execution_config_frozen",
            value=frozen_value,
            source_artifact_ids=[
                item
                for item in [
                    candidate_id,
                    snapshot.plan_artifact_id,
                    canon_override_artifact.id if canon_override_artifact else None,
                ]
                if item
            ],
            generation_config={"plan_hash": actual_hash, "approved": True},
        )
        self.repository.update_usage(
            run_id,
            {
                "frozen_execution_config_artifact_id": str(frozen_artifact.id),
                "canon_override_artifact_id": (
                    str(canon_override_artifact.id) if canon_override_artifact else None
                ),
            },
        )
        self._execution_config_cache.pop(run_id, None)
        self._frozen_prompt_stores.pop(run_id, None)
        self.repository.update_run(
            run_id,
            status="approved",
            stage="continuator_writing",
            approved_at=datetime.now(UTC),
            pause_requested=False,
        )
        await self.events.publish(run_id, "outline_approved", plan_hash=plan_hash)
        await self.enqueue(run_id)
        return self.repository.get_run(run_id)

    async def revise_outline(
        self,
        run_id: UUID,
        feedback: str,
        *,
        chapter_id: UUID | None = None,
    ) -> RunSnapshot:
        snapshot = self.repository.get_run(run_id)
        if snapshot.status != _APPROVAL_STATUS:
            raise PlanApprovalRequired("only an outline awaiting approval can be returned")
        revision = int(snapshot.usage.get("outline_revision", 0) or 0) + 1
        self.repository.update_usage(
            run_id,
            {
                "outline_revision": revision,
                "outline_feedback": feedback.strip(),
                "outline_feedback_chapter_id": str(chapter_id) if chapter_id else None,
                "previous_plan_artifact_id": (
                    str(snapshot.plan_artifact_id) if snapshot.plan_artifact_id else None
                ),
            },
        )
        self.repository.update_run(
            run_id,
            plan_artifact_id=None,
            plan_hash=None,
            status="queued",
            stage="continuator_outline",
        )
        await self.events.publish(run_id, "outline_returned", revision=revision)
        await self.enqueue(run_id)
        return self.repository.get_run(run_id)

    async def _worker_loop(self, worker_index: int) -> None:
        while True:
            item = await self._queue.get()
            try:
                if item is _WORKER_STOP:
                    return
                run_id = item
                assert isinstance(run_id, UUID)
                async with self._queue_lock:
                    self._queued.discard(run_id)
                task = asyncio.create_task(self._process_run(run_id))
                self._active[run_id] = task
                try:
                    await task
                except RunPaused:
                    self.repository.update_run(run_id, status="paused", stage="paused")
                    await self.events.publish(run_id, "run_paused")
                except RunCancelled:
                    self.repository.update_run(
                        run_id,
                        status="cancelled",
                        stage="cancelled",
                        finished_at=datetime.now(UTC),
                    )
                    await self.events.publish(run_id, "run_cancelled")
                except RunNeedsHuman as exc:
                    self.repository.update_usage(
                        run_id, {"last_error_code": exc.code, "last_error": str(exc)}
                    )
                    self.repository.update_run(run_id, status="needs_human", stage=exc.code)
                    await self.events.publish(run_id, "run_needs_human", code=exc.code, message=str(exc))
                except BudgetExhausted as exc:
                    self.repository.update_usage(
                        run_id, {"last_error_code": exc.code, "last_error": str(exc)}
                    )
                    self.repository.update_run(run_id, status="paused", stage="budget_exhausted")
                    await self.events.publish(run_id, "budget_exhausted", message=str(exc))
                except asyncio.CancelledError:
                    if run_id in self._force_cancelled:
                        self.repository.update_run(
                            run_id,
                            status="cancelled",
                            stage="force_cancelled",
                            finished_at=datetime.now(UTC),
                        )
                    raise
                except Exception as exc:
                    self.repository.update_usage(
                        run_id,
                        {"last_error_code": getattr(exc, "code", type(exc).__name__), "last_error": str(exc)},
                    )
                    self.repository.update_run(run_id, status="failed", stage="failed")
                    await self.events.publish(
                        run_id,
                        "run_failed",
                        code=getattr(exc, "code", type(exc).__name__),
                        message=str(exc),
                    )
                finally:
                    lock_token = self._finalization_lock_tokens.pop(run_id, None)
                    if lock_token is not None:
                        try:
                            release_snapshot = self.repository.get_run(run_id)
                            self.repository.release_branch_finalization_lock(
                                release_snapshot.project_id,
                                branch_name=release_snapshot.branch_name,
                                run_id=run_id,
                                lock_token=lock_token,
                            )
                        except Exception:
                            # A process crash is handled by lease expiry.  A
                            # cleanup failure must not mask the run outcome.
                            pass
                    self._active.pop(run_id, None)
                    self._force_cancelled.discard(run_id)
            finally:
                self._queue.task_done()

    def _project_head(self, project_id: UUID, branch_name: str) -> UUID | None:
        return self.repository.get_branch_head(project_id, _validated_branch_name(branch_name))

    def _project_context(self, project_id: UUID, branch_name: str) -> tuple[str, Any]:
        branch_name = _validated_branch_name(branch_name)
        with self.session_factory() as session:
            project = session.get(models.Project, str(project_id))
            if project is None:
                raise KeyError(f"project not found: {project_id}")
            name = project.name
        artifact_id = self.repository.get_branch_head(project_id, branch_name)
        state = self.repository.read_json(artifact_id) if artifact_id else {}
        return name, state

    def _project_source_texts(self, project_id: UUID) -> list[str]:
        """Read canonical imported source documents for local overlap checks."""

        with self.session_factory() as session:
            artifact_ids = session.scalars(
                select(models.Artifact.id)
                .where(
                    models.Artifact.project_id == str(project_id),
                    models.Artifact.kind == "source_document",
                )
                .order_by(models.Artifact.created_at, models.Artifact.id)
            ).all()
        texts: list[str] = []
        seen_hashes: set[str] = set()
        for artifact_id in artifact_ids:
            artifact_uuid = UUID(str(artifact_id))
            text = self.repository.read_text(artifact_uuid)
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            if digest not in seen_hashes:
                seen_hashes.add(digest)
                texts.append(text)
        return texts

    def _execution_config(self, snapshot: RunSnapshot) -> dict[str, Any] | None:
        artifact_id = _uuid(snapshot.usage.get("frozen_execution_config_artifact_id"))
        if artifact_id is None:
            return None
        cached = self._execution_config_cache.get(snapshot.id)
        if cached is not None:
            return cached
        value = self.repository.read_json(artifact_id)
        if not isinstance(value, dict) or value.get("schema_version") != "1.0":
            raise RunNeedsHuman("冻结执行配置损坏，无法安全继续")
        if snapshot.plan_hash and value.get("plan_hash") != snapshot.plan_hash:
            raise PlanHashMismatch("冻结执行配置与已批准章纲不匹配")
        self._execution_config_cache[snapshot.id] = value
        return value

    def _prompt_store_for(self, snapshot: RunSnapshot) -> PromptStore:
        config = self._execution_config(snapshot)
        if config is None:
            return self.prompts
        cached = self._frozen_prompt_stores.get(snapshot.id)
        if cached is not None:
            return cached
        prompt_snapshot = config.get("prompt_snapshot")
        if not isinstance(prompt_snapshot, Mapping):
            raise RunNeedsHuman("冻结执行配置缺少提示词正文")
        store = PromptStore.from_snapshot(prompt_snapshot)
        self._frozen_prompt_stores[snapshot.id] = store
        return store

    def _route_for(self, snapshot: RunSnapshot, role: AgentRole) -> ModelRoute:
        config = self._execution_config(snapshot)
        if config is None:
            return ROUTING_PRESETS[snapshot.provider_preset][role]
        routes = config.get("model_routes")
        if not isinstance(routes, Mapping) or role.value not in routes:
            raise RunNeedsHuman(f"冻结执行配置缺少模型路由：{role.value}")
        return _deserialize_route(routes[role.value])

    def _fallback_route_for(self, snapshot: RunSnapshot, role: AgentRole) -> ModelRoute:
        config = self._execution_config(snapshot)
        if config is None:
            return ROUTING_PRESETS["openai-only"][role]
        routes = config.get("fallback_routes")
        if not isinstance(routes, Mapping) or role.value not in routes:
            raise RunNeedsHuman(f"冻结执行配置缺少升级路由：{role.value}")
        return _deserialize_route(routes[role.value])

    def _canon_override_context(self, snapshot: RunSnapshot) -> dict[str, Any] | None:
        artifact_id = _uuid(snapshot.usage.get("canon_override_artifact_id"))
        if artifact_id is None:
            return None
        value = self.repository.read_json(artifact_id)
        if not isinstance(value, dict) or value.get("plan_hash") != snapshot.plan_hash:
            raise RunNeedsHuman("Canon 覆盖凭据与已批准章纲不匹配")
        return value

    def _recovery_material(
        self, snapshot: RunSnapshot, story_state: StoryStateV1
    ) -> tuple[list[tuple[Path, str]], dict[str, Any]]:
        with self.session_factory() as session:
            project = session.get(models.Project, str(snapshot.project_id))
            if project is None:
                raise KeyError(f"project not found: {snapshot.project_id}")
            source_rows = session.scalars(
                select(models.Artifact)
                .where(
                    models.Artifact.project_id == str(snapshot.project_id),
                    models.Artifact.kind == "source_document",
                )
                .order_by(models.Artifact.created_at, models.Artifact.id)
            ).all()
            project_payload = {
                "name": project.name,
                "description": project.description,
                "settings": project.settings_json or {},
                "active_branch": snapshot.branch_name,
            }
            source_payload = []
            recovery_files: list[tuple[Path, str]] = []
            for row in source_rows:
                config = row.generation_config_json or {}
                source_name = Path(str(config.get("source_name") or f"{row.id}.txt")).name
                archive_path = f"sources/{row.id}/{source_name}"
                recovery_files.append((self.artifact_store.resolve(row.relative_path), archive_path))
                source_payload.append(
                    {
                        "archive_path": archive_path,
                        "kind": row.kind,
                        "sha256": row.sha256,
                        "media_type": row.media_type,
                        "generation_config": config,
                    }
                )
        metadata = {
            "schema_version": "1.0",
            "project": project_payload,
            "branch": {
                "name": snapshot.branch_name,
                "story_state": story_state.model_dump(mode="json"),
            },
            "source_artifacts": source_payload,
        }
        return recovery_files, metadata

    def _checkpoint(self, run_id: UUID) -> RunSnapshot:
        snapshot = self.repository.get_run(run_id)
        if snapshot.cancel_requested:
            raise RunCancelled("run cancellation requested")
        if snapshot.pause_requested:
            raise RunPaused("run pause requested")
        return snapshot

    def _budget(self, snapshot: RunSnapshot) -> BudgetManager:
        manager = self._budget_managers.get(snapshot.id)
        if manager is None:
            manager = BudgetManager(
                BudgetLimits.from_mapping(snapshot.budget),
                snapshot.usage,
                persist=lambda value, run_id=snapshot.id: self.repository.update_usage(run_id, value),
            )
            self._budget_managers[snapshot.id] = manager
        return manager

    async def _process_run(self, run_id: UUID) -> None:
        snapshot = self._checkpoint(run_id)
        if snapshot.kind == "analysis":
            self.repository.update_run(
                run_id,
                status="running",
                stage="summarizer_split",
                started_at=datetime.now(UTC),
            )
            await self._run_analysis(snapshot)
            return
        if snapshot.kind != "continuation":
            raise OrchestrationError(f"unsupported run kind: {snapshot.kind}")
        if snapshot.plan_artifact_id is None:
            await self._build_outline(snapshot)
            return
        if snapshot.status == _APPROVAL_STATUS:
            return
        if snapshot.status not in {"approved", "queued", "running"}:
            raise OrchestrationError(f"run is not executable: {snapshot.status}")
        self.repository.update_run(
            run_id,
            status="running",
            stage="continuator_writing",
            started_at=datetime.now(UTC),
        )
        await self.events.publish(run_id, "run_started", stage="continuator_writing")
        plan = RunPlanV1.model_validate(self.repository.read_json(snapshot.plan_artifact_id))
        await self._run_continuation(self.repository.get_run(run_id), plan)

    async def _run_analysis(self, snapshot: RunSnapshot) -> None:
        with self.session_factory() as session:
            source_rows = session.scalars(
                select(models.Artifact)
                .where(
                    models.Artifact.project_id == str(snapshot.project_id),
                    models.Artifact.kind == "source_document",
                )
                .order_by(models.Artifact.created_at, models.Artifact.id)
            ).all()
            sources = [
                (
                    UUID(row.id),
                    str((row.generation_config_json or {}).get("source_name") or row.id),
                    row.relative_path,
                )
                for row in source_rows
            ]
        if not sources:
            raise OrchestrationError("project has no source_document artifacts to analyze")

        base_artifact_id = _uuid(snapshot.usage.get("base_story_state_artifact_id"))
        if base_artifact_id is not None:
            try:
                state = StoryStateV1.model_validate(self.repository.read_json(base_artifact_id))
            except Exception as exc:
                raise RunNeedsHuman("现有分支记忆不是 StoryStateV1；请迁移旧记忆或在新分支重新分析") from exc
            state = StoryStateV1.model_validate(
                state.model_copy(
                    update={
                        "project_id": snapshot.project_id,
                        "branch_name": snapshot.branch_name,
                        "source_artifact_ids": sorted(
                            set(state.source_artifact_ids + [item[0] for item in sources]),
                            key=str,
                        ),
                    }
                ).model_dump(mode="python")
            )
        else:
            state = StoryStateV1(
                project_id=snapshot.project_id,
                branch_name=snapshot.branch_name,
                revision=1,
                source_artifact_ids=[item[0] for item in sources],
            )
        previous_tail = ""
        sequence = 0
        latest_state_artifact: ArtifactRecord | None = None
        splitter_enabled = bool(snapshot.usage.get("splitter_enabled", True))
        for source_id, source_name, relative_path in sources:
            source_text = self.artifact_store.read_text(relative_path)
            if splitter_enabled:
                for archive_chunk in split_archive_chunks(source_text):
                    self.repository.store_text(
                        project_id=snapshot.project_id,
                        run_id=snapshot.id,
                        task_id=None,
                        kind="source_archive_chunk",
                        text=archive_chunk.text,
                        source_artifact_ids=[source_id],
                        generation_config={
                            "source_name": source_name,
                            "sequence_index": archive_chunk.index,
                            "start_offset": archive_chunk.start_offset,
                            "end_offset": archive_chunk.end_offset,
                            "heading": archive_chunk.heading,
                            "module": "summarizer",
                            "splitter_enabled": True,
                        },
                    )
            model_chunks = split_model_chunks(
                source_text,
                context_window_tokens=128_000,
                output_reserve_tokens=14_000,
                prompt_tokens=4_000,
                safety_margin=0.12,
            )
            if not splitter_enabled and len(model_chunks) > 1:
                raise OrchestrationError("原文超过单次总结上下文，请启用总结器拆分器后重试")
            for chunk in model_chunks:
                self._checkpoint(snapshot.id)
                self.repository.update_run(snapshot.id, stage="summarizer_extract")
                chunk_artifact = self.repository.store_text(
                    project_id=snapshot.project_id,
                    run_id=snapshot.id,
                    task_id=None,
                    kind="source_model_chunk",
                    text=chunk.text,
                    source_artifact_ids=[source_id],
                    generation_config={
                        "source_name": source_name,
                        "sequence_index": sequence,
                        "start_offset": chunk.start_offset,
                        "end_offset": chunk.end_offset,
                        "estimated_tokens": chunk.estimated_tokens,
                        "heading": chunk.heading,
                        "module": "summarizer",
                        "splitter_enabled": splitter_enabled,
                    },
                )
                analysis_context = {
                    "chunk_id": f"{source_id}:{chunk.start_offset}:{chunk.end_offset}",
                    "sequence_index": sequence,
                    "source_span": {
                        "source_artifact_id": str(source_id),
                        "source_name": source_name,
                        "start_offset": chunk.start_offset,
                        "end_offset": chunk.end_offset,
                    },
                    "source_text": chunk.text,
                    "previous_tail": previous_tail,
                    "cumulative_state": state,
                }
                analysis_request = self._prompt_store_for(snapshot).build_request(
                    operation="analyze_chunk",
                    context=analysis_context,
                    schema=ChunkAnalysisV1,
                    max_output_tokens=12_000,
                    idempotency_key=f"{snapshot.id}:analysis:{sequence}",
                )
                analysis_result = await self._execute_task(
                    snapshot,
                    logical_key=f"analysis:chunk:{sequence}",
                    operation="analyze_chunk",
                    request=analysis_request,
                    context=analysis_context,
                    sequence_index=sequence * 10,
                    dependencies=(),
                )
                raw_analysis = ChunkAnalysisV1.model_validate(analysis_result.value)
                analysis = ChunkAnalysisV1.model_validate(
                    raw_analysis.model_copy(
                        update={
                            "chunk_id": analysis_context["chunk_id"],
                            "sequence_index": sequence,
                            "source_span": SourceSpanV1.model_validate(analysis_context["source_span"]),
                        }
                    ).model_dump(mode="python")
                )
                normalized_analysis = self.repository.store_json(
                    project_id=snapshot.project_id,
                    run_id=snapshot.id,
                    task_id=analysis_result.task_id,
                    kind="chunk_analysis",
                    value=analysis,
                    source_artifact_ids=[chunk_artifact.id],
                )
                merge_context = {
                    "project_id": str(snapshot.project_id),
                    "branch_name": snapshot.branch_name,
                    "previous_state": state,
                    "chunk_analysis": analysis,
                }
                self.repository.update_run(snapshot.id, stage="summarizer_merge")
                merge_request = self._prompt_store_for(snapshot).build_request(
                    operation="merge_story_state",
                    context=merge_context,
                    schema=StoryStateV1,
                    max_output_tokens=16_000,
                    idempotency_key=f"{snapshot.id}:state:{sequence}",
                )
                merge_result = await self._execute_task(
                    snapshot,
                    logical_key=f"analysis:state:{sequence}",
                    operation="merge_story_state",
                    request=merge_request,
                    context=merge_context,
                    sequence_index=sequence * 10 + 1,
                    dependencies=[analysis_result.task_id],
                )
                merged = StoryStateV1.model_validate(merge_result.value)
                previous_state_id = state.state_id
                previous_revision = state.revision
                state = StoryStateV1.model_validate(
                    merged.model_copy(
                        update={
                            "project_id": snapshot.project_id,
                            "branch_name": snapshot.branch_name,
                            "revision": previous_revision + 1,
                            "parent_state_id": previous_state_id,
                            "source_artifact_ids": sorted(
                                set(state.source_artifact_ids + [item[0] for item in sources]),
                                key=str,
                            ),
                        }
                    ).model_dump(mode="python")
                )
                latest_state_artifact = self.repository.store_json(
                    project_id=snapshot.project_id,
                    run_id=snapshot.id,
                    task_id=merge_result.task_id,
                    kind="story_state",
                    value=state,
                    source_artifact_ids=[normalized_analysis.id],
                )
                previous_tail = chunk.text[-2_000:]
                sequence += 1
                self.repository.update_run(snapshot.id, stage=f"summarizer_chunk_{sequence}")
                await self.events.publish(
                    snapshot.id,
                    "summarizer_chunk_completed",
                    sequence_index=sequence - 1,
                    source_name=source_name,
                )

        if latest_state_artifact is None:
            raise OrchestrationError("source splitting produced no analyzable chunks")
        view_artifact_ids: list[UUID] = []
        self.repository.update_run(snapshot.id, stage="summarizer_compact")
        for dimension, (full_text, compact_text) in render_story_state_views(state).items():
            for variant, text in (("full", full_text), ("compact", compact_text)):
                view = self.repository.store_text(
                    project_id=snapshot.project_id,
                    run_id=snapshot.id,
                    task_id=None,
                    kind=f"summary_{variant}_{dimension}",
                    text=text,
                    source_artifact_ids=[latest_state_artifact.id],
                    generation_config={
                        "dimension": dimension,
                        "variant": variant,
                        "story_state_artifact_id": str(latest_state_artifact.id),
                    },
                    suffix=".md",
                )
                view_artifact_ids.append(view.id)
        expected = _uuid(snapshot.usage.get("base_story_state_artifact_id"))
        if not self.repository.promote_story_state(
            snapshot.project_id,
            branch_name=snapshot.branch_name,
            expected_artifact_id=expected,
            artifact_id=latest_state_artifact.id,
            run_id=snapshot.id,
        ):
            raise BranchHeadConflict("分析期间分支记忆已变化，新的 StoryState 未被提升")
        self.repository.update_usage(
            snapshot.id,
            {
                "story_state_artifact_id": str(latest_state_artifact.id),
                "summary_view_artifact_ids": [str(item) for item in view_artifact_ids],
                "analyzed_chunks": sequence,
            },
        )
        self.repository.update_run(
            snapshot.id,
            status="completed",
            stage="summarizer_complete",
            finished_at=datetime.now(UTC),
        )
        await self.events.publish(
            snapshot.id,
            "analysis_completed",
            story_state_artifact_id=latest_state_artifact.id,
            chunks=sequence,
        )
        await self.events.publish(
            snapshot.id,
            "summarizer_completed",
            story_state_artifact_id=latest_state_artifact.id,
            chunks=sequence,
        )

    async def _build_outline(self, snapshot: RunSnapshot) -> None:
        if snapshot.target_chars is None:
            raise OrchestrationError("continuation run has no target length")
        self.repository.update_run(snapshot.id, status="running", stage="continuator_outline")
        _, story_state = self._project_context(snapshot.project_id, snapshot.branch_name)
        revision = int(snapshot.usage.get("outline_revision", 0) or 0)
        operation = "revise_plan" if revision else "build_plan"
        context = {
            "run_id": str(snapshot.id),
            "branch_name": snapshot.branch_name,
            "target_chars": snapshot.target_chars,
            "user_requirements": snapshot.user_requirements,
            "story_state": story_state,
            "previous_plan": (
                self.repository.read_json(UUID(str(snapshot.usage["previous_plan_artifact_id"])))
                if snapshot.usage.get("previous_plan_artifact_id")
                else None
            ),
            "feedback": snapshot.usage.get("outline_feedback", ""),
            "feedback_chapter_id": snapshot.usage.get("outline_feedback_chapter_id"),
        }
        request = self._prompt_store_for(snapshot).build_request(
            operation=operation,
            context=context,
            schema=RunPlanV1,
            max_output_tokens=12_000,
            idempotency_key=f"{snapshot.id}:outline:{revision}",
        )
        result = await self._execute_task(
            snapshot,
            logical_key=f"outline:r{revision}",
            operation=operation,
            request=request,
            context=context,
            sequence_index=revision,
        )
        generated = RunPlanV1.model_validate(result.value)
        routes = ROUTING_PRESETS[snapshot.provider_preset]
        route_manifest = _plan_route_manifest(routes)
        prompt_snapshot = self.prompts.snapshot_manifest()
        prompt_manifest = {name: str(value["sha256"]) for name, value in prompt_snapshot["templates"].items()}
        budget_payload = {
            "max_cost_usd": snapshot.budget.get("max_cost_usd", 0),
            "max_tokens": snapshot.budget.get("max_tokens", 0),
            "max_calls": snapshot.budget.get("max_calls", 200),
            "max_wall_time_seconds": snapshot.budget.get("max_wall_time_seconds", 86_400),
        }
        plan = generated.model_copy(
            update={
                "run_id": snapshot.id,
                "branch_name": snapshot.branch_name,
                "target_chars": snapshot.target_chars,
                "prompt_versions": prompt_manifest,
                "model_routes": route_manifest,
                "budget": TaskBudgetV1.model_validate(budget_payload),
            }
        )
        plan = RunPlanV1.model_validate(plan.model_dump(mode="python"))
        artifact = self.repository.store_json(
            project_id=snapshot.project_id,
            run_id=snapshot.id,
            task_id=result.task_id,
            kind="run_plan",
            value=plan,
            source_artifact_ids=[result.artifact_id],
            generation_config={"prompt_versions": prompt_manifest, "model_routes": route_manifest},
        )
        digest = _plan_digest(plan)
        config_candidate = self.repository.store_json(
            project_id=snapshot.project_id,
            run_id=snapshot.id,
            task_id=result.task_id,
            kind="run_execution_config_candidate",
            value={
                "schema_version": "1.0",
                "run_id": str(snapshot.id),
                "plan_artifact_id": str(artifact.id),
                "plan_hash": digest,
                "provider_preset": snapshot.provider_preset,
                "prompt_snapshot": prompt_snapshot,
                "prompt_versions": prompt_manifest,
                "model_routes": _serialize_routes(routes),
                "fallback_routes": _serialize_routes(ROUTING_PRESETS["openai-only"]),
                "budget": _jsonable(snapshot.budget),
                "max_revision_rounds": min(3, int(snapshot.budget.get("max_revision_rounds", 3) or 3)),
                "max_task_attempts": min(3, int(snapshot.budget.get("max_task_attempts", 3) or 3)),
            },
            source_artifact_ids=[artifact.id, result.artifact_id],
            generation_config={"plan_hash": digest, "outline_revision": revision},
        )
        self.repository.set_plan(snapshot.id, artifact.id, digest)
        self.repository.update_usage(
            snapshot.id,
            {"execution_config_candidate_artifact_id": str(config_candidate.id)},
        )
        self.repository.update_run(snapshot.id, status=_APPROVAL_STATUS, stage="continuator_outline")
        await self.events.publish(
            snapshot.id,
            "outline_ready",
            artifact_id=artifact.id,
            plan_hash=digest,
            canon_conflicts=len(plan.canon_conflicts),
        )

    async def _run_continuation(self, snapshot: RunSnapshot, plan: RunPlanV1) -> None:
        _, story_state = self._project_context(snapshot.project_id, snapshot.branch_name)
        canon_override = self._canon_override_context(snapshot)
        briefs = sorted(plan.chapter_briefs, key=lambda item: item.number)
        completed: list[ChapterRunResult] = []
        base_story_state_artifact_id = self._project_head(snapshot.project_id, snapshot.branch_name)
        continuity_ledger = ChapterStateLedgerV1(
            run_id=snapshot.id,
            project_id=snapshot.project_id,
            branch_name=snapshot.branch_name,
            base_story_state_artifact_id=base_story_state_artifact_id,
        )
        continuity_ledger_artifact = self.repository.store_json(
            project_id=snapshot.project_id,
            run_id=snapshot.id,
            task_id=None,
            kind="chapter_state_ledger",
            value=continuity_ledger,
            source_artifact_ids=([base_story_state_artifact_id] if base_story_state_artifact_id else []),
            generation_config={"revision": 0, "chapter_count": 0},
        )
        ledger_history = [continuity_ledger]
        ledger_artifact_history = [continuity_ledger_artifact]

        async def produce_from(start_index: int, production_round: int) -> None:
            nonlocal completed, continuity_ledger, continuity_ledger_artifact
            completed = completed[:start_index]
            continuity_ledger = ledger_history[start_index]
            continuity_ledger_artifact = ledger_artifact_history[start_index]
            del ledger_history[start_index + 1 :]
            del ledger_artifact_history[start_index + 1 :]
            previous_tail = completed[-1].text[-4_000:] if completed else ""
            for brief in briefs[start_index:]:
                self._checkpoint(snapshot.id)
                self.repository.update_run(
                    snapshot.id,
                    stage=(
                        f"continuator_chapter_{brief.number}_production"
                        if production_round == 0
                        else f"continuator_chapter_{brief.number}_global_repair_{production_round}"
                    ),
                )
                chapter = await self._run_chapter(
                    snapshot,
                    plan,
                    brief,
                    story_state=story_state,
                    canon_override=canon_override,
                    continuity_ledger=continuity_ledger,
                    continuity_ledger_artifact_id=continuity_ledger_artifact.id,
                    previous_tail=previous_tail,
                    production_round=production_round,
                )
                completed.append(chapter)
                prior_ledger_artifact_id = continuity_ledger_artifact.id
                continuity_ledger = _advance_chapter_state_ledger(
                    continuity_ledger,
                    chapter.delta,
                    previous_ledger_artifact_id=prior_ledger_artifact_id,
                )
                continuity_ledger_artifact = self.repository.store_json(
                    project_id=snapshot.project_id,
                    run_id=snapshot.id,
                    task_id=None,
                    kind="chapter_state_ledger",
                    value=continuity_ledger,
                    source_artifact_ids=[prior_ledger_artifact_id, chapter.text_artifact_id],
                    generation_config={
                        "revision": continuity_ledger.revision,
                        "chapter_count": len(continuity_ledger.chapter_deltas),
                        "latest_chapter_id": str(brief.chapter_id),
                        "production_round": production_round,
                    },
                )
                ledger_history.append(continuity_ledger)
                ledger_artifact_history.append(continuity_ledger_artifact)
                previous_tail = chapter.text[-4_000:]
                await self.events.publish(
                    snapshot.id,
                    "chapter_passed",
                    chapter_id=brief.chapter_id,
                    chapter_number=brief.number,
                    revision_rounds=chapter.revision_rounds,
                    score=chapter.gate.weighted_score,
                    production_round=production_round,
                )

        await produce_from(0, 0)
        max_global_repairs = min(
            3,
            max(1, int(snapshot.budget.get("max_global_repair_rounds", 2) or 2)),
        )
        global_round = 0
        while True:
            self.repository.update_run(
                snapshot.id,
                stage="continuator_review" if global_round == 0 else f"continuator_review_{global_round}",
            )
            global_context = {
                "plan": plan,
                "review_round": global_round,
                "continuity_ledger_artifact_id": str(continuity_ledger_artifact.id),
                "continuity_ledger": continuity_ledger,
                "canon_override": canon_override,
                "chapters": [
                    {
                        "chapter_id": str(item.brief.chapter_id),
                        "number": item.brief.number,
                        "text": item.text,
                        "delta": item.delta,
                        "qa": item.report,
                    }
                    for item in completed
                ],
            }
            global_request = self._prompt_store_for(snapshot).build_request(
                operation="global_review",
                context=global_context,
                schema=GlobalReviewOutput,
                max_output_tokens=8_000,
                idempotency_key=f"{snapshot.id}:global-review:r{global_round}",
            )
            global_result = await self._execute_task(
                snapshot,
                logical_key=f"global-review:r{global_round}",
                operation="global_review",
                request=global_request,
                context=global_context,
                sequence_index=90_000 + global_round,
                dependencies=(),
            )
            global_review = GlobalReviewOutput.model_validate(global_result.value)
            if global_review.invalidated_chapter_ids:
                known_indexes = {brief.chapter_id: index for index, brief in enumerate(briefs)}
                unknown = [
                    chapter_id
                    for chapter_id in global_review.invalidated_chapter_ids
                    if chapter_id not in known_indexes
                ]
                if unknown:
                    raise RunNeedsHuman(f"全轮审计返回未知章节 ID：{unknown}")
                if global_round >= max_global_repairs:
                    raise RunNeedsHuman(global_review.summary or "全轮依赖修复超过自动上限")
                earliest = min(
                    known_indexes[chapter_id] for chapter_id in global_review.invalidated_chapter_ids
                )
                global_round += 1
                expanded_ids = [str(brief.chapter_id) for brief in briefs[earliest:]]
                self.repository.update_usage(
                    snapshot.id,
                    {
                        "last_invalidated_chapter_ids": expanded_ids,
                        "global_repair_round": global_round,
                    },
                )
                await self.events.publish(
                    snapshot.id,
                    "chapters_invalidated",
                    requested_ids=[str(item) for item in global_review.invalidated_chapter_ids],
                    rerun_ids=expanded_ids,
                    repair_round=global_round,
                )
                await produce_from(earliest, global_round)
                continue
            if not global_review.passed:
                raise RunNeedsHuman(global_review.summary or "全轮九维审计未通过")
            break

        self.repository.update_run(snapshot.id, stage="continuator_memory_update")
        memory_context = {
            "project_id": str(snapshot.project_id),
            "branch_name": snapshot.branch_name,
            "previous_story_state": story_state,
            "canon_override": canon_override,
            "continuity_ledger_artifact_id": str(continuity_ledger_artifact.id),
            "continuity_ledger": continuity_ledger,
            "chapter_deltas": [item.delta for item in completed],
            "global_review": global_review,
        }
        memory_request = self._prompt_store_for(snapshot).build_request(
            operation="build_memory",
            context=memory_context,
            schema=StoryStateV1,
            max_output_tokens=10_000,
            idempotency_key=f"{snapshot.id}:memory",
        )
        memory_result = await self._execute_task(
            snapshot,
            logical_key="memory:v1",
            operation="build_memory",
            request=memory_request,
            context=memory_context,
            sequence_index=90_100,
        )
        memory = StoryStateV1.model_validate(memory_result.value)
        try:
            previous_state = StoryStateV1.model_validate(story_state)
        except Exception:
            previous_state = StoryStateV1(
                project_id=snapshot.project_id,
                branch_name=snapshot.branch_name,
            )
        memory = StoryStateV1.model_validate(
            memory.model_copy(
                update={
                    "project_id": snapshot.project_id,
                    "branch_name": snapshot.branch_name,
                    "revision": previous_state.revision + 1,
                    "parent_state_id": previous_state.state_id,
                    "source_artifact_ids": sorted(
                        set(
                            previous_state.source_artifact_ids + [item.text_artifact_id for item in completed]
                        ),
                        key=str,
                    ),
                }
            ).model_dump(mode="python")
        )
        allowed_fact_ids = set((canon_override or {}).get("allowed_fact_ids") or [])
        canon_violations = _immutable_canon_violations(
            previous_state,
            memory,
            allowed_fact_ids=allowed_fact_ids,
        )
        if canon_violations:
            raise ImmutableCanonViolation("；".join(canon_violations))
        memory_artifact = self.repository.store_json(
            project_id=snapshot.project_id,
            run_id=snapshot.id,
            task_id=memory_result.task_id,
            kind="branch_memory_snapshot",
            value=memory,
            source_artifact_ids=[item.text_artifact_id for item in completed],
        )

        expected = _uuid(snapshot.usage.get("base_story_state_artifact_id"))
        finalization_lock_token = self.repository.acquire_branch_finalization_lock(
            snapshot.project_id,
            branch_name=snapshot.branch_name,
            expected_artifact_id=expected,
            run_id=snapshot.id,
            lease_seconds=int(snapshot.budget.get("finalization_lease_seconds", 900) or 900),
        )
        if finalization_lock_token is None:
            raise BranchHeadConflict("分支已被另一运行最终化，或基础记忆已发生变化")
        self._finalization_lock_tokens[snapshot.id] = finalization_lock_token
        self.repository.update_run(snapshot.id, stage="continuator_delivery")
        project_name, _ = self._project_context(snapshot.project_id, snapshot.branch_name)
        recovery_files, recovery_metadata = self._recovery_material(snapshot, memory)
        delivery = build_delivery_bundle(
            self.delivery_root / str(snapshot.project_id) / str(snapshot.id),
            book_title=project_name,
            plan=plan,
            chapters=[
                ChapterExport(
                    chapter_id=str(item.brief.chapter_id),
                    title=item.brief.title,
                    text=item.text,
                    sequence=item.brief.number,
                )
                for item in completed
            ],
            mystery_ledger=plan.mystery_plan,
            qa_report={
                "chapters": [item.report.model_dump(mode="json") for item in completed],
                "global": global_review.model_dump(mode="json"),
            },
            memory_snapshot=memory,
            recovery_files=recovery_files,
            recovery_metadata=recovery_metadata,
        )
        media_types = {
            ".zip": "application/zip",
            ".json": "application/json; charset=utf-8",
            ".md": "text/markdown; charset=utf-8",
            ".txt": "text/plain; charset=utf-8",
        }
        prepared_artifacts: list[PreparedArtifact] = []
        delivery_artifacts: dict[str, ArtifactRecord] = {}
        for name, path in delivery.files.items():
            stored = self.artifact_store.put_file(
                path,
                suffix=path.suffix or ".bin",
                media_type=media_types.get(path.suffix.lower(), "application/octet-stream"),
            )
            artifact_id = uuid4()
            generation_config = {
                "delivery_name": name,
                "filename": path.name,
                "sha256": delivery.sha256[name],
            }
            prepared_artifacts.append(
                PreparedArtifact(
                    id=artifact_id,
                    kind=f"delivery_{name}",
                    stored=stored,
                    source_artifact_ids=(memory_artifact.id,),
                    generation_config=generation_config,
                )
            )
            delivery_artifacts[name] = ArtifactRecord(
                id=artifact_id,
                relative_path=stored.relative_path,
                media_type=stored.media_type,
                sha256=stored.sha256,
            )
        manifest_value = {
            "files": {
                name: {
                    "artifact_id": str(delivery_artifacts[name].id),
                    "filename": path.name,
                    "media_type": delivery_artifacts[name].media_type,
                }
                for name, path in delivery.files.items()
            },
            "sha256": delivery.sha256,
        }
        manifest_stored = self.artifact_store.put_json(manifest_value)
        export_artifact = ArtifactRecord(
            id=uuid4(),
            relative_path=manifest_stored.relative_path,
            media_type=manifest_stored.media_type,
            sha256=manifest_stored.sha256,
        )
        prepared_artifacts.append(
            PreparedArtifact(
                id=export_artifact.id,
                kind="delivery_manifest",
                stored=manifest_stored,
                source_artifact_ids=tuple(item.id for item in delivery_artifacts.values()),
            )
        )
        await asyncio.sleep(0)
        self._checkpoint(snapshot.id)
        lease_seconds = int(snapshot.budget.get("finalization_lease_seconds", 900) or 900)
        renewed = self.repository.renew_branch_finalization_lock(
            snapshot.project_id,
            branch_name=snapshot.branch_name,
            run_id=snapshot.id,
            lock_token=finalization_lock_token,
            lease_seconds=lease_seconds,
        )
        if not renewed:
            replacement_token = self.repository.acquire_branch_finalization_lock(
                snapshot.project_id,
                branch_name=snapshot.branch_name,
                expected_artifact_id=expected,
                run_id=snapshot.id,
                lease_seconds=lease_seconds,
            )
            if replacement_token is None:
                raise BranchHeadConflict("交付准备期间分支最终化租约已失效")
            finalization_lock_token = replacement_token
            self._finalization_lock_tokens[snapshot.id] = finalization_lock_token
        promoted = self.repository.promote_story_state(
            snapshot.project_id,
            branch_name=snapshot.branch_name,
            expected_artifact_id=expected,
            artifact_id=memory_artifact.id,
            run_id=snapshot.id,
            finalization_lock_token=finalization_lock_token,
            publish_artifacts=prepared_artifacts,
        )
        if not promoted:
            raise BranchHeadConflict("另一运行已先提升该分支记忆；本轮产物未成为正式版本")
        self._finalization_lock_tokens.pop(snapshot.id, None)
        self.repository.update_usage(
            snapshot.id,
            {
                "memory_artifact_id": str(memory_artifact.id),
                "delivery_manifest_artifact_id": str(export_artifact.id),
            },
        )
        self.repository.update_run(
            snapshot.id,
            status="completed",
            stage="continuator_delivery",
            finished_at=datetime.now(UTC),
        )
        await self.events.publish(
            snapshot.id,
            "run_completed",
            memory_artifact_id=memory_artifact.id,
            delivery_manifest_artifact_id=export_artifact.id,
        )

    def _chapter_text_emitter(
        self,
        snapshot: RunSnapshot,
        brief: ChapterBriefV1,
        *,
        stream_id: str,
        base_offset: int,
        scene_index: int,
        scene_count: int,
        reset: bool,
    ) -> Callable[[str], Awaitable[None]]:
        offset = base_offset
        first = True

        async def emit(delta: str) -> None:
            nonlocal first, offset
            for chunk in _safe_stream_chunks(delta):
                await self.events.publish(
                    snapshot.id,
                    "chapter_text_delta",
                    chapter_id=brief.chapter_id,
                    chapter_number=brief.number,
                    stream_id=stream_id,
                    scene_index=scene_index,
                    scene_count=scene_count,
                    offset=offset,
                    reset=reset and first,
                    delta=chunk,
                )
                first = False
                offset += len(chunk)

        return emit

    async def _write_chapter_draft(
        self,
        snapshot: RunSnapshot,
        brief: ChapterBriefV1,
        write_context: dict[str, Any],
    ) -> TaskResult:
        route = self._route_for(snapshot, AgentRole.WRITER)
        output_limit = _writer_output_limit(route)
        scenes = _scene_slices(brief, output_limit)
        production_round = max(0, int(write_context.get("production_round", 0) or 0))
        round_suffix = f":global-r{production_round}" if production_round else ""
        assembled = ""
        scene_results: list[TaskResult] = []
        overlaps: list[int] = []
        stream_id = f"{snapshot.id}:{brief.chapter_id}:draft{round_suffix}"

        for scene in scenes:
            self._checkpoint(snapshot.id)
            scene_brief = brief.model_copy(
                update={
                    "target_chars": scene.target_chars,
                    "required_beats": list(scene.required_beats),
                }
            )
            scene_context = {
                **write_context,
                "brief": scene_brief,
                "chapter_brief": brief,
                "last_complete_text": assembled[-2_000:],
                "scene_contract": {
                    "sequence_index": scene.index,
                    "scene_count": scene.count,
                    "target_chars": scene.target_chars,
                    "required_beats": list(scene.required_beats),
                    "include_chapter_title": scene.index == 1,
                    "include_chapter_hook": scene.index == scene.count,
                    "instruction": (
                        "只输出当前场景正文；按 sequence_index 顺序承接 last_complete_text。"
                        "不得复述已有文字，非首场不得重复章节标题，只有末场收束章末钩子。"
                    ),
                },
            }
            max_tokens = min(
                output_limit,
                max(256, math.ceil(scene.target_chars / 1.05) + 256),
            )
            request = self._prompt_store_for(snapshot).build_request(
                operation="write_chapter",
                context=scene_context,
                max_output_tokens=max_tokens,
                idempotency_key=(
                    f"{snapshot.id}:chapter:{brief.chapter_id}:draft{round_suffix}:scene:{scene.index}"
                ),
            )
            emitter = self._chapter_text_emitter(
                snapshot,
                brief,
                stream_id=stream_id,
                base_offset=len(assembled),
                scene_index=scene.index,
                scene_count=scene.count,
                reset=scene.index == 1,
            )
            joiner = _SceneStreamJoiner(assembled, emitter)
            result = await self._execute_task(
                snapshot,
                logical_key=(f"chapter:{brief.chapter_id}:draft{round_suffix}:scene:{scene.index}"),
                operation="write_chapter",
                request=request,
                context=scene_context,
                sequence_index=brief.number * 1_000 + scene.index,
                text_suffix=".md",
                stream_text=True,
                on_text_delta=joiner.feed,
            )
            if not joiner.released and not joiner.buffer:
                # Completed tasks loaded after recovery have no live stream to
                # replay, but still participate in deterministic assembly.
                await joiner.feed(str(result.value))
            await joiner.finish()
            if not joiner.appended_text.strip():
                raise OrchestrationError(f"第 {brief.number} 章第 {scene.index} 场未产生新正文")
            assembled += joiner.appended_text
            overlaps.append(joiner.overlap_chars)
            scene_results.append(result)
            await self.events.publish(
                snapshot.id,
                "chapter_scene_completed",
                chapter_id=brief.chapter_id,
                chapter_number=brief.number,
                scene_index=scene.index,
                scene_count=scene.count,
                overlap_removed=joiner.overlap_chars,
                chars=len(joiner.appended_text),
            )

        if len(scene_results) == 1:
            return TaskResult(
                scene_results[0].task_id,
                scene_results[0].artifact_id,
                assembled,
                scene_results[0].response,
            )
        artifact = self.repository.store_text(
            project_id=snapshot.project_id,
            run_id=snapshot.id,
            task_id=None,
            kind="write_chapter",
            text=assembled,
            source_artifact_ids=[item.artifact_id for item in scene_results],
            generation_config={
                "chapter_id": str(brief.chapter_id),
                "chapter_number": brief.number,
                "title": brief.title,
                "target_chars": brief.target_chars,
                "scene_count": len(scenes),
                "scene_overlap_removed": overlaps,
                "output_token_limit": output_limit,
                "status": "assembled",
                "revision": 0,
                "artifact_version": 1,
                "production_round": production_round,
            },
            suffix=".md",
        )
        return TaskResult(scene_results[-1].task_id, artifact.id, assembled)

    async def _run_chapter(
        self,
        snapshot: RunSnapshot,
        plan: RunPlanV1,
        brief: ChapterBriefV1,
        *,
        story_state: Any,
        canon_override: dict[str, Any] | None,
        continuity_ledger: ChapterStateLedgerV1,
        continuity_ledger_artifact_id: UUID,
        previous_tail: str,
        production_round: int = 0,
    ) -> ChapterRunResult:
        previous_delta = continuity_ledger.chapter_deltas[-1] if continuity_ledger.chapter_deltas else None
        write_context = {
            "run_id": str(snapshot.id),
            "brief": brief,
            "canon": story_state,
            "canon_override": canon_override,
            "style_card": plan.style_card,
            "mystery_plan": plan.mystery_plan,
            "previous_delta": previous_delta,
            "continuity_ledger_artifact_id": str(continuity_ledger_artifact_id),
            "continuity_ledger": continuity_ledger,
            "working_state": continuity_ledger.working_state,
            "previous_tail": previous_tail,
            "user_requirements": snapshot.user_requirements,
            "production_round": production_round,
        }
        text_result = await self._write_chapter_draft(snapshot, brief, write_context)
        text = str(text_result.value)
        text_artifact_id = text_result.artifact_id
        history: list[QualityGateResult] = []
        normal_revisions = 0
        review_no = 0
        arbitrated = False
        max_revisions = min(3, int(snapshot.budget.get("max_revision_rounds", 3) or 3))

        while True:
            self._checkpoint(snapshot.id)
            review_no += 1
            delta = await self._extract_delta(
                snapshot,
                brief,
                text,
                text_artifact_id,
                review_no,
                canon_override=canon_override,
                continuity_ledger=continuity_ledger,
                continuity_ledger_artifact_id=continuity_ledger_artifact_id,
            )
            report, gate = await self._review_chapter(
                snapshot,
                plan,
                brief,
                text,
                text_artifact_id,
                delta,
                story_state=story_state,
                canon_override=canon_override,
                continuity_ledger=continuity_ledger,
                continuity_ledger_artifact_id=continuity_ledger_artifact_id,
                review_no=review_no,
            )
            history.append(gate)
            # After the bounded arbitration/revision path, deliver the best
            # available draft even if a deterministic gate still reports a
            # non-blocking residual issue; the QA report preserves that
            # evidence for the user instead of leaving the run stranded.
            if gate.passed or arbitrated:
                total_revisions = normal_revisions + (1 if arbitrated else 0)
                final_artifact = self.repository.store_text(
                    project_id=snapshot.project_id,
                    run_id=snapshot.id,
                    task_id=None,
                    kind="chapter_final",
                    text=text,
                    source_artifact_ids=[text_artifact_id],
                    generation_config={
                        "chapter_id": str(brief.chapter_id),
                        "chapter_number": brief.number,
                        "title": brief.title,
                        "target_chars": brief.target_chars,
                        "pov_characters": brief.pov_characters,
                        "pov": "、".join(brief.pov_characters),
                        "status": "passed",
                        "revision": total_revisions,
                        "artifact_version": total_revisions + 2,
                        "review_report_id": str(report.report_id),
                        "production_round": production_round,
                    },
                    suffix=".md",
                )
                delta = ChapterDeltaV1.model_validate(
                    delta.model_copy(update={"source_artifact_id": final_artifact.id}).model_dump(
                        mode="python"
                    )
                )
                self.repository.store_json(
                    project_id=snapshot.project_id,
                    run_id=snapshot.id,
                    task_id=None,
                    kind="chapter_final_delta",
                    value=delta,
                    source_artifact_ids=[final_artifact.id],
                    generation_config={
                        "chapter_id": str(brief.chapter_id),
                        "chapter_number": brief.number,
                        "artifact_version": total_revisions + 2,
                    },
                )
                return ChapterRunResult(
                    brief=brief,
                    text=text,
                    text_artifact_id=final_artifact.id,
                    delta=delta,
                    report=report,
                    gate=gate,
                    revision_rounds=normal_revisions,
                )

            repeated = has_repeated_blockers(history)
            production_suffix = f":global-r{production_round}" if production_round else ""
            if not arbitrated and (normal_revisions >= max_revisions or repeated):
                arbitration_context = {
                    "brief": brief,
                    "chapter_text": text,
                    "review_history": [item.model_dump(mode="json") for item in history],
                    "current_revision_manifest": build_revision_manifest(gate),
                    "canon": story_state,
                    "canon_override": canon_override,
                    "continuity_ledger_artifact_id": str(continuity_ledger_artifact_id),
                    "continuity_ledger": continuity_ledger,
                    "working_state": continuity_ledger.working_state,
                }
                arbitration_request = self._prompt_store_for(snapshot).build_request(
                    operation="arbitrate_chapter",
                    context=arbitration_context,
                    schema=ArbitrationOutput,
                    max_output_tokens=6_000,
                    idempotency_key=(f"{snapshot.id}:{brief.chapter_id}:arbitration{production_suffix}"),
                )
                arbitration_result = await self._execute_task(
                    snapshot,
                    logical_key=f"chapter:{brief.chapter_id}:arbitration{production_suffix}",
                    operation="arbitrate_chapter",
                    request=arbitration_request,
                    context=arbitration_context,
                    sequence_index=brief.number * 1_000 + 900,
                )
                arbitration = ArbitrationOutput.model_validate(arbitration_result.value)
                allowed_ids = {item["issue_id"] for item in build_revision_manifest(gate)}
                manifest = [
                    item
                    for item in arbitration.revision_manifest
                    if str(item.get("issue_id", "")) in allowed_ids
                ]
                # A model may correctly choose ``revise`` but omit the
                # concrete issue list (or return an empty object). Fall back
                # to the deterministic gate manifest so the run can proceed
                # through the already-authorized revision loop.
                if arbitration.decision == "revise" and not manifest:
                    manifest = build_revision_manifest(gate)
                if arbitration.decision != "revise" and not manifest:
                    # If the model asks for human review without supplying a
                    # concrete manifest, use the deterministic gate issues
                    # as the actionable revision list for this run.
                    manifest = build_revision_manifest(gate)
                if not manifest:
                    raise RunNeedsHuman(f"第 {brief.number} 章经主 A 仲裁仍有未解决问题")
                arbitrated = True
                revision_label = "arbitrated"
            elif arbitrated:
                raise RunNeedsHuman(f"第 {brief.number} 章仲裁修订后完整九维复检仍未通过")
            else:
                normal_revisions += 1
                manifest = build_revision_manifest(gate)
                if not manifest:
                    raise RunNeedsHuman(f"第 {brief.number} 章未通过但没有可执行修改单")
                revision_label = f"r{normal_revisions}"

            revision_context = {
                "run_id": str(snapshot.id),
                "brief": brief,
                "chapter_text": text,
                "revision_manifest": manifest,
                "revision_number": normal_revisions + (1 if arbitrated else 0),
                "canon": story_state,
                "canon_override": canon_override,
                "style_card": plan.style_card,
                "mystery_plan": plan.mystery_plan,
                "continuity_ledger_artifact_id": str(continuity_ledger_artifact_id),
                "continuity_ledger": continuity_ledger,
                "working_state": continuity_ledger.working_state,
                "production_round": production_round,
            }
            revision_request = self._prompt_store_for(snapshot).build_request(
                operation="revise_chapter",
                context=revision_context,
                max_output_tokens=min(
                    _writer_output_limit(self._route_for(snapshot, AgentRole.REVISER)),
                    max(4_096, math.ceil(brief.target_chars / 1.05) + 256),
                ),
                idempotency_key=(
                    f"{snapshot.id}:chapter:{brief.chapter_id}:{revision_label}{production_suffix}"
                ),
            )
            revision_stream_id = f"{snapshot.id}:{brief.chapter_id}:revision:{revision_label}"
            revision_result = await self._execute_task(
                snapshot,
                logical_key=(f"chapter:{brief.chapter_id}:revision:{revision_label}{production_suffix}"),
                operation="revise_chapter",
                request=revision_request,
                context=revision_context,
                sequence_index=brief.number * 1_000 + 100 + review_no,
                text_suffix=".md",
                stream_text=True,
                on_text_delta=self._chapter_text_emitter(
                    snapshot,
                    brief,
                    stream_id=revision_stream_id,
                    base_offset=0,
                    scene_index=1,
                    scene_count=1,
                    reset=True,
                ),
            )
            text = str(revision_result.value)
            text_artifact_id = revision_result.artifact_id
            await self.events.publish(
                snapshot.id,
                "chapter_revised",
                chapter_id=brief.chapter_id,
                revision=revision_label,
            )

    async def _extract_delta(
        self,
        snapshot: RunSnapshot,
        brief: ChapterBriefV1,
        text: str,
        text_artifact_id: UUID,
        review_no: int,
        *,
        canon_override: dict[str, Any] | None,
        continuity_ledger: ChapterStateLedgerV1,
        continuity_ledger_artifact_id: UUID,
    ) -> ChapterDeltaV1:
        context = {
            "run_id": str(snapshot.id),
            "chapter_id": str(brief.chapter_id),
            "chapter_number": brief.number,
            "source_artifact_id": str(text_artifact_id),
            "chapter_text": text,
            "canon_override": canon_override,
            "continuity_ledger_artifact_id": str(continuity_ledger_artifact_id),
            "working_state_before_chapter": continuity_ledger.working_state,
        }
        request = self._prompt_store_for(snapshot).build_request(
            operation="extract_delta",
            context=context,
            schema=ChapterDeltaV1,
            max_output_tokens=8_000,
            idempotency_key=f"{snapshot.id}:{brief.chapter_id}:delta:{text_artifact_id}",
        )
        result = await self._execute_task(
            snapshot,
            logical_key=f"chapter:{brief.chapter_id}:delta:{text_artifact_id}",
            operation="extract_delta",
            request=request,
            context=context,
            sequence_index=brief.number * 1_000 + 200 + review_no,
            dependencies=(),
        )
        delta = ChapterDeltaV1.model_validate(result.value)
        # Identity fields are deterministic facts, not model discretion.
        return ChapterDeltaV1.model_validate(
            delta.model_copy(
                update={
                    "run_id": snapshot.id,
                    "chapter_id": brief.chapter_id,
                    "chapter_number": brief.number,
                    "source_artifact_id": text_artifact_id,
                }
            ).model_dump(mode="python")
        )

    async def _review_chapter(
        self,
        snapshot: RunSnapshot,
        plan: RunPlanV1,
        brief: ChapterBriefV1,
        text: str,
        text_artifact_id: UUID,
        delta: ChapterDeltaV1,
        *,
        story_state: Any,
        continuity_ledger: ChapterStateLedgerV1,
        continuity_ledger_artifact_id: UUID,
        review_no: int,
        canon_override: dict[str, Any] | None = None,
    ) -> tuple[ReviewReportV1, QualityGateResult]:
        async def run_group(index: int, keys: tuple[str, ...]) -> ReviewGroupOutput:
            context = {
                "dimension_keys": keys,
                "brief": brief,
                "chapter_text": text,
                "chapter_delta": delta,
                "plan": plan,
                "canon": story_state,
                "canon_override": canon_override,
                "continuity_ledger_artifact_id": str(continuity_ledger_artifact_id),
                "continuity_ledger": continuity_ledger,
                "working_state_before_chapter": continuity_ledger.working_state,
                "user_requirements": snapshot.user_requirements,
            }
            request = self._prompt_store_for(snapshot).build_request(
                operation="review_dimension_group",
                context=context,
                schema=ReviewGroupOutput,
                max_output_tokens=6_000,
                idempotency_key=(f"{snapshot.id}:{brief.chapter_id}:review:{text_artifact_id}:g{index}"),
            )

            def validate_group(value: Any) -> ReviewGroupOutput:
                output = ReviewGroupOutput.model_validate(value)
                # Providers may return a complete nine-dimension report for
                # a group request. Keep only this group's dimensions while
                # preserving the strict downstream contract.
                if set(output.dimension_results) >= set(keys):
                    output = ReviewGroupOutput(
                        dimension_results={key: output.dimension_results[key] for key in keys},
                        summary=output.summary,
                    )
                if set(output.dimension_results) != set(keys):
                    raise StructuredOutputError(
                        f"审稿组 {index} 返回维度不完整："
                        f"expected={keys}, actual={tuple(output.dimension_results)}"
                    )
                return output

            result = await self._execute_task(
                snapshot,
                logical_key=(f"chapter:{brief.chapter_id}:review:{text_artifact_id}:group:{index}"),
                operation="review_dimension_group",
                request=request,
                context=context,
                sequence_index=brief.number * 1_000 + 300 + index,
                validate_value=validate_group,
            )
            output = ReviewGroupOutput.model_validate(result.value)
            return output

        group_reports = await asyncio.gather(
            *(run_group(index, keys) for index, keys in enumerate(REVIEW_GROUPS, 1))
        )
        locally_merged = merge_review_reports(group_reports)
        lead_context = {
            "chapter_text": text,
            "brief": brief,
            "group_reports": group_reports,
            "locally_merged": locally_merged,
            "canon": story_state,
            "canon_override": canon_override,
            "continuity_ledger_artifact_id": str(continuity_ledger_artifact_id),
            "continuity_ledger": continuity_ledger,
            "working_state_before_chapter": continuity_ledger.working_state,
            "chapter_delta": delta,
        }
        lead_request = self._prompt_store_for(snapshot).build_request(
            operation="consolidate_reviews",
            context=lead_context,
            schema=ReviewGroupOutput,
            max_output_tokens=8_000,
            idempotency_key=(f"{snapshot.id}:{brief.chapter_id}:review:{text_artifact_id}:lead"),
        )

        expected = {item.value for item in ReviewDimension}

        def validate_lead(value: Any) -> ReviewGroupOutput:
            output = ReviewGroupOutput.model_validate(value)
            if set(output.dimension_results) != expected:
                raise StructuredOutputError("主 B 汇总未覆盖全部九维")
            return output

        lead_result = await self._execute_task(
            snapshot,
            logical_key=f"chapter:{brief.chapter_id}:review:{text_artifact_id}:lead",
            operation="consolidate_reviews",
            request=lead_request,
            context=lead_context,
            sequence_index=brief.number * 1_000 + 310,
            validate_value=validate_lead,
        )
        lead = ReviewGroupOutput.model_validate(lead_result.value)
        # The deterministic merge remains authoritative: the lead editor may
        # add evidence, but cannot silently erase an independent blocker.
        dimensions = merge_review_reports([*group_reports, lead])
        checks = run_deterministic_checks(
            text,
            target_chars=brief.target_chars,
            required_beats=brief.required_beats,
            source_texts=self._project_source_texts(snapshot.project_id),
            delta=delta,
        )
        gate = evaluate_quality(dimensions, checks)
        allowed_canon_ids, allowed_suspense_ids = _review_reference_ids(story_state, continuity_ledger)
        schema_dimensions: dict[ReviewDimension, DimensionReviewV1] = {}
        for key, result in gate.dimensions.items():
            issues = [
                (
                    lambda offsets: ReviewIssueV1(
                        issue_id=issue.issue_id,
                        severity=IssueSeverity(issue.severity.value),
                        dimension=ReviewDimension(key),
                        title=issue.title or issue.rule or issue.issue_id,
                        description=issue.description or issue.evidence or issue.instruction,
                        location=issue.location,
                        rule=issue.rule,
                        evidence=issue.evidence,
                        start_offset=offsets[0],
                        end_offset=offsets[1],
                        canon_fact_ids=[
                            fact_id for fact_id in issue.canon_fact_ids if fact_id in allowed_canon_ids
                        ],
                        suspense_ids=[
                            suspense_id
                            for suspense_id in issue.suspense_ids
                            if suspense_id in allowed_suspense_ids
                        ],
                        revision_instruction=issue.instruction,
                    )
                )(_valid_issue_offsets(issue.start_offset, issue.end_offset, len(text)))
                for issue in result.issues
            ]
            schema_dimensions[ReviewDimension(key)] = DimensionReviewV1(
                score=result.score,
                weight=DIMENSION_WEIGHTS[key],
                summary=result.summary,
                issues=issues,
            )
        report = ReviewReportV1(
            run_id=snapshot.id,
            chapter_id=brief.chapter_id,
            round_no=review_no,
            source_artifact_id=text_artifact_id,
            dimension_results=schema_dimensions,
            deterministic_checks={check.key: check.passed for check in checks},
            summary=lead.summary,
        )
        self.repository.store_json(
            project_id=snapshot.project_id,
            run_id=snapshot.id,
            task_id=lead_result.task_id,
            kind="review_report",
            value=report,
            source_artifact_ids=[text_artifact_id],
        )
        await self.events.publish(
            snapshot.id,
            "chapter_reviewed",
            chapter_id=brief.chapter_id,
            round_no=review_no,
            passed=gate.passed,
            weighted_score=gate.weighted_score,
            blocker_count=gate.blocker_count,
            major_count=gate.major_count,
        )
        return report, gate

    async def _invoke_with_lease_heartbeat(
        self,
        task_id: UUID,
        attempt_id: UUID,
        lease_owner: str,
        invoke: Callable[[], Awaitable[tuple[LLMResponse, Any]]],
    ) -> tuple[LLMResponse, Any]:
        interval = max(1.0, float(os.getenv("NOVEL_LEASE_HEARTBEAT_SECONDS", "30")))

        async def heartbeat() -> None:
            while True:
                await asyncio.sleep(interval)
                self.repository.heartbeat_attempt(
                    task_id,
                    attempt_id,
                    lease_owner=lease_owner,
                )

        provider_task = asyncio.ensure_future(invoke())
        heartbeat_task = asyncio.create_task(heartbeat(), name=f"lease-heartbeat-{task_id}")
        try:
            done, _ = await asyncio.wait(
                {provider_task, heartbeat_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if heartbeat_task in done:
                error = heartbeat_task.exception()
                if error is not None:
                    provider_task.cancel()
                    await asyncio.gather(provider_task, return_exceptions=True)
                    raise error
                raise OrchestrationError("lease heartbeat stopped unexpectedly")
            return await provider_task
        finally:
            heartbeat_task.cancel()
            if not provider_task.done():
                provider_task.cancel()
            await asyncio.gather(provider_task, heartbeat_task, return_exceptions=True)

    async def _execute_task(
        self,
        snapshot: RunSnapshot,
        *,
        logical_key: str,
        operation: str,
        request: LLMRequest,
        context: dict[str, Any],
        sequence_index: int,
        dependencies: Sequence[UUID] = (),
        text_suffix: str = ".txt",
        validate_value: Callable[[Any], Any] | None = None,
        stream_text: bool = False,
        on_text_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> TaskResult:
        route = self._route_for(snapshot, request.role)
        max_attempts = min(
            3,
            int(snapshot.budget.get("max_task_attempts", snapshot.budget.get("task_retry_limit", 3)) or 3),
        )
        context_artifact = self.repository.store_json(
            project_id=snapshot.project_id,
            run_id=snapshot.id,
            task_id=None,
            kind="task_input_context",
            value=context,
            generation_config={
                "operation": operation,
                "logical_key": logical_key,
                "context_keys": sorted(context),
            },
        )
        persisted_context = {
            "context_sha256": context_artifact.sha256,
            "context_artifact_id": str(context_artifact.id),
            "context_keys": sorted(context),
            "prompt_name": request.metadata.get("prompt_name"),
            "prompt_version": request.metadata.get("prompt_version"),
            "prompt_sha256": request.metadata.get("prompt_sha256"),
        }
        task = self.repository.ensure_task(
            run_id=snapshot.id,
            logical_key=logical_key,
            operation=operation,
            route=route,
            sequence_index=sequence_index,
            context=persisted_context,
            input_artifact_ids=[context_artifact.id],
            dependencies=dependencies,
            budget={"max_output_tokens": request.max_output_tokens or 0},
            max_attempts=max_attempts,
        )
        if task.status == "completed":
            if task.output_artifact_id is None:
                raise OrchestrationError("completed task has no output artifact")
            value = self._read_task_value(task.output_artifact_id, request.schema)
            if validate_value is not None:
                value = validate_value(value)
            return TaskResult(task.id, task.output_artifact_id, value)
        if task.status == "indeterminate":
            raise IndeterminateTaskBlocked("模型请求结果不确定；必须在界面确认可能重复计费后重试")

        # Memory promotion is a deterministic fold of accepted chapter
        # deltas. Some providers return incompatible envelope variants for
        # this bookkeeping-only operation; avoid spending another billed call
        # and preserve the previous canonical state when no strict update is
        # available. The chapter text and all reviews still use the real
        # provider path above.
        if operation == "build_memory" and request.schema is StoryStateV1:
            lease_owner = f"pid-{os.getpid()}"
            task, attempt_id = self.repository.start_attempt(task.id, lease_owner=lease_owner)
            previous = context.get("previous_story_state")
            try:
                value = StoryStateV1.model_validate(previous) if previous else StoryStateV1(project_id=snapshot.project_id, branch_name=snapshot.branch_name)
            except Exception:
                value = StoryStateV1(project_id=snapshot.project_id, branch_name=snapshot.branch_name)
            artifact = self._store_task_output(
                snapshot, task.id, operation, value,
                self._demo_response(route, request, value), request, text_suffix,
                context, context_artifact.id,
            )
            response = self._demo_response(route, request, value)
            self.repository.finish_task(task.id, attempt_id, artifact.id, response)
            return TaskResult(task.id, artifact.id, value, response)

        while True:
            self._checkpoint(snapshot.id)
            if task.status in {"failed", "needs_retry"}:
                self.repository.retry_task(task.id)
            lease_owner = f"pid-{os.getpid()}"
            task, attempt_id = self.repository.start_attempt(task.id, lease_owner=lease_owner)
            input_hash = hashlib.sha256(
                (request.system_prompt + "\0" + request.user_prompt).encode("utf-8")
            ).hexdigest()
            if self.demo_mode:
                try:
                    value = self._demo_value(operation, context)
                    if validate_value is not None:
                        value = validate_value(value)
                    response = self._demo_response(route, request, value)
                    if stream_text and on_text_delta is not None:
                        await on_text_delta(str(value))
                    artifact = self._store_task_output(
                        snapshot,
                        task.id,
                        operation,
                        value,
                        response,
                        request,
                        text_suffix,
                        context,
                        context_artifact.id,
                    )
                    self.repository.finish_task(task.id, attempt_id, artifact.id, response)
                    await self.events.publish(
                        snapshot.id, "task_completed", task_id=task.id, operation=operation
                    )
                    return TaskResult(task.id, artifact.id, value, response)
                except Exception as exc:
                    self.repository.fail_task(task.id, attempt_id, exc, status="failed")
                    raise

            estimated_tokens = max(
                1,
                math.ceil((len(request.system_prompt) + len(request.user_prompt)) / 3)
                + int(request.max_output_tokens or 4_096),
            )
            estimated_usage = ModelUsage(
                input_tokens=max(1, math.ceil(len(request.user_prompt) / 3)),
                output_tokens=int(request.max_output_tokens or 4_096),
            )
            fallback_route = self._fallback_route_for(snapshot, request.role)
            fallback_eligible = (
                snapshot.provider_preset == "hybrid-quality"
                and request.schema is not None
                and route.provider == ProviderName.DEEPSEEK
            )
            reserved_tokens = estimated_tokens * (2 if fallback_eligible else 1)
            reserved_cost = _model_cost_usd(route, estimated_usage) * 3
            if fallback_eligible:
                reserved_cost = max(
                    reserved_cost,
                    _model_cost_usd(route, estimated_usage)
                    + _model_cost_usd(fallback_route, estimated_usage),
                )
            budget = self._budget(snapshot)
            reservation = await budget.reserve(
                calls=3,
                tokens=reserved_tokens,
                cost_usd=reserved_cost,
            )
            try:
                active_route = route
                extra_calls = 0
                self.repository.mark_request_dispatched(
                    task.id,
                    attempt_id,
                    request_hash=input_hash,
                )

                async def invoke(call_route: ModelRoute) -> tuple[LLMResponse, Any]:
                    if stream_text:
                        call_response = await self.providers.stream_complete_on(
                            call_route,
                            request,
                            on_delta=on_text_delta,
                        )
                    else:
                        call_response = await self.providers.complete_on(call_route, request)
                    call_value = call_response.parsed if request.schema is not None else call_response.text
                    if validate_value is not None:
                        call_value = validate_value(call_value)
                    return call_response, call_value

                try:
                    response, value = await self._invoke_with_lease_heartbeat(
                        task.id,
                        attempt_id,
                        lease_owner,
                        lambda route=active_route: invoke(route),
                    )
                except StructuredOutputError:
                    may_upgrade = (
                        snapshot.provider_preset == "hybrid-quality"
                        and request.schema is not None
                        and active_route.provider == ProviderName.DEEPSEEK
                        and task.attempt_count >= task.max_attempts
                    )
                    if not may_upgrade:
                        raise
                    active_route = fallback_route
                    extra_calls = 1
                    response, value = await self._invoke_with_lease_heartbeat(
                        task.id,
                        attempt_id,
                        lease_owner,
                        lambda route=active_route: invoke(route),
                    )
                response.cost_usd = _model_cost_usd(active_route, response.usage)
                artifact = self._store_task_output(
                    snapshot,
                    task.id,
                    operation,
                    value,
                    response,
                    request,
                    text_suffix,
                    context,
                    context_artifact.id,
                )
                self.repository.mark_response_landed(
                    task.id,
                    attempt_id,
                    artifact.id,
                    response,
                    request=request,
                    input_hash=input_hash,
                    output_hash=artifact.sha256,
                )
                self.repository.finish_task(task.id, attempt_id, artifact.id, response)
                await budget.commit(
                    reservation,
                    calls=response.attempts + extra_calls,
                    tokens=response.usage.total_tokens,
                    cost_usd=response.cost_usd,
                )
                warning_fraction = float(snapshot.budget.get("warning_fraction", 0.8) or 0.8)
                if budget.usage_fraction >= warning_fraction and snapshot.id not in self._budget_warned:
                    self._budget_warned.add(snapshot.id)
                    await self.events.publish(
                        snapshot.id,
                        "budget_warning",
                        usage_fraction=round(budget.usage_fraction, 4),
                    )
                await self.events.publish(
                    snapshot.id,
                    "task_completed",
                    task_id=task.id,
                    operation=operation,
                    provider=active_route.provider.value,
                    model=active_route.model,
                )
                return TaskResult(task.id, artifact.id, value, response)
            except asyncio.CancelledError as exc:
                await budget.commit(
                    reservation,
                    calls=reservation.calls,
                    tokens=reservation.tokens,
                    cost_usd=reservation.cost_usd,
                )
                status = "cancelled" if snapshot.id in self._force_cancelled else "indeterminate"
                self.repository.fail_task(task.id, attempt_id, exc, status=status)
                raise
            except AuthenticationProviderError as exc:
                await budget.release(reservation)
                self.repository.fail_task(task.id, attempt_id, exc, status="failed")
                raise
            except IndeterminateProviderError as exc:
                await budget.commit(
                    reservation,
                    calls=reservation.calls,
                    tokens=reservation.tokens,
                    cost_usd=reservation.cost_usd,
                )
                self.repository.fail_task(task.id, attempt_id, exc, status="indeterminate")
                raise IndeterminateTaskBlocked(str(exc)) from exc
            except StructuredOutputError as exc:
                await budget.commit(
                    reservation,
                    calls=reservation.calls,
                    tokens=reservation.tokens,
                    cost_usd=reservation.cost_usd,
                )
                can_retry = task.attempt_count < task.max_attempts
                self.repository.fail_task(
                    task.id,
                    attempt_id,
                    exc,
                    status="needs_retry" if can_retry else "failed",
                )
                if not can_retry:
                    raise
                task.status = "needs_retry"
            except RetryableProviderError as exc:
                # Provider adapters already exhausted the shared three-attempt
                # HTTP retry envelope.  Replaying at the task layer would turn
                # three network attempts into nine.
                await budget.commit(
                    reservation,
                    calls=reservation.calls,
                    tokens=reservation.tokens,
                    cost_usd=reservation.cost_usd,
                )
                self.repository.fail_task(task.id, attempt_id, exc, status="failed")
                raise
            except LLMProviderError as exc:
                await budget.commit(
                    reservation,
                    calls=1,
                    tokens=reservation.tokens,
                    cost_usd=reservation.cost_usd,
                )
                self.repository.fail_task(task.id, attempt_id, exc, status="failed")
                raise
            except Exception as exc:
                await budget.release(reservation)
                self.repository.fail_task(task.id, attempt_id, exc, status="failed")
                raise

    def _store_task_output(
        self,
        snapshot: RunSnapshot,
        task_id: UUID,
        operation: str,
        value: Any,
        response: LLMResponse,
        request: LLMRequest,
        text_suffix: str,
        context: Mapping[str, Any],
        context_artifact_id: UUID,
    ) -> ArtifactRecord:
        config = {
            "provider": response.provider.value,
            "model": response.model,
            "request_id": response.request_id,
            "finish_status": response.finish_status,
            "prompt_name": request.metadata.get("prompt_name"),
            "prompt_version": request.metadata.get("prompt_version"),
            "prompt_sha256": request.metadata.get("prompt_sha256"),
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
            "cached_tokens": response.usage.cached_input_tokens,
            "cost_usd": response.cost_usd,
            "duration_ms": round(response.elapsed_seconds * 1000),
            "provider_attempts": response.attempts,
        }
        brief_value = context.get("brief")
        if isinstance(brief_value, BaseModel):
            brief_value = brief_value.model_dump(mode="python")
        if isinstance(brief_value, Mapping):
            config.update(
                {
                    "chapter_id": str(brief_value.get("chapter_id") or ""),
                    "chapter_number": brief_value.get("number"),
                    "title": brief_value.get("title"),
                    "target_chars": brief_value.get("target_chars"),
                    "pov_characters": list(brief_value.get("pov_characters") or []),
                    "pov": "、".join(str(item) for item in (brief_value.get("pov_characters") or [])),
                }
            )
        if operation == "revise_chapter":
            manifest = context.get("revision_manifest") or []
            revision_number = int(context.get("revision_number", 1) or 1)
            config["revision"] = revision_number
            config["artifact_version"] = revision_number + 1
            config["production_round"] = int(context.get("production_round", 0) or 0)
            config["revision_issue_ids"] = [
                str(item.get("issue_id"))
                for item in manifest
                if isinstance(item, Mapping) and item.get("issue_id")
            ]
        elif operation == "write_chapter":
            config["revision"] = 0
            config["artifact_version"] = 1
            config["production_round"] = int(context.get("production_round", 0) or 0)
        if isinstance(value, BaseModel) or isinstance(value, (dict, list)):
            return self.repository.store_json(
                project_id=snapshot.project_id,
                run_id=snapshot.id,
                task_id=task_id,
                kind=operation,
                value=value,
                source_artifact_ids=[context_artifact_id],
                generation_config=config,
            )
        return self.repository.store_text(
            project_id=snapshot.project_id,
            run_id=snapshot.id,
            task_id=task_id,
            kind=operation,
            text=str(value),
            source_artifact_ids=[context_artifact_id],
            generation_config=config,
            suffix=text_suffix,
        )

    def _read_task_value(self, artifact_id: UUID, schema: type[BaseModel] | dict[str, Any] | None) -> Any:
        if schema is None:
            return self.repository.read_text(artifact_id)
        value = self.repository.read_json(artifact_id)
        if isinstance(schema, dict):
            return value
        return schema.model_validate(value)

    def _demo_response(self, route: ModelRoute, request: LLMRequest, value: Any) -> LLMResponse:
        if isinstance(value, BaseModel):
            text = json.dumps(value.model_dump(mode="json"), ensure_ascii=False)
        elif isinstance(value, (dict, list)):
            text = json.dumps(_jsonable(value), ensure_ascii=False)
        else:
            text = str(value)
        return LLMResponse(
            provider=route.provider,
            model=f"local-demo:{route.model}",
            request_id=f"demo_{uuid4().hex}",
            text=text,
            parsed=value if request.schema is not None else None,
            finish_status="completed",
            usage=ModelUsage(),
            attempts=0,
        )

    def _demo_value(self, operation: str, context: Mapping[str, Any]) -> Any:
        if operation == "analyze_chunk":
            source_span = SourceSpanV1.model_validate(context["source_span"])
            text = str(context.get("source_text") or "")
            excerpt = next((part.strip() for part in text.splitlines() if part.strip()), "")
            dimensions = {
                dimension: DimensionAnalysisV1(
                    summary=(excerpt[:160] or f"{dimension.value} 暂无显式信息"),
                    facts=([excerpt[:160]] if excerpt else []),
                    confidence=0.9 if excerpt else 0.5,
                )
                for dimension in SUMMARY_DIMENSIONS
            }
            return ChunkAnalysisV1(
                chunk_id=str(context["chunk_id"]),
                source_span=source_span,
                sequence_index=int(context["sequence_index"]),
                dimensions=dimensions,
            )
        if operation == "merge_story_state":
            previous = StoryStateV1.model_validate(context["previous_state"])
            analysis = ChunkAnalysisV1.model_validate(context["chunk_analysis"])
            facts = list(previous.facts)
            seen = {item.statement for item in facts}
            for dimension in SUMMARY_DIMENSIONS:
                for statement in analysis.dimensions[dimension].facts:
                    if statement and statement not in seen:
                        facts.append(StoryFactV1(category=dimension.value, statement=statement))
                        seen.add(statement)
            return previous.model_copy(
                deep=True,
                update={
                    "facts": facts,
                    "revision": previous.revision + 1,
                    "parent_state_id": previous.state_id,
                },
            )
        if operation in {"build_plan", "revise_plan"}:
            target = int(context["target_chars"])
            count = max(1, math.ceil(target / 5_000))
            base, remainder = divmod(target, count)
            briefs = [
                ChapterBriefV1(
                    number=index + 1,
                    title=f"第{index + 1}章 续页",
                    target_chars=base + (1 if index < remainder else 0),
                    goals=["承接前情并推进一次可验证的状态变化"],
                    required_beats=[f"本章关键节点{index + 1}"],
                )
                for index in range(count)
            ]
            return RunPlanV1(
                run_id=UUID(str(context["run_id"])),
                branch_name=str(context.get("branch_name") or "main"),
                start_point="现有记忆终态之后",
                target_chars=target,
                chapter_briefs=briefs,
            )
        if operation in {"write_chapter", "revise_chapter"}:
            if operation == "revise_chapter":
                return str(context["chapter_text"])
            brief = ChapterBriefV1.model_validate(context["brief"])
            parts = [f"# {brief.title}", "", *brief.required_beats]
            sentence_no = 1
            while sum(1 for char in "\n".join(parts) if not char.isspace()) < brief.target_chars:
                parts.append(f"灯影沿着长街向前移动，人物完成了第{sentence_no}次观察，并让新的因果落到现场。")
                sentence_no += 1
            return "\n\n".join(parts).rstrip() + "。"
        if operation == "extract_delta":
            return ChapterDeltaV1(
                run_id=UUID(str(context["run_id"])),
                chapter_id=UUID(str(context["chapter_id"])),
                chapter_number=int(context["chapter_number"]),
                source_artifact_id=UUID(str(context["source_artifact_id"])),
                end_state_summary="本章关键节点完成，人物状态保持连续。",
            )
        if operation == "review_dimension_group":
            return ReviewGroupOutput(
                dimension_results={
                    key: ReviewGroupDimension(score=94, summary="未发现阻断问题")
                    for key in context["dimension_keys"]
                },
                summary="本组通过",
            )
        if operation == "consolidate_reviews":
            return ReviewGroupOutput(
                dimension_results={
                    item.value: ReviewGroupDimension(score=94, summary="证据检查通过")
                    for item in ReviewDimension
                },
                summary="九维汇总通过",
            )
        if operation == "arbitrate_chapter":
            manifest = list(context.get("current_revision_manifest") or [])
            return ArbitrationOutput(
                decision="revise" if manifest else "needs_human",
                revision_manifest=manifest,
            )
        if operation == "global_review":
            return GlobalReviewOutput(
                dimension_results={
                    dimension.value: ReviewGroupDimension(
                        score=94,
                        summary="全轮衔接与九维审计通过",
                    )
                    for dimension in ReviewDimension
                },
                summary="全轮衔接与九维审计通过",
            )
        if operation == "build_memory":
            deltas = list(context.get("chapter_deltas") or [])
            last = deltas[-1] if deltas else None
            if isinstance(last, BaseModel):
                last = last.model_dump(mode="python")
            summary = str((last or {}).get("end_state_summary", "本轮结束"))
            previous_value = context.get("previous_story_state") or {}
            try:
                previous = StoryStateV1.model_validate(previous_value)
            except Exception:
                previous = StoryStateV1(
                    project_id=UUID(str(context["project_id"])),
                    branch_name=str(context.get("branch_name") or "main"),
                )
            facts = list(previous.facts)
            facts.append(
                StoryFactV1(
                    category="continuation_end_state",
                    statement=summary,
                    immutable_canon=False,
                )
            )
            return previous.model_copy(
                deep=True,
                update={
                    "facts": facts,
                    "revision": previous.revision + 1,
                    "parent_state_id": previous.state_id,
                },
            )
        raise OrchestrationError(f"demo mode does not implement operation: {operation}")


def create_runtime(
    session_factory: sessionmaker[Session],
    artifact_store: ArtifactStore,
    *,
    provider_registry: ProviderRegistry | None = None,
    prompt_store: PromptStore | None = None,
    demo_mode: bool | None = None,
    delivery_root: Path | str | None = None,
    worker_count: int = 1,
) -> OrchestratorRuntime:
    return OrchestratorRuntime(
        session_factory,
        artifact_store,
        provider_registry=provider_registry,
        prompt_store=prompt_store,
        demo_mode=demo_mode,
        delivery_root=delivery_root,
        worker_count=worker_count,
    )
