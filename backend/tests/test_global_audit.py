from __future__ import annotations

import asyncio
from uuid import UUID

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from backend.app.database import Base, build_engine, make_session_factory
from backend.app.models import Project, Task
from backend.app.orchestrator import (
    GlobalReviewOutput,
    ReviewGroupDimension,
    ReviewGroupIssue,
    create_runtime,
)
from backend.app.schemas import ChapterBriefV1, ReviewDimension, RunPlanV1
from backend.app.storage import ArtifactStore


def _dimensions(score: float = 94) -> dict[str, ReviewGroupDimension]:
    return {
        dimension.value: ReviewGroupDimension(score=score, summary="全轮审计")
        for dimension in ReviewDimension
    }


def test_global_review_gate_recomputes_pass_and_requires_all_nine_dimensions() -> None:
    result = GlobalReviewOutput(dimension_results=_dimensions(80), passed=True)
    assert result.passed is False
    assert result.weighted_score == 80

    incomplete = _dimensions()
    incomplete.pop(ReviewDimension.DIALOGUE_POV.value)
    with pytest.raises(ValidationError):
        GlobalReviewOutput(dimension_results=incomplete, passed=True)

    blocked = _dimensions()
    blocked[ReviewDimension.CANON_WORLD_RULES.value] = ReviewGroupDimension(
        score=100,
        issues=[
            ReviewGroupIssue(
                issue_id="GLOBAL-CANON",
                severity="blocker",
                dimension=ReviewDimension.CANON_WORLD_RULES.value,
                title="Canon 冲突",
            )
        ],
    )
    result = GlobalReviewOutput(dimension_results=blocked, passed=True)
    assert result.passed is False
    assert result.blocker_count == 1


def test_global_invalidation_rewrites_from_earliest_chapter_and_reaudits(tmp_path) -> None:
    async def scenario() -> None:
        engine = build_engine(f"sqlite:///{(tmp_path / 'global.sqlite3').as_posix()}")
        Base.metadata.create_all(engine)
        factory = make_session_factory(engine)
        store = ArtifactStore(tmp_path / "artifacts")
        with factory.begin() as session:
            project = Project(name="全轮审计", data_path="projects/global")
            session.add(project)
            session.flush()
            project_id = UUID(project.id)
        runtime = create_runtime(factory, store, demo_mode=True, delivery_root=tmp_path / "delivery")
        snapshot = runtime.repository.create_run(
            project_id=project_id,
            kind="continuation",
            branch_name="main",
            target_chars=4_000,
            provider_preset="hybrid-quality",
            user_requirements="",
            budget={
                "max_calls": 500,
                "max_tokens": 2_000_000,
                "max_wall_time_seconds": 60,
                "max_revision_rounds": 3,
                "max_global_repair_rounds": 2,
            },
        )
        plan = RunPlanV1(
            run_id=snapshot.id,
            start_point="旧章之后",
            target_chars=4_000,
            chapter_briefs=[
                ChapterBriefV1(number=1, title="第1章 起点", target_chars=2_000),
                ChapterBriefV1(number=2, title="第2章 回声", target_chars=2_000),
            ],
        )
        original_demo = runtime._demo_value
        global_calls = 0
        writer_rounds: list[int] = []

        def scripted(operation, context):
            nonlocal global_calls
            if operation == "write_chapter":
                writer_rounds.append(int(context.get("production_round", 0) or 0))
            if operation == "global_review":
                global_calls += 1
                return GlobalReviewOutput(
                    dimension_results=_dimensions(),
                    invalidated_chapter_ids=(
                        [plan.chapter_briefs[0].chapter_id] if global_calls == 1 else []
                    ),
                    summary="首次发现状态依赖失效" if global_calls == 1 else "复审通过",
                )
            return original_demo(operation, context)

        runtime._demo_value = scripted
        await runtime._run_continuation(snapshot, plan)

        assert global_calls == 2
        assert writer_rounds == [0, 0, 1, 1]
        with factory() as session:
            global_tasks = session.scalars(
                select(Task).where(
                    Task.run_id == str(snapshot.id),
                    Task.operation == "global_review",
                )
            ).all()
            rerun_tasks = session.scalars(
                select(Task).where(
                    Task.run_id == str(snapshot.id),
                    Task.logical_key.contains("global-r1"),
                )
            ).all()
        assert len(global_tasks) == 2
        assert rerun_tasks

    asyncio.run(scenario())
