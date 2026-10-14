"""Read models and immutable artifacts into stable HTTP response shapes."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from . import models
from .storage import ArtifactStore


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


def _public_run_status(status: str) -> str:
    return {
        "approved": "queued",
        "awaiting_approval": "awaiting_outline_approval",
        "waiting_approval": "awaiting_outline_approval",
        "canceled": "cancelled",
    }.get(status, status)


def _artifact_title(artifact: models.Artifact) -> str:
    config = artifact.generation_config_json or {}
    if artifact.kind in {"source_archive_chunk", "source_model_chunk"}:
        chunk_label = "归档段" if artifact.kind == "source_archive_chunk" else "模型子块"
        sequence = config.get("sequence_index")
        suffix = f" {int(sequence) + 1}" if isinstance(sequence, int) else ""
        return f"{config.get('source_name', '源稿')} · {chunk_label}{suffix}"
    if artifact.kind.startswith("summary_"):
        dimension_labels = {
            "core_summary": "核心摘要",
            "story_outline": "故事大纲",
            "characters": "角色",
            "conflicts": "冲突",
            "core_mysteries": "核心悬念",
            "worldbuilding": "背景 / 世界观",
            "narrative_pov": "叙事视角",
            "pacing": "节奏控制",
            "foreshadowing": "伏笔追踪",
        }
        dimension = str(config.get("dimension", ""))
        variant = "完全版" if config.get("variant") == "full" else "精简版"
        return f"{dimension_labels.get(dimension, dimension or '九维总结')}（{variant}）"
    title = config.get("title") or config.get("source_name") or config.get("filename")
    if title:
        return str(title)
    labels = {
        "source_document": "源稿",
        "import_manifest": "导入清单",
        "story_state": "九维故事状态",
        "run_plan": "续写章纲",
        "chapter_text": "章节正文",
        "chapter_final": "定稿章节",
        "review_report": "审校报告",
        "delivery_zip": "阅读包",
        "project_bundle": "可恢复项目包",
    }
    return labels.get(artifact.kind, artifact.kind.replace("_", " "))


class ViewBuilder:
    """Build API views without making SQLite the source of artifact content."""

    def __init__(self, session_factory: sessionmaker[Session], artifact_store: ArtifactStore) -> None:
        self.session_factory = session_factory
        self.artifact_store = artifact_store

    def _artifact_json(self, artifact: models.Artifact | None) -> Any:
        if artifact is None:
            return None
        try:
            return json.loads(self.artifact_store.read_text(artifact.relative_path))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return None

    def _artifact_text(self, artifact: models.Artifact | None) -> str | None:
        if artifact is None:
            return None
        try:
            return self.artifact_store.read_text(artifact.relative_path)
        except (OSError, UnicodeDecodeError, ValueError):
            return None

    @staticmethod
    def artifact(artifact: models.Artifact) -> dict[str, Any]:
        return {
            "id": artifact.id,
            "title": _artifact_title(artifact),
            "kind": artifact.kind,
            "version": artifact.version,
            "created_at": _iso(artifact.created_at),
            "size": artifact.byte_size,
            "media_type": artifact.media_type,
            "sha256": artifact.sha256,
        }

    @staticmethod
    def _public_project_artifact(artifact: models.Artifact) -> bool:
        return artifact.kind not in {
            "task_input_context",
            "analyze_chunk",
            "merge_story_state",
            "build_plan",
            "revise_plan",
            "review_dimension_group",
            "consolidate_reviews",
            "build_memory",
        }

    def project_summary(self, project: models.Project, session: Session) -> dict[str, Any]:
        latest_run = session.scalar(
            select(models.Run)
            .where(models.Run.project_id == project.id)
            .order_by(models.Run.created_at.desc())
            .limit(1)
        )
        source_count = session.scalar(
            select(func.count(models.Artifact.id)).where(
                models.Artifact.project_id == project.id,
                models.Artifact.kind.in_(["source_document", "source_archive"]),
            )
        )
        return {
            "id": project.id,
            "name": project.name,
            "status": project.status,
            "created_at": _iso(project.created_at),
            "updated_at": _iso(project.updated_at),
            "source_count": int(source_count or 0),
            "latest_run_id": latest_run.id if latest_run else None,
        }

    def list_projects(self) -> list[dict[str, Any]]:
        with self.session_factory() as session:
            projects = session.scalars(
                select(models.Project).order_by(models.Project.updated_at.desc())
            ).all()
            return [self.project_summary(project, session) for project in projects]

    def project_detail(self, project_id: str) -> dict[str, Any]:
        with self.session_factory() as session:
            project = session.get(models.Project, project_id)
            if project is None:
                raise KeyError(f"project not found: {project_id}")
            artifacts = session.scalars(
                select(models.Artifact)
                .where(models.Artifact.project_id == project.id)
                .order_by(models.Artifact.created_at.desc())
            ).all()
            runs = session.scalars(
                select(models.Run)
                .where(models.Run.project_id == project.id)
                .order_by(models.Run.created_at.desc())
            ).all()
            story_artifact = (
                session.get(models.Artifact, project.current_story_state_artifact_id)
                if project.current_story_state_artifact_id
                else None
            )
            summary = self.project_summary(project, session)
            summary.update(
                {
                    "description": project.description,
                    "active_branch": project.active_branch,
                    "sources": [
                        self.artifact(item)
                        for item in artifacts
                        if item.kind in {"source_document", "source_archive"}
                    ],
                    "artifacts": [
                        self.artifact(item) for item in artifacts if self._public_project_artifact(item)
                    ],
                    "story_state": self._story_state_view(self._artifact_json(story_artifact)),
                    "runs": [self._run_summary(item) for item in runs],
                }
            )
            return summary

    @staticmethod
    def _run_summary(run: models.Run) -> dict[str, Any]:
        return {
            "id": run.id,
            "project_id": run.project_id,
            "kind": run.kind,
            "status": _public_run_status(run.status),
            "stage": run.stage,
            "branch_name": run.branch_name,
            "created_at": _iso(run.created_at),
            "updated_at": _iso(run.updated_at),
            "routing_preset": run.provider_preset,
            "target_chars": run.target_chars,
        }

    @staticmethod
    def _story_state_view(value: Any) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            return None

        def adapt(
            items: Any,
            *,
            id_keys: tuple[str, ...],
            title_keys: tuple[str, ...],
            detail_keys: tuple[str, ...] = (),
        ) -> list[Any]:
            if not isinstance(items, list):
                return []
            result: list[Any] = []
            for index, item in enumerate(items):
                if not isinstance(item, dict):
                    result.append(item)
                    continue
                identifier = next((item.get(key) for key in id_keys if item.get(key)), None)
                title = next((item.get(key) for key in title_keys if item.get(key)), None)
                detail = next((item.get(key) for key in detail_keys if item.get(key)), None)
                result.append(
                    {
                        **item,
                        "id": str(identifier or f"story-item-{index}"),
                        "title": str(title or "未命名条目"),
                        "detail": str(detail or ""),
                    }
                )
            return result

        mysteries = adapt(
            value.get("mysteries", value.get("suspense", [])),
            id_keys=("mystery_id", "id"),
            title_keys=("label", "title", "subject"),
            detail_keys=("follow_up", "subject"),
        )
        foreshadowing = adapt(
            value.get("foreshadowing", []),
            id_keys=("foreshadowing_id", "id"),
            title_keys=("description", "title"),
            detail_keys=("status", "planted_at"),
        )
        return {
            "canon": adapt(
                value.get("facts", value.get("canon", [])),
                id_keys=("fact_id", "id"),
                title_keys=("statement", "title"),
                detail_keys=("category",),
            ),
            "suspense": [*mysteries, *foreshadowing],
            "entities": adapt(
                value.get("entities", []),
                id_keys=("entity_id", "id"),
                title_keys=("canonical_name", "name", "title"),
                detail_keys=("description", "kind"),
            ),
            "conflicts": adapt(
                value.get("conflicts", []),
                id_keys=("conflict_id", "id"),
                title_keys=("description", "title"),
                detail_keys=("stakes", "status"),
            ),
            "world_rules": adapt(
                value.get("world_rules", []),
                id_keys=("rule_id", "id"),
                title_keys=("statement", "title"),
            ),
        }

    @staticmethod
    def _plan_view(value: Any, run: models.Run) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            return None
        raw_chapters = value.get("chapter_briefs", value.get("chapters", []))
        chapters: list[dict[str, Any]] = []
        if isinstance(raw_chapters, list):
            for index, raw in enumerate(raw_chapters, start=1):
                if not isinstance(raw, dict):
                    continue
                goals = [str(item) for item in raw.get("goals", [])]
                beats = [str(item) for item in raw.get("required_beats", [])]
                summary = "；".join([*goals, *beats])
                expected = raw.get("expected_state_changes", raw.get("expected_end_state", ""))
                if isinstance(expected, (dict, list)):
                    expected = json.dumps(expected, ensure_ascii=False)
                chapters.append(
                    {
                        **raw,
                        "id": raw.get("chapter_id", raw.get("id", f"chapter-{index}")),
                        "index": raw.get("number", raw.get("index", index)),
                        "summary": raw.get("summary", summary),
                        "pov": "、".join(str(item) for item in raw.get("pov_characters", [])),
                        "dependencies": raw.get("dependency_chapter_ids", raw.get("dependencies", [])),
                        "expected_end_state": expected,
                    }
                )
        raw_conflicts = value.get("canon_conflicts", [])
        conflicts = (
            [
                {
                    **item,
                    "message": item.get("explanation") or item.get("requested_change") or "Canon 冲突",
                }
                if isinstance(item, dict)
                else item
                for item in raw_conflicts
            ]
            if isinstance(raw_conflicts, list)
            else []
        )
        style_card = value.get("style_card", "")
        if isinstance(style_card, (dict, list)):
            style_card = json.dumps(style_card, ensure_ascii=False)
        return {
            **value,
            "id": value.get("plan_id", value.get("id")),
            "status": (
                "approved"
                if run.approved_at
                else "awaiting_outline_approval"
                if run.plan_artifact_id
                else "draft"
            ),
            "approved_hash": run.plan_hash if run.approved_at else None,
            "premise": value.get("start_point", value.get("premise", "")),
            "style_card": style_card,
            "user_requirements": run.user_requirements,
            "canon_conflicts": conflicts,
            "chapters": chapters,
        }

    @staticmethod
    def _chapter_brief(
        artifact: models.Artifact,
        task_contexts: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        context = task_contexts.get(artifact.task_id or "", {})
        brief = context.get("brief", {}) if isinstance(context, dict) else {}
        return brief if isinstance(brief, dict) else {}

    @staticmethod
    def _chapter_revision(artifact: models.Artifact) -> int:
        """Return the authored revision, not the artifact-kind version.

        A final chapter artifact promotes an already-authored revision and can
        therefore have a different database version from its source.  Scene
        fragments likewise share revision zero.  Diff selection must collapse
        both cases instead of treating adjacent artifact rows as revisions.
        """

        config = artifact.generation_config_json or {}
        revision = config.get("revision")
        try:
            return max(0, int(revision))
        except (TypeError, ValueError):
            return max(0, int(artifact.version or 1) - 1)

    @staticmethod
    def _chapter_candidate_priority(artifact: models.Artifact) -> tuple[int, datetime]:
        config = artifact.generation_config_json or {}
        if artifact.kind == "chapter_final":
            priority = 3
        elif config.get("status") == "assembled":
            priority = 2
        elif artifact.task_id is None:
            priority = 1
        else:
            priority = 0
        return priority, artifact.created_at

    @classmethod
    def _chapter_key(
        cls,
        artifact: models.Artifact,
        task_contexts: dict[str, dict[str, Any]],
    ) -> str:
        config = artifact.generation_config_json or {}
        brief = cls._chapter_brief(artifact, task_contexts)
        return str(
            config.get("chapter_id")
            or brief.get("chapter_id")
            or config.get("chapter_number")
            or brief.get("number")
            or config.get("number")
            or artifact.id
        )

    def _chapter_views(
        self,
        artifacts: list[models.Artifact],
        plan: dict[str, Any] | None,
        task_contexts: dict[str, dict[str, Any]],
        run_status: str,
    ) -> list[dict[str, Any]]:
        text_artifacts = [
            item
            for item in artifacts
            if "chapter" in item.kind
            and not any(token in item.kind for token in ("delta", "review", "plan", "brief"))
            and item.media_type.startswith("text/")
        ]
        grouped: dict[str, list[models.Artifact]] = defaultdict(list)
        for artifact in text_artifacts:
            grouped[self._chapter_key(artifact, task_contexts)].append(artifact)

        plan_chapters = (plan or {}).get("chapters", [])
        plan_by_id = {str(item.get("id")): item for item in plan_chapters if isinstance(item, dict)}
        plan_by_number = {
            int(item.get("index")): item
            for item in plan_chapters
            if isinstance(item, dict) and str(item.get("index", "")).isdigit()
        }
        chapters: list[dict[str, Any]] = []
        for key, versions in grouped.items():
            authored_versions: dict[int, list[models.Artifact]] = defaultdict(list)
            for artifact in versions:
                authored_versions[self._chapter_revision(artifact)].append(artifact)
            selected_versions = [
                max(candidates, key=self._chapter_candidate_priority)
                for _, candidates in sorted(authored_versions.items())
            ]
            current = selected_versions[-1]
            config = current.generation_config_json or {}
            task_brief = self._chapter_brief(current, task_contexts)
            number = int(
                config.get(
                    "chapter_number",
                    task_brief.get("number", config.get("number", 0)),
                )
                or 0
            )
            brief = (
                plan_by_id.get(str(config.get("chapter_id") or task_brief.get("chapter_id") or key))
                or plan_by_number.get(number)
                or task_brief
                or {}
            )
            content = self._artifact_text(current) or ""
            previous = self._artifact_text(selected_versions[-2]) if len(selected_versions) > 1 else None
            pov = config.get("pov", brief.get("pov", brief.get("pov_characters")))
            if isinstance(pov, list):
                pov = "、".join(str(item) for item in pov)
            chapters.append(
                {
                    "id": str(config.get("chapter_id") or brief.get("id") or brief.get("chapter_id") or key),
                    "index": number or int(brief.get("index", len(chapters) + 1)),
                    "title": str(config.get("title") or brief.get("title") or _artifact_title(current)),
                    "status": str(
                        config.get("status") or ("passed" if run_status == "completed" else "generated")
                    ),
                    "content": content,
                    "previous_content": previous,
                    "target_chars": config.get("target_chars", brief.get("target_chars")),
                    "actual_chars": len(content),
                    "revision": config.get("revision", len(versions) - 1),
                    "content_sha256": current.sha256,
                    "version_source": config.get("source", "model"),
                    "human_edited": config.get("source") == "human" or current.kind == "chapter_manual",
                    "editable": run_status in {"paused", "completed", "failed", "needs_human", "cancelled"},
                    "pov": pov,
                }
            )

        existing_ids = {item["id"] for item in chapters}
        for brief in plan_chapters:
            if not isinstance(brief, dict) or str(brief.get("id")) in existing_ids:
                continue
            chapters.append(
                {
                    "id": str(brief.get("id")),
                    "index": int(brief.get("index", len(chapters) + 1)),
                    "title": str(brief.get("title", "未命名章节")),
                    "status": "queued",
                    "target_chars": brief.get("target_chars"),
                    "actual_chars": 0,
                    "revision": 0,
                    "content_sha256": None,
                    "version_source": None,
                    "human_edited": False,
                    "editable": False,
                    "pov": brief.get("pov"),
                }
            )
        return sorted(chapters, key=lambda item: (item["index"], item["id"]))

    @staticmethod
    def _reference_index(story_state: dict[str, Any] | None) -> dict[tuple[str, str], dict[str, Any]]:
        index: dict[tuple[str, str], dict[str, Any]] = {}
        if not story_state:
            return index
        reference_groups = (
            ("canon", story_state.get("canon", [])),
            ("suspense", story_state.get("suspense", [])),
        )
        for kind, items in reference_groups:
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                identifier = str(item.get("id") or "")
                if not identifier:
                    continue
                index[(kind, identifier)] = {
                    "id": identifier,
                    "kind": kind,
                    "title": str(item.get("title") or "未命名依据"),
                    "detail": str(item.get("detail") or item.get("status") or ""),
                }
        return index

    @staticmethod
    def _id_values(value: Any) -> list[str]:
        if isinstance(value, str):
            return [value] if value else []
        if isinstance(value, list):
            return [str(item) for item in value if str(item)]
        return []

    @staticmethod
    def _task_view(task: models.Task) -> dict[str, Any]:
        retryable_statuses = {"failed", "indeterminate", "needs_retry"}
        retryable = task.status in retryable_statuses and task.attempt_count < task.max_attempts
        return {
            "id": task.id,
            "operation": task.operation,
            "status": task.status,
            "sequence_index": task.sequence_index,
            "provider": task.provider,
            "model": task.model,
            "attempt_count": task.attempt_count,
            "max_attempts": task.max_attempts,
            "retryable": retryable,
            "requires_acknowledgement": task.status == "indeterminate" and retryable,
            "error_code": task.error_code,
            "error_message": task.error_message,
            "updated_at": _iso(task.updated_at),
        }

    def _review_issues(
        self,
        artifacts: list[models.Artifact],
        story_state: dict[str, Any] | None,
        *,
        stale: bool = False,
    ) -> list[dict[str, Any]]:
        issues: dict[str, dict[str, Any]] = {}
        references = self._reference_index(story_state)
        review_artifacts = [
            item for item in artifacts if "review" in item.kind or item.kind.startswith("qa_")
        ]
        latest_reports: dict[str, tuple[models.Artifact, dict[str, Any]]] = {}
        for artifact in review_artifacts:
            report = self._artifact_json(artifact)
            if not isinstance(report, dict):
                continue
            report_key = str(report.get("chapter_id") or report.get("report_scope") or artifact.id)
            latest_reports[report_key] = (artifact, report)

        for artifact, report in latest_reports.values():
            chapter_id = report.get("chapter_id")
            candidates: list[Any] = []
            raw_issues = report.get("issues", report.get("review_issues", []))
            if isinstance(raw_issues, list):
                candidates.extend(raw_issues)
            dimensions = report.get("dimension_results", {})
            if isinstance(dimensions, dict):
                for dimension, result in dimensions.items():
                    if not isinstance(result, dict):
                        continue
                    for issue in result.get("issues", []):
                        if isinstance(issue, dict):
                            candidates.append({"dimension": dimension, **issue})
            for index, raw in enumerate(candidates):
                if isinstance(raw, str):
                    raw = {"description": raw}
                if not isinstance(raw, dict):
                    continue
                issue_id = str(raw.get("issue_id", raw.get("id", f"{artifact.id}-{index}")))
                start = raw.get("start_offset")
                end = raw.get("end_offset")
                location = raw.get("location")
                if not location and start is not None:
                    location = f"{start}:{end if end is not None else start}"
                reference_keys = [
                    *(
                        ("canon", item)
                        for item in self._id_values(raw.get("canon_fact_ids", raw.get("canonFactIds", [])))
                    ),
                    *(
                        ("suspense", item)
                        for item in self._id_values(raw.get("suspense_ids", raw.get("suspenseIds", [])))
                    ),
                ]
                issue_references = [references[key] for key in reference_keys if key in references]
                issues[issue_id] = {
                    "id": issue_id,
                    "severity": raw.get("severity", "note"),
                    "dimension": raw.get("dimension", "综合"),
                    "message": raw.get("description", raw.get("message", raw.get("title", "未提供问题描述"))),
                    "title": raw.get("title", ""),
                    "evidence": raw.get("evidence", ""),
                    "instruction": raw.get(
                        "revision_instruction", raw.get("instruction", raw.get("suggestion", ""))
                    ),
                    "location": location,
                    "start_offset": start if isinstance(start, int) else None,
                    "end_offset": end if isinstance(end, int) else None,
                    "references": issue_references,
                    "status": raw.get("status", "open"),
                    "chapter_id": raw.get("chapter_id", chapter_id),
                    "stale": stale,
                }
        return list(issues.values())

    def run_detail(self, run_id: str) -> dict[str, Any]:
        with self.session_factory() as session:
            run = session.get(models.Run, run_id)
            if run is None:
                raise KeyError(f"run not found: {run_id}")
            project = session.get(models.Project, run.project_id)
            artifacts = session.scalars(
                select(models.Artifact)
                .where(models.Artifact.run_id == run.id)
                .order_by(models.Artifact.created_at.asc())
            ).all()
            story_artifact = (
                session.get(models.Artifact, project.current_story_state_artifact_id)
                if project is not None and project.current_story_state_artifact_id
                else None
            )
            story_state = self._story_state_view(self._artifact_json(story_artifact))
            plan_artifact = (
                session.get(models.Artifact, run.plan_artifact_id) if run.plan_artifact_id else None
            )
            plan = self._plan_view(self._artifact_json(plan_artifact), run)
            tasks = session.scalars(
                select(models.Task)
                .where(models.Task.run_id == run.id)
                .order_by(models.Task.sequence_index.asc(), models.Task.created_at.asc())
            ).all()
            calls = session.scalars(select(models.ModelCall).where(models.ModelCall.run_id == run.id)).all()

            counts: dict[str, int] = defaultdict(int)
            for task in tasks:
                counts[task.status] += 1
            usage = dict(run.usage_json or {})
            call_tokens = sum(item.input_tokens + item.output_tokens for item in calls)
            call_cost = sum(item.cost_usd for item in calls)
            started = run.started_at or run.created_at
            finished = run.finished_at
            now = datetime.now(UTC)
            if started.tzinfo is None:
                started = started.replace(tzinfo=UTC)
            if finished and finished.tzinfo is None:
                finished = finished.replace(tzinfo=UTC)
            elapsed = max(0, round(((finished or now) - started).total_seconds()))
            budget = dict(run.budget_json or {})
            used_cost = float(usage.get("used_cost_usd", usage.get("cost_usd", call_cost)) or 0)
            max_cost = float(budget.get("max_cost_usd", budget.get("max_cost", 0)) or 0)
            warning = None
            if max_cost and used_cost / max_cost >= float(budget.get("warning_fraction", 0.8)):
                warning = "费用预算已使用 80% 以上"
            failed_task = next(
                (item for item in reversed(tasks) if item.error_message),
                None,
            )
            artifact_by_id = {item.id: item for item in artifacts}
            task_contexts: dict[str, dict[str, Any]] = {}
            for task in tasks:
                context_artifact_id = (task.context_json or {}).get("context_artifact_id")
                context_artifact = artifact_by_id.get(str(context_artifact_id))
                context = self._artifact_json(context_artifact)
                if isinstance(context, dict):
                    task_contexts[task.id] = context

            result = self._run_summary(run)
            result.update(
                {
                    "plan": plan,
                    "chapters": self._chapter_views(
                        artifacts,
                        plan,
                        task_contexts,
                        _public_run_status(run.status),
                    ),
                    "review_issues": self._review_issues(
                        artifacts,
                        story_state,
                        stale=bool(usage.get("content_revision_invalidated")),
                    ),
                    "tasks": [self._task_view(item) for item in tasks],
                    "artifacts": [self.artifact(item) for item in artifacts],
                    "metrics": {
                        "queued": counts.get("pending", 0) + counts.get("queued", 0),
                        "active": counts.get("running", 0),
                        "completed": counts.get("completed", 0),
                        "calls": int(usage.get("used_calls", usage.get("calls", len(calls))) or 0),
                        "tokens": int(usage.get("used_tokens", usage.get("tokens", call_tokens)) or 0),
                        "cost": used_cost,
                        "max_cost": max_cost,
                        "elapsed_seconds": elapsed,
                        "warning": warning,
                    },
                    "budget": budget,
                    "usage": usage,
                    "message": self._status_message(run),
                    "error": (failed_task.error_message if failed_task else usage.get("last_error")),
                    "content_revision_invalidated": bool(usage.get("content_revision_invalidated")),
                    "delivery_valid": not bool(usage.get("content_revision_invalidated")),
                }
            )
            return result

    @staticmethod
    def _status_message(run: models.Run) -> str:
        status = _public_run_status(run.status)
        messages = {
            "draft": "运行已创建",
            "queued": "任务已进入队列",
            "running": "编排器正在处理",
            "pausing": "正在停止新调用，等待在途结果安全落盘",
            "awaiting_outline_approval": "章纲已生成，等待批准",
            "paused": "运行已暂停，不再派发新调用",
            "completed": "运行已完成",
            "failed": "运行失败，请查看任务错误",
            "cancelled": "运行已取消，部分产物仍然保留",
            "cancelling": "正在取消运行，等待在途调用结束",
            "needs_human": "自动审核未通过，需要人工处理",
            "indeterminate": "调用状态不确定，重试前请确认可能重复计费",
        }
        return messages.get(status, run.stage)


def safe_download_name(artifact: models.Artifact) -> str:
    """Return a human-readable filename without trusting stored metadata as a path."""

    suffix = Path(artifact.relative_path).suffix or ".bin"
    title = _artifact_title(artifact).strip().replace("/", "_").replace("\\", "_")
    title = title[:120] or artifact.id
    return title if title.lower().endswith(suffix.lower()) else f"{title}{suffix}"
