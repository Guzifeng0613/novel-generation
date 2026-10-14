"""Persistent orchestration index.

Large model inputs and outputs do not live in SQLite.  These tables only keep
state, dependency metadata and immutable artifact references.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def new_uuid() -> str:
    return str(uuid4())


def utc_now() -> datetime:
    return datetime.now(UTC)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class Project(TimestampMixin, Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(240), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False, index=True)
    active_branch: Mapped[str] = mapped_column(String(120), default="main", nullable=False)
    data_path: Mapped[str] = mapped_column(Text, nullable=False)
    settings_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    current_story_state_artifact_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    runs: Mapped[list[Run]] = relationship(back_populates="project", cascade="all, delete-orphan")
    artifacts: Mapped[list[Artifact]] = relationship(back_populates="project", cascade="all, delete-orphan")


class BranchHead(TimestampMixin, Base):
    __tablename__ = "branch_heads"
    __table_args__ = (
        UniqueConstraint("project_id", "branch_name", name="uq_branch_head_project_name"),
        Index("ix_branch_heads_project", "project_id", "branch_name"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    branch_name: Mapped[str] = mapped_column(String(120), nullable=False)
    artifact_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    promoted_by_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    finalization_owner_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    finalization_lock_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    finalization_lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class Run(TimestampMixin, Base):
    __tablename__ = "runs"
    __table_args__ = (
        Index("ix_runs_project_created", "project_id", "created_at"),
        Index("ix_runs_project_branch_status", "project_id", "branch_name", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)  # import / analysis / continuation
    status: Mapped[str] = mapped_column(String(40), default="draft", nullable=False, index=True)
    stage: Mapped[str] = mapped_column(String(64), default="created", nullable=False)
    branch_name: Mapped[str] = mapped_column(String(120), default="main", nullable=False)
    target_chars: Mapped[int | None] = mapped_column(Integer, nullable=True)
    provider_preset: Mapped[str] = mapped_column(String(40), default="hybrid-quality", nullable=False)
    user_requirements: Mapped[str] = mapped_column(Text, default="", nullable=False)
    plan_artifact_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    plan_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    budget_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    usage_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    pause_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    project: Mapped[Project] = relationship(back_populates="runs")
    tasks: Mapped[list[Task]] = relationship(back_populates="run", cascade="all, delete-orphan")
    artifacts: Mapped[list[Artifact]] = relationship(back_populates="run")
    model_calls: Mapped[list[ModelCall]] = relationship(back_populates="run")


class Task(TimestampMixin, Base):
    __tablename__ = "tasks"
    __table_args__ = (
        UniqueConstraint("run_id", "logical_key", name="uq_tasks_run_logical_key"),
        Index("ix_tasks_run_status_sequence", "run_id", "status", "sequence_index"),
        Index("ix_tasks_lease", "status", "lease_expires_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), nullable=False)
    operation: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="pending", nullable=False)
    sequence_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    logical_key: Mapped[str] = mapped_column(String(240), nullable=False)
    provider: Mapped[str | None] = mapped_column(String(40), nullable=True)
    model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    input_artifact_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    context_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    constraints_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    budget_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    dependency_task_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    output_artifact_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(120), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    run: Mapped[Run] = relationship(back_populates="tasks")
    attempts: Mapped[list[Attempt]] = relationship(back_populates="task", cascade="all, delete-orphan")
    model_calls: Mapped[list[ModelCall]] = relationship(back_populates="task")


class Attempt(Base):
    __tablename__ = "attempts"
    __table_args__ = (UniqueConstraint("task_id", "attempt_no", name="uq_attempt_task_number"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="started", nullable=False)
    phase: Mapped[str] = mapped_column(String(40), default="claimed", nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    response_landed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    response_artifact_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    provider_request_id: Mapped[str | None] = mapped_column(String(240), nullable=True)
    request_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    response_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    finish_reason: Mapped[str | None] = mapped_column(String(80), nullable=True)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cached_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    task: Mapped[Task] = relationship(back_populates="attempts")
    model_calls: Mapped[list[ModelCall]] = relationship(back_populates="attempt")


class Artifact(Base):
    __tablename__ = "artifacts"
    __table_args__ = (
        Index("ix_artifacts_project_kind_created", "project_id", "kind", "created_at"),
        Index("ix_artifacts_sha256", "sha256"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.id", ondelete="SET NULL"), nullable=True)
    task_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True)
    kind: Mapped[str] = mapped_column(String(80), nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    relative_path: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    media_type: Mapped[str] = mapped_column(String(160), default="application/octet-stream", nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    source_artifact_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    generation_config_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    immutable: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    project: Mapped[Project] = relationship(back_populates="artifacts")
    run: Mapped[Run | None] = relationship(back_populates="artifacts", foreign_keys=[run_id])


class PromptVersion(Base):
    __tablename__ = "prompt_versions"
    __table_args__ = (UniqueConstraint("name", "version", name="uq_prompt_name_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    version: Mapped[str] = mapped_column(String(40), nullable=False)
    operation: Mapped[str] = mapped_column(String(80), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(20), default="1.0", nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class ModelCall(Base):
    __tablename__ = "model_calls"
    __table_args__ = (
        Index("ix_model_calls_run_created", "run_id", "created_at"),
        Index("ix_model_calls_provider_request", "provider", "provider_request_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), nullable=False)
    task_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True)
    attempt_id: Mapped[str | None] = mapped_column(
        ForeignKey("attempts.id", ondelete="SET NULL"),
        nullable=True,
    )
    prompt_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("prompt_versions.id", ondelete="SET NULL"), nullable=True
    )
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    model: Mapped[str] = mapped_column(String(120), nullable=False)
    provider_request_id: Mapped[str | None] = mapped_column(String(240), nullable=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    output_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    finish_status: Mapped[str | None] = mapped_column(String(80), nullable=True)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cached_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cache_hit: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    call_details_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    run: Mapped[Run] = relationship(back_populates="model_calls")
    task: Mapped[Task | None] = relationship(back_populates="model_calls")
    attempt: Mapped[Attempt | None] = relationship(back_populates="model_calls")
