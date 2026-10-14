from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from backend.app.api import _store_import
from backend.app.database import Base, build_engine, make_session_factory
from backend.app.importer import ImportKind, inspect_import
from backend.app.models import Artifact, BranchHead, ModelCall, Project, Task
from backend.app.orchestrator import (
    ArbitrationOutput,
    BranchHeadConflict,
    BudgetLimits,
    BudgetManager,
    IndeterminateTaskBlocked,
    PlanHashMismatch,
    PreparedArtifact,
    ReviewGroupDimension,
    ReviewGroupIssue,
    ReviewGroupOutput,
    _scene_append_delta,
    create_runtime,
)
from backend.app.providers import (
    ROUTING_PRESETS,
    AgentRole,
    IndeterminateProviderError,
    LLMResponse,
    LLMStreamEvent,
    ModelUsage,
    ProviderName,
    ProviderRegistry,
    RetryableProviderError,
    StructuredOutputError,
)
from backend.app.quality import build_revision_manifest
from backend.app.schemas import (
    REVIEW_DIMENSIONS,
    ChapterBriefV1,
    ChapterDeltaV1,
    ChapterStateLedgerV1,
    RunPlanV1,
    StateChangeV1,
    StoryFactV1,
    StoryStateV1,
)
from backend.app.storage import ArtifactStore


async def _wait_for_status(runtime, run_id: UUID, statuses: set[str], timeout: float = 8.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        snapshot = runtime.repository.get_run(run_id)
        if snapshot.status in statuses:
            return snapshot
        await asyncio.sleep(0.01)
    snapshot = runtime.repository.get_run(run_id)
    raise AssertionError(f"run did not reach {statuses}; status={snapshot.status}, stage={snapshot.stage}")


def _workspace(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    engine = build_engine(f"sqlite:///{(tmp_path / 'runtime.sqlite3').as_posix()}")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    store = ArtifactStore(tmp_path / "artifacts")
    with factory.begin() as session:
        project = Project(name="无名城", data_path="projects/test")
        session.add(project)
        session.flush()
        project_id = UUID(project.id)
    return factory, store, project_id


def _add_source(factory, store, project_id: UUID, text: str) -> UUID:
    stored = store.put_text(text, suffix=".txt")
    with factory.begin() as session:
        artifact = Artifact(
            project_id=str(project_id),
            kind="source_document",
            version=1,
            relative_path=stored.relative_path,
            sha256=stored.sha256,
            media_type=stored.media_type,
            byte_size=stored.byte_size,
            generation_config_json={"source_name": "source.txt", "sequence_index": 1},
        )
        session.add(artifact)
        session.flush()
        return UUID(artifact.id)


def test_demo_continuation_stops_for_approval_then_delivers_without_sqlite_body(tmp_path) -> None:
    async def scenario() -> None:
        factory, store, project_id = _workspace(tmp_path)
        runtime = create_runtime(
            factory,
            store,
            demo_mode=True,
            delivery_root=tmp_path / "deliveries",
        )
        await runtime.start()
        try:
            run = await runtime.create_continuation_run(
                project_id=project_id,
                target_chars=4_000,
                budget={"max_calls": 300, "max_tokens": 800_000, "max_wall_time_seconds": 60},
            )
            waiting = await _wait_for_status(runtime, run.id, {"awaiting_outline_approval", "failed"})
            assert waiting.status == "awaiting_outline_approval"
            with factory() as session:
                operations = set(
                    session.scalars(select(Task.operation).where(Task.run_id == str(run.id))).all()
                )
            assert "write_chapter" not in operations
            assert waiting.plan_hash
            with pytest.raises(PlanHashMismatch):
                await runtime.approve_outline(run.id, "0" * 64)

            await runtime.approve_outline(run.id, waiting.plan_hash)
            completed = await _wait_for_status(
                runtime, run.id, {"completed", "failed", "needs_human"}, timeout=15
            )
            assert completed.status == "completed", completed.usage.get("last_error")
            with factory() as session:
                artifacts = session.scalars(select(Artifact).where(Artifact.run_id == str(run.id))).all()
                kinds = {item.kind for item in artifacts}
                writer_tasks = session.scalars(
                    select(Task).where(Task.run_id == str(run.id), Task.operation == "write_chapter")
                ).all()
            assert {"chapter_final", "delivery_reading_zip", "delivery_recovery_zip"} <= kinds
            assert writer_tasks
            assert all("灯影沿着长街" not in str(task.context_json) for task in writer_tasks)
            assert all(task.input_artifact_ids_json for task in writer_tasks)
            chapter_artifacts = [item for item in artifacts if item.kind == "write_chapter"]
            assert chapter_artifacts[0].generation_config_json["chapter_id"]
            final = next(item for item in artifacts if item.kind == "chapter_final")
            assert final.generation_config_json["status"] == "passed"
            recovery = next(item for item in artifacts if item.kind == "delivery_recovery_zip")
            assert inspect_import(store.resolve(recovery.relative_path)).kind == ImportKind.PROJECT_BUNDLE
        finally:
            await runtime.stop()

    asyncio.run(scenario())


def test_three_chapters_pass_cumulative_ledger_to_all_chapter_agents(tmp_path) -> None:
    async def scenario() -> None:
        factory, store, project_id = _workspace(tmp_path)
        runtime = create_runtime(
            factory,
            store,
            demo_mode=True,
            delivery_root=tmp_path / "deliveries",
        )
        snapshot = runtime.repository.create_run(
            project_id=project_id,
            kind="continuation",
            branch_name="main",
            target_chars=4_000,
            provider_preset="deepseek-only",
            user_requirements="",
            budget={
                "max_calls": 300,
                "max_tokens": 1_000_000,
                "max_wall_time_seconds": 60,
                "max_revision_rounds": 3,
            },
        )
        plan = RunPlanV1(
            run_id=snapshot.id,
            branch_name="main",
            start_point="旧章之后",
            target_chars=4_000,
            chapter_briefs=[
                ChapterBriefV1(number=1, title="第1章 一", target_chars=1_300),
                ChapterBriefV1(number=2, title="第2章 二", target_chars=1_300),
                ChapterBriefV1(number=3, title="第3章 三", target_chars=1_400),
            ],
        )
        original_demo_value = runtime._demo_value
        captured: dict[str, list[dict[str, object]]] = {
            "write_chapter": [],
            "review_dimension_group": [],
            "revise_chapter": [],
            "arbitrate_chapter": [],
        }

        def scripted_demo_value(operation: str, context):
            if operation in captured:
                captured[operation].append(context)
            if operation == "extract_delta":
                number = int(context["chapter_number"])
                return ChapterDeltaV1(
                    run_id=UUID(str(context["run_id"])),
                    chapter_id=UUID(str(context["chapter_id"])),
                    chapter_number=number,
                    source_artifact_id=UUID(str(context["source_artifact_id"])),
                    state_changes=[
                        StateChangeV1(
                            subject_id="主角",
                            field=f"chapter_{number}_status",
                            before=None,
                            after=f"第{number}章已完成",
                        )
                    ],
                    new_facts=[
                        StoryFactV1(
                            fact_id=f"accepted_fact_{number}",
                            category="continuity",
                            statement=f"第{number}章累计事实",
                        )
                    ],
                    end_state_summary=f"第{number}章终态",
                )
            if operation == "write_chapter":
                brief = ChapterBriefV1.model_validate(context["brief"])
                if brief.number == 3:
                    return f"# {brief.title}\n\n短。"
            if operation == "revise_chapter":
                brief = ChapterBriefV1.model_validate(context["brief"])
                if brief.number == 3 and int(context["revision_number"]) < 2:
                    return str(context["chapter_text"])
                return original_demo_value("write_chapter", {"brief": brief})
            return original_demo_value(operation, context)

        runtime._demo_value = scripted_demo_value
        await runtime._run_continuation(snapshot, plan)

        writer_ledgers = [
            ChapterStateLedgerV1.model_validate(item["continuity_ledger"])
            for item in captured["write_chapter"]
        ]
        assert [len(item.chapter_deltas) for item in writer_ledgers] == [0, 1, 2]
        for context, ledger in zip(captured["write_chapter"], writer_ledgers, strict=True):
            persisted = ChapterStateLedgerV1.model_validate(
                runtime.repository.read_json(UUID(str(context["continuity_ledger_artifact_id"])))
            )
            assert persisted.ledger_id == ledger.ledger_id
            assert persisted.revision == ledger.revision
        assert [fact.fact_id for fact in writer_ledgers[2].working_state.active_facts] == [
            "accepted_fact_1",
            "accepted_fact_2",
        ]
        assert writer_ledgers[2].working_state.subject_fields["主角"] == {
            "chapter_1_status": "第1章已完成",
            "chapter_2_status": "第2章已完成",
        }

        third_review = next(
            item
            for item in captured["review_dimension_group"]
            if ChapterBriefV1.model_validate(item["brief"]).number == 3
        )
        assert len(ChapterStateLedgerV1.model_validate(third_review["continuity_ledger"]).chapter_deltas) == 2
        assert all(
            len(ChapterStateLedgerV1.model_validate(item["continuity_ledger"]).chapter_deltas) == 2
            for item in captured["revise_chapter"]
        )
        assert len(captured["revise_chapter"]) == 2
        assert len(captured["arbitrate_chapter"]) == 1
        arbitration_ledger = ChapterStateLedgerV1.model_validate(
            captured["arbitrate_chapter"][0]["continuity_ledger"]
        )
        assert [fact.fact_id for fact in arbitration_ledger.working_state.active_facts] == [
            "accepted_fact_1",
            "accepted_fact_2",
        ]

        with factory() as session:
            ledger_artifacts = session.scalars(
                select(Artifact)
                .where(
                    Artifact.run_id == str(snapshot.id),
                    Artifact.kind == "chapter_state_ledger",
                )
                .order_by(Artifact.created_at, Artifact.id)
            ).all()
        assert len(ledger_artifacts) == 4
        final_ledger = ChapterStateLedgerV1.model_validate(
            runtime.repository.read_json(UUID(ledger_artifacts[-1].id))
        )
        assert final_ledger.revision == 3
        assert final_ledger.previous_ledger_artifact_id == UUID(ledger_artifacts[-2].id)
        assert len(final_ledger.chapter_deltas) == 3

    asyncio.run(scenario())


def test_formal_chapter_review_reads_source_artifacts_and_blocks_long_copy(tmp_path) -> None:
    async def scenario() -> None:
        factory, store, project_id = _workspace(tmp_path)
        copied = "雨声沿旧站铁轨反复回响，锈蚀的信号灯在雾里明灭。" * 12
        _add_source(factory, store, project_id, f"# 原作\n\n{copied}\n\n旧章结束。")
        runtime = create_runtime(factory, store, demo_mode=True)
        snapshot = runtime.repository.create_run(
            project_id=project_id,
            kind="continuation",
            branch_name="main",
            target_chars=4_000,
            provider_preset="deepseek-only",
            user_requirements="",
            budget={"max_calls": 100, "max_tokens": 500_000, "max_wall_time_seconds": 60},
        )
        brief = ChapterBriefV1(number=1, title="第1章 重影", target_chars=4_000)
        plan = RunPlanV1(
            run_id=snapshot.id,
            start_point="旧章之后",
            target_chars=4_000,
            chapter_briefs=[brief],
        )
        parts = [f"# {brief.title}", copied]
        while sum(1 for char in "\n\n".join(parts) if not char.isspace()) < 4_000:
            parts.append("新人物沿另一条因果线追查失踪的车票，让现场状态继续推进。")
        text = "\n\n".join(parts) + "。"
        draft = runtime.repository.store_text(
            project_id=project_id,
            run_id=snapshot.id,
            task_id=None,
            kind="write_chapter",
            text=text,
            suffix=".md",
        )
        delta = ChapterDeltaV1(
            run_id=snapshot.id,
            chapter_id=brief.chapter_id,
            chapter_number=1,
            source_artifact_id=draft.id,
            end_state_summary="调查继续。",
        )
        ledger = ChapterStateLedgerV1(
            run_id=snapshot.id,
            project_id=project_id,
            branch_name="main",
        )
        ledger_artifact = runtime.repository.store_json(
            project_id=project_id,
            run_id=snapshot.id,
            task_id=None,
            kind="chapter_state_ledger",
            value=ledger,
        )

        _, gate = await runtime._review_chapter(
            snapshot,
            plan,
            brief,
            text,
            draft.id,
            delta,
            story_state={},
            continuity_ledger=ledger,
            continuity_ledger_artifact_id=ledger_artifact.id,
            review_no=1,
        )

        checks = {item.key: item for item in gate.deterministic_checks}
        assert checks["source_overlap"].passed is False
        assert checks["source_overlap"].evidence
        manifest = build_revision_manifest(gate)
        overlap_issue = next(item for item in manifest if item["issue_id"] == "DET-SOURCE-OVERLAP")
        assert overlap_issue["severity"] == "blocker"
        assert overlap_issue["evidence"]
        assert overlap_issue["rule"]
        assert overlap_issue["instruction"]

    asyncio.run(scenario())


def test_recovery_export_import_restores_branch_state_and_can_continue(tmp_path) -> None:
    async def scenario() -> None:
        source_factory, source_store, source_project_id = _workspace(tmp_path / "source")
        _add_source(
            source_factory,
            source_store,
            source_project_id,
            "第一章 旧站\n\n雨声覆盖了月台。",
        )
        source_runtime = create_runtime(
            source_factory,
            source_store,
            demo_mode=True,
            delivery_root=tmp_path / "source-delivery",
        )
        await source_runtime.start()
        try:
            run = await source_runtime.create_continuation_run(
                project_id=source_project_id,
                target_chars=4_000,
                budget={"max_calls": 300, "max_tokens": 800_000, "max_wall_time_seconds": 60},
            )
            waiting = await _wait_for_status(source_runtime, run.id, {"awaiting_outline_approval"})
            await source_runtime.approve_outline(run.id, waiting.plan_hash)
            completed = await _wait_for_status(
                source_runtime, run.id, {"completed", "failed", "needs_human"}, timeout=15
            )
            assert completed.status == "completed"
            with source_factory() as session:
                recovery_artifact = session.scalar(
                    select(Artifact).where(
                        Artifact.run_id == str(run.id),
                        Artifact.kind == "delivery_recovery_zip",
                    )
                )
                assert recovery_artifact is not None
                recovery_path = source_store.resolve(recovery_artifact.relative_path)

            restored_root = tmp_path / "restored"
            restored_factory, restored_store, unused_project_id = _workspace(restored_root)
            with restored_factory.begin() as session:
                unused = session.get(Project, str(unused_project_id))
                session.delete(unused)
            inspection = inspect_import(recovery_path)
            request = SimpleNamespace(
                app=SimpleNamespace(
                    state=SimpleNamespace(
                        session_factory=restored_factory,
                        views=None,
                        artifact_store=restored_store,
                        settings=SimpleNamespace(project_dir=restored_root / "projects"),
                    )
                )
            )
            restored_project_id = UUID(
                _store_import(
                    request,
                    inspection,
                    project_name=inspection.title,
                    original_path=recovery_path,
                    original_name="recovery.zip",
                )
            )
            with restored_factory() as session:
                restored_project = session.get(Project, str(restored_project_id))
                restored_head = session.scalar(
                    select(BranchHead).where(
                        BranchHead.project_id == str(restored_project_id),
                        BranchHead.branch_name == restored_project.active_branch,
                    )
                )
                restored_sources = session.scalars(
                    select(Artifact).where(
                        Artifact.project_id == str(restored_project_id),
                        Artifact.kind == "source_document",
                    )
                ).all()
            assert restored_project.current_story_state_artifact_id == restored_head.artifact_id
            assert restored_sources

            restored_runtime = create_runtime(restored_factory, restored_store, demo_mode=True)
            await restored_runtime.start()
            try:
                next_run = await restored_runtime.create_continuation_run(
                    project_id=restored_project_id,
                    target_chars=4_000,
                    branch_name=restored_project.active_branch,
                    budget={"max_calls": 100, "max_tokens": 300_000, "max_wall_time_seconds": 60},
                )
                next_waiting = await _wait_for_status(
                    restored_runtime, next_run.id, {"awaiting_outline_approval", "failed"}
                )
                assert next_waiting.status == "awaiting_outline_approval"
                assert next_waiting.usage["base_story_state_artifact_id"] == restored_head.artifact_id
            finally:
                await restored_runtime.stop()
        finally:
            await source_runtime.stop()

    asyncio.run(scenario())


def test_demo_analysis_promotes_story_state_and_eighteen_views(tmp_path) -> None:
    async def scenario() -> None:
        factory, store, project_id = _workspace(tmp_path)
        _add_source(factory, store, project_id, "第一章 雨夜\n\n车站的灯在雨里亮起。")
        runtime = create_runtime(factory, store, demo_mode=True)
        seed_run = runtime.repository.create_run(
            project_id=project_id,
            kind="continuation",
            branch_name="main",
            target_chars=4_000,
            provider_preset="hybrid-quality",
            user_requirements="",
            budget={"max_calls": 10},
        )
        seed_state = StoryStateV1(
            project_id=project_id,
            facts=[StoryFactV1(category="continuation", statement="已批准续写状态必须保留")],
        )
        seed_artifact = runtime.repository.store_json(
            project_id=project_id,
            run_id=seed_run.id,
            task_id=None,
            kind="story_state",
            value=seed_state,
        )
        assert runtime.repository.promote_story_state(
            project_id,
            branch_name="main",
            expected_artifact_id=None,
            artifact_id=seed_artifact.id,
            run_id=seed_run.id,
        )
        await runtime.start()
        try:
            run = await runtime.create_analysis_run(
                project_id=project_id,
                budget={"max_calls": 100, "max_tokens": 500_000, "max_wall_time_seconds": 60},
            )
            completed = await _wait_for_status(
                runtime, run.id, {"completed", "failed", "needs_human"}, timeout=10
            )
            assert completed.status == "completed", completed.usage.get("last_error")
            with factory() as session:
                artifacts = session.scalars(select(Artifact).where(Artifact.run_id == str(run.id))).all()
                head = session.scalar(
                    select(BranchHead).where(
                        BranchHead.project_id == str(project_id), BranchHead.branch_name == "main"
                    )
                )
            views = [item for item in artifacts if item.kind.startswith("summary_")]
            assert len(views) == 18
            assert head is not None
            assert head.artifact_id == completed.usage["story_state_artifact_id"]
            final_state = StoryStateV1.model_validate(runtime.repository.read_json(UUID(head.artifact_id)))
            assert any(fact.statement == "已批准续写状态必须保留" for fact in final_state.facts)
            assert {item.generation_config_json["variant"] for item in views} == {
                "full",
                "compact",
            }
        finally:
            await runtime.stop()

    asyncio.run(scenario())


def test_budget_recovery_converts_stale_reservations_and_merge_keeps_metadata(tmp_path) -> None:
    async def scenario() -> None:
        persisted: list[dict[str, object]] = []
        manager = BudgetManager(
            BudgetLimits(10.0, 10_000, 10, 60),
            {
                "used_calls": 1,
                "reserved_calls": 2,
                "reserved_tokens": 300,
                "reserved_cost_usd": 0.4,
            },
            persist=lambda value: persisted.append(value),
        )
        assert manager.used_calls == 3
        assert manager.used_tokens == 300
        assert manager.reserved_calls == 0
        reservation = await manager.reserve(calls=2, tokens=100, cost_usd=0.1)
        await manager.commit(reservation, calls=1, tokens=40, cost_usd=0.02)
        assert manager.used_calls == 4
        assert persisted[-1]["reserved_calls"] == 0

        factory, store, project_id = _workspace(tmp_path)
        runtime = create_runtime(factory, store, demo_mode=True)
        run = runtime.repository.create_run(
            project_id=project_id,
            kind="analysis",
            branch_name="main",
            target_chars=None,
            provider_preset="hybrid-quality",
            user_requirements="",
            budget={"max_calls": 10},
        )
        runtime.repository.update_usage(run.id, {"base_story_state_artifact_id": "base"})
        runtime.repository.update_usage(run.id, {"used_calls": 2})
        usage = runtime.repository.get_run(run.id).usage
        assert usage["base_story_state_artifact_id"] == "base"
        assert usage["used_calls"] == 2

    asyncio.run(scenario())


def test_branch_heads_are_independent_and_same_base_has_single_cas_winner(tmp_path) -> None:
    factory, store, project_id = _workspace(tmp_path)
    runtime = create_runtime(factory, store, demo_mode=True)
    assert runtime.repository.ensure_branch_head(project_id, "main") is None
    assert runtime.repository.ensure_branch_head(project_id, "alternate") is None
    run_main = runtime.repository.create_run(
        project_id=project_id,
        kind="analysis",
        branch_name="main",
        target_chars=None,
        provider_preset="hybrid-quality",
        user_requirements="",
        budget={"max_calls": 10},
    )
    run_loser = runtime.repository.create_run(
        project_id=project_id,
        kind="analysis",
        branch_name="main",
        target_chars=None,
        provider_preset="hybrid-quality",
        user_requirements="",
        budget={"max_calls": 10},
    )
    run_alternate = runtime.repository.create_run(
        project_id=project_id,
        kind="analysis",
        branch_name="alternate",
        target_chars=None,
        provider_preset="hybrid-quality",
        user_requirements="",
        budget={"max_calls": 10},
    )
    main_artifact = runtime.repository.store_json(
        project_id=project_id,
        run_id=run_main.id,
        task_id=None,
        kind="story_state",
        value={"branch": "main"},
    )
    loser_artifact = runtime.repository.store_json(
        project_id=project_id,
        run_id=run_loser.id,
        task_id=None,
        kind="story_state",
        value={"branch": "loser"},
    )
    alternate_artifact = runtime.repository.store_json(
        project_id=project_id,
        run_id=run_alternate.id,
        task_id=None,
        kind="story_state",
        value={"branch": "alternate"},
    )
    assert runtime.repository.promote_story_state(
        project_id,
        branch_name="main",
        expected_artifact_id=None,
        artifact_id=main_artifact.id,
        run_id=run_main.id,
    )
    assert not runtime.repository.promote_story_state(
        project_id,
        branch_name="main",
        expected_artifact_id=None,
        artifact_id=loser_artifact.id,
        run_id=run_loser.id,
    )
    assert runtime.repository.promote_story_state(
        project_id,
        branch_name="alternate",
        expected_artifact_id=None,
        artifact_id=alternate_artifact.id,
        run_id=run_alternate.id,
    )
    assert runtime.repository.get_branch_head(project_id, "main") == main_artifact.id
    assert runtime.repository.get_branch_head(project_id, "alternate") == alternate_artifact.id


def test_branch_finalization_lease_blocks_other_runs_and_expired_owner_is_fenced(tmp_path) -> None:
    factory, store, project_id = _workspace(tmp_path)
    runtime = create_runtime(factory, store, demo_mode=True)
    first = runtime.repository.create_run(
        project_id=project_id,
        kind="continuation",
        branch_name="main",
        target_chars=4_000,
        provider_preset="hybrid-quality",
        user_requirements="",
        budget={"max_calls": 10},
    )
    second = runtime.repository.create_run(
        project_id=project_id,
        kind="analysis",
        branch_name="main",
        target_chars=None,
        provider_preset="hybrid-quality",
        user_requirements="",
        budget={"max_calls": 10},
    )
    first_token = runtime.repository.acquire_branch_finalization_lock(
        project_id,
        branch_name="main",
        expected_artifact_id=None,
        run_id=first.id,
        lease_seconds=60,
    )
    assert first_token is not None
    assert (
        runtime.repository.acquire_branch_finalization_lock(
            project_id,
            branch_name="main",
            expected_artifact_id=None,
            run_id=first.id,
            lease_seconds=60,
        )
        is None
    )
    assert runtime.repository.renew_branch_finalization_lock(
        project_id,
        branch_name="main",
        run_id=first.id,
        lock_token=first_token,
        lease_seconds=60,
    )
    assert (
        runtime.repository.acquire_branch_finalization_lock(
            project_id,
            branch_name="main",
            expected_artifact_id=None,
            run_id=second.id,
            lease_seconds=60,
        )
        is None
    )
    analysis_artifact = runtime.repository.store_json(
        project_id=project_id,
        run_id=second.id,
        task_id=None,
        kind="story_state",
        value={"owner": "analysis"},
    )
    assert not runtime.repository.promote_story_state(
        project_id,
        branch_name="main",
        expected_artifact_id=None,
        artifact_id=analysis_artifact.id,
        run_id=second.id,
    )

    with factory.begin() as session:
        head = session.scalar(
            select(BranchHead).where(
                BranchHead.project_id == str(project_id),
                BranchHead.branch_name == "main",
            )
        )
        assert head is not None
        head.finalization_lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)

    second_token = runtime.repository.acquire_branch_finalization_lock(
        project_id,
        branch_name="main",
        expected_artifact_id=None,
        run_id=second.id,
        lease_seconds=60,
    )
    assert second_token is not None and second_token != first_token
    assert not runtime.repository.release_branch_finalization_lock(
        project_id,
        branch_name="main",
        run_id=first.id,
        lock_token=first_token,
    )
    assert runtime.repository.promote_story_state(
        project_id,
        branch_name="main",
        expected_artifact_id=None,
        artifact_id=analysis_artifact.id,
        run_id=second.id,
        finalization_lock_token=second_token,
    )
    unpublished_memory = runtime.repository.store_json(
        project_id=project_id,
        run_id=first.id,
        task_id=None,
        kind="branch_memory_snapshot",
        value={"owner": "expired-first-run"},
    )
    unpublished_id = uuid4()
    unpublished_object = store.put_text("must remain unindexed", suffix=".txt")
    assert not runtime.repository.promote_story_state(
        project_id,
        branch_name="main",
        expected_artifact_id=None,
        artifact_id=unpublished_memory.id,
        run_id=first.id,
        finalization_lock_token=first_token,
        publish_artifacts=[
            PreparedArtifact(
                id=unpublished_id,
                kind="delivery_plain_txt",
                stored=unpublished_object,
            )
        ],
    )
    with factory() as session:
        head = session.scalar(
            select(BranchHead).where(
                BranchHead.project_id == str(project_id),
                BranchHead.branch_name == "main",
            )
        )
        unpublished = session.get(Artifact, str(unpublished_id))
    assert head is not None
    assert head.finalization_owner_run_id is None
    assert head.finalization_lock_token is None
    assert head.finalization_lease_expires_at is None
    assert unpublished is None


def test_competing_continuations_publish_delivery_for_only_one_cas_winner(tmp_path) -> None:
    async def scenario() -> None:
        factory, store, project_id = _workspace(tmp_path)
        runtime = create_runtime(
            factory,
            store,
            demo_mode=True,
            delivery_root=tmp_path / "deliveries",
        )
        runs = [
            runtime.repository.create_run(
                project_id=project_id,
                kind="continuation",
                branch_name="main",
                target_chars=4_000,
                provider_preset="deepseek-only",
                user_requirements="",
                budget={"max_calls": 200, "max_tokens": 1_000_000, "max_wall_time_seconds": 60},
            )
            for _ in range(2)
        ]
        plans = [
            RunPlanV1(
                run_id=run.id,
                branch_name="main",
                start_point="共同基础之后",
                target_chars=4_000,
                chapter_briefs=[ChapterBriefV1(number=1, title=f"第1章 竞争{index}", target_chars=4_000)],
            )
            for index, run in enumerate(runs, 1)
        ]

        outcomes = await asyncio.gather(
            *(runtime._run_continuation(run, plan) for run, plan in zip(runs, plans, strict=True)),
            return_exceptions=True,
        )
        assert sum(item is None for item in outcomes) == 1
        assert sum(isinstance(item, BranchHeadConflict) for item in outcomes) == 1
        winner = next(run for run in runs if runtime.repository.get_run(run.id).status == "completed")
        loser = next(run for run in runs if run.id != winner.id)
        with factory() as session:
            formal = session.scalars(select(Artifact).where(Artifact.kind.like("delivery_%"))).all()
            manifests = [item for item in formal if item.kind == "delivery_manifest"]
            head = session.scalar(
                select(BranchHead).where(
                    BranchHead.project_id == str(project_id),
                    BranchHead.branch_name == "main",
                )
            )
        assert formal
        assert {item.run_id for item in formal} == {str(winner.id)}
        assert manifests
        assert all(item.run_id != str(loser.id) for item in formal)
        assert head is not None and head.finalization_owner_run_id is None

    asyncio.run(scenario())


def test_worker_releases_finalization_lock_when_delivery_build_raises(tmp_path, monkeypatch) -> None:
    async def scenario() -> None:
        factory, store, project_id = _workspace(tmp_path)

        def fail_delivery(*_args, **_kwargs):
            raise RuntimeError("injected delivery failure")

        monkeypatch.setattr("backend.app.orchestrator.build_delivery_bundle", fail_delivery)
        runtime = create_runtime(
            factory,
            store,
            demo_mode=True,
            delivery_root=tmp_path / "deliveries",
        )
        await runtime.start()
        try:
            run = await runtime.create_continuation_run(
                project_id=project_id,
                target_chars=4_000,
                budget={"max_calls": 300, "max_tokens": 1_000_000, "max_wall_time_seconds": 60},
            )
            waiting = await _wait_for_status(runtime, run.id, {"awaiting_outline_approval"})
            await runtime.approve_outline(run.id, waiting.plan_hash)
            failed = await _wait_for_status(runtime, run.id, {"failed", "needs_human"}, timeout=15)
            assert failed.status == "failed"

            deadline = asyncio.get_running_loop().time() + 3
            while True:
                with factory() as session:
                    head = session.scalar(
                        select(BranchHead).where(
                            BranchHead.project_id == str(project_id),
                            BranchHead.branch_name == "main",
                        )
                    )
                    formal_count = len(
                        session.scalars(
                            select(Artifact).where(
                                Artifact.run_id == str(run.id),
                                Artifact.kind.like("delivery_%"),
                            )
                        ).all()
                    )
                if head is not None and head.finalization_owner_run_id is None:
                    break
                if asyncio.get_running_loop().time() >= deadline:
                    raise AssertionError("worker did not release branch finalization lease")
                await asyncio.sleep(0.01)
            assert formal_count == 0
            assert run.id not in runtime._finalization_lock_tokens
        finally:
            await runtime.stop()

    asyncio.run(scenario())


class _FailingReviewScript:
    def __init__(self) -> None:
        self.review_cycle = 0
        self.revisions = 0
        self.arbitrations = 0
        self.consolidations = 0

    def chapter_text(self, brief: dict[str, object]) -> str:
        parts = [f"# {brief['title']}", "", *(brief.get("required_beats") or [])]
        while sum(1 for char in "\n".join(parts) if not char.isspace()) < int(brief["target_chars"]):
            parts.append("夜雨压住街声，人物沿着既定线索前行，并把现场变化落实为新的因果。")
        return "\n\n".join(str(item) for item in parts) + "。"

    def value(self, operation: str, context: dict[str, object]):
        if operation in {"build_plan", "revise_plan"}:
            target = int(context["target_chars"])
            return RunPlanV1(
                run_id=UUID(str(context["run_id"])),
                branch_name=str(context["branch_name"]),
                start_point="旧章之后",
                target_chars=target,
                chapter_briefs=[
                    ChapterBriefV1(
                        number=1,
                        title="第1章 雨线",
                        target_chars=target,
                        required_beats=["关键节点"],
                    )
                ],
            )
        if operation == "write_chapter":
            return self.chapter_text(context["brief"])
        if operation == "revise_chapter":
            self.revisions += 1
            return str(context["chapter_text"])
        if operation == "extract_delta":
            return ChapterDeltaV1(
                run_id=UUID(str(context["run_id"])),
                chapter_id=UUID(str(context["chapter_id"])),
                chapter_number=int(context["chapter_number"]),
                source_artifact_id=UUID(str(context["source_artifact_id"])),
                end_state_summary="雨夜调查暂告一段落。",
            )
        if operation == "review_dimension_group":
            dimensions = {}
            for key in context["dimension_keys"]:
                issues = []
                score = 94
                if key == "plot_alignment":
                    score = 80
                    issues = [
                        ReviewGroupIssue(
                            issue_id="STABLE",
                            severity="major",
                            dimension=key,
                            rule="主线必须推进",
                            evidence=f"round-evidence-{self.review_cycle}",
                            location="中段",
                            revision_instruction="补足主线推进证据",
                        )
                    ]
                dimensions[key] = ReviewGroupDimension(score=score, summary="脚本审校", issues=issues)
            return ReviewGroupOutput(dimension_results=dimensions, summary="组审完成")
        if operation == "consolidate_reviews":
            dimensions = {}
            for dimension in REVIEW_DIMENSIONS:
                issues = []
                score = 94
                if dimension.value == "plot_alignment":
                    score = 80
                    issues = [
                        ReviewGroupIssue(
                            issue_id="STABLE",
                            severity="major",
                            dimension=dimension.value,
                            rule="主线必须推进",
                            evidence=f"round-evidence-{self.review_cycle}",
                            location="中段",
                            revision_instruction="补足主线推进证据",
                        )
                    ]
                dimensions[dimension.value] = ReviewGroupDimension(
                    score=score, summary="主 B 汇总", issues=issues
                )
            self.review_cycle += 1
            self.consolidations += 1
            return ReviewGroupOutput(dimension_results=dimensions, summary="仍有主线问题")
        if operation == "arbitrate_chapter":
            self.arbitrations += 1
            return ArbitrationOutput(
                decision="revise",
                revision_manifest=list(context["current_revision_manifest"]),
            )
        raise AssertionError(f"unexpected operation: {operation}")


class _ScriptedProvider:
    def __init__(self, name: ProviderName, script: _FailingReviewScript) -> None:
        self.name = name
        self.script = script

    async def complete(self, model: str, request) -> LLMResponse:
        payload = json.loads(request.user_prompt)["input"]
        value = self.script.value(request.metadata["operation"], payload)
        text = (
            json.dumps(value.model_dump(mode="json"), ensure_ascii=False)
            if hasattr(value, "model_dump")
            else str(value)
        )
        return LLMResponse(
            provider=self.name,
            model=model,
            request_id=f"script-{self.script.review_cycle}-{request.metadata['operation']}",
            text=text,
            parsed=value if request.schema is not None else None,
            finish_status="completed",
            usage=ModelUsage(input_tokens=10, output_tokens=10, total_tokens=20),
        )

    async def stream_text(self, model: str, request):
        response = await self.complete(model, request)
        yield response.text


def test_three_revision_rounds_then_arbitration_and_full_recheck_needs_human(tmp_path) -> None:
    async def scenario() -> None:
        factory, store, project_id = _workspace(tmp_path)
        script = _FailingReviewScript()
        registry = ProviderRegistry(
            providers={
                ProviderName.OPENAI: _ScriptedProvider(ProviderName.OPENAI, script),
                ProviderName.DEEPSEEK: _ScriptedProvider(ProviderName.DEEPSEEK, script),
            }
        )
        runtime = create_runtime(
            factory,
            store,
            provider_registry=registry,
            demo_mode=False,
            delivery_root=tmp_path / "deliveries",
        )
        await runtime.start()
        try:
            run = await runtime.create_continuation_run(
                project_id=project_id,
                target_chars=4_000,
                budget={
                    "max_calls": 500,
                    "max_tokens": 5_000_000,
                    "max_wall_time_seconds": 60,
                    "max_revision_rounds": 3,
                },
            )
            waiting = await _wait_for_status(runtime, run.id, {"awaiting_outline_approval"})
            await runtime.approve_outline(run.id, waiting.plan_hash)
            terminal = await _wait_for_status(
                runtime, run.id, {"completed", "failed", "needs_human"}, timeout=20
            )
            assert terminal.status == "needs_human", terminal.usage.get("last_error")
            assert terminal.stage == "needs_human"
            assert script.revisions == 4  # three automatic plus one arbitration revision
            assert script.arbitrations == 1
            assert script.consolidations == 5  # arbitration is followed by a full nine-dimension review
            with factory() as session:
                delivery_count = len(
                    session.scalars(
                        select(Artifact).where(
                            Artifact.run_id == str(run.id), Artifact.kind.like("delivery_%")
                        )
                    ).all()
                )
            assert delivery_count == 0
        finally:
            await runtime.stop()

    asyncio.run(scenario())


class _NativeTextProvider:
    def __init__(self, name: ProviderName, chunks: list[str]) -> None:
        self.name = name
        self.chunks = chunks
        self.calls = 0

    async def complete(self, model: str, request) -> LLMResponse:
        self.calls += 1
        text = "".join(self.chunks)
        return LLMResponse(
            provider=self.name,
            model=model,
            request_id=f"complete-{self.calls}",
            text=text,
            finish_status="stop",
            usage=ModelUsage(input_tokens=3, output_tokens=4, total_tokens=7),
        )

    async def stream_events(self, model: str, request):
        self.calls += 1
        for chunk in self.chunks:
            yield LLMStreamEvent(delta=chunk)
        yield LLMStreamEvent(
            response=LLMResponse(
                provider=self.name,
                model=model,
                request_id=f"stream-{self.calls}",
                text="".join(self.chunks),
                finish_status="stop",
                usage=ModelUsage(input_tokens=11, output_tokens=13, total_tokens=24),
                elapsed_seconds=0.125,
            )
        )

    async def stream_text(self, model: str, request):
        async for event in self.stream_events(model, request):
            if event.delta:
                yield event.delta


def _direct_run(runtime, project_id: UUID, *, preset: str = "deepseek-only"):
    return runtime.repository.create_run(
        project_id=project_id,
        kind="continuation",
        branch_name="main",
        target_chars=4_000,
        provider_preset=preset,
        user_requirements="",
        budget={
            "max_calls": 100,
            "max_tokens": 5_000_000,
            "max_wall_time_seconds": 60,
            "max_task_attempts": 3,
        },
    )


def test_streamed_task_persists_terminal_telemetry_and_emits_safe_deltas(tmp_path) -> None:
    async def scenario() -> None:
        factory, store, project_id = _workspace(tmp_path)
        provider = _NativeTextProvider(ProviderName.DEEPSEEK, ["雨\x00落", "长街。"])
        registry = ProviderRegistry(
            providers={
                ProviderName.OPENAI: provider,
                ProviderName.DEEPSEEK: provider,
            },
            preset="deepseek-only",
        )
        runtime = create_runtime(factory, store, provider_registry=registry, demo_mode=False)
        snapshot = _direct_run(runtime, project_id)
        brief = ChapterBriefV1(number=1, title="雨线", target_chars=4_000)
        context = {"brief": brief}
        request = runtime.prompts.build_request(
            operation="write_chapter",
            context=context,
            max_output_tokens=4_096,
            idempotency_key="stream-telemetry",
        )
        messages: list[dict[str, object]] = []

        async def collect() -> None:
            async for message in runtime.events.subscribe(snapshot.id):
                messages.append(message)
                if message["event"] == "task_completed":
                    return

        collector = asyncio.create_task(collect())
        await asyncio.sleep(0)
        result = await runtime._execute_task(
            snapshot,
            logical_key="chapter:streamed",
            operation="write_chapter",
            request=request,
            context=context,
            sequence_index=1,
            text_suffix=".md",
            stream_text=True,
            on_text_delta=runtime._chapter_text_emitter(
                snapshot,
                brief,
                stream_id="draft-stream",
                base_offset=0,
                scene_index=1,
                scene_count=1,
                reset=True,
            ),
        )
        await collector

        deltas = [message["payload"] for message in messages if message["event"] == "chapter_text_delta"]
        assert "".join(str(item["delta"]) for item in deltas) == "雨落长街。"
        assert deltas[0]["reset"] is True
        assert result.value == "雨\x00落长街。"
        with factory() as session:
            call = session.scalar(select(ModelCall).where(ModelCall.task_id == str(result.task_id)))
        assert call is not None
        assert call.provider == "deepseek"
        assert call.provider_request_id == "stream-1"
        assert call.finish_status == "stop"
        assert call.input_tokens == 11
        assert call.output_tokens == 13
        assert call.duration_ms == 125

    asyncio.run(scenario())


class _InterruptedTextProvider(_NativeTextProvider):
    async def stream_events(self, model: str, request):
        self.calls += 1
        yield LLMStreamEvent(delta="已经展示的正文")
        raise IndeterminateProviderError("socket closed after delta")


def test_stream_interruption_marks_task_indeterminate(tmp_path) -> None:
    async def scenario() -> None:
        factory, store, project_id = _workspace(tmp_path)
        provider = _InterruptedTextProvider(ProviderName.DEEPSEEK, [])
        registry = ProviderRegistry(
            providers={ProviderName.DEEPSEEK: provider, ProviderName.OPENAI: provider},
            preset="deepseek-only",
        )
        runtime = create_runtime(factory, store, provider_registry=registry, demo_mode=False)
        snapshot = _direct_run(runtime, project_id)
        context = {"brief": ChapterBriefV1(number=1, title="断流", target_chars=4_000)}
        request = runtime.prompts.build_request(
            operation="write_chapter",
            context=context,
            max_output_tokens=4_096,
        )
        with pytest.raises(IndeterminateTaskBlocked):
            await runtime._execute_task(
                snapshot,
                logical_key="chapter:interrupted",
                operation="write_chapter",
                request=request,
                context=context,
                sequence_index=1,
                stream_text=True,
                on_text_delta=lambda _delta: asyncio.sleep(0),
            )
        with factory() as session:
            task = session.scalar(
                select(Task).where(Task.run_id == str(snapshot.id), Task.logical_key == "chapter:interrupted")
            )
        assert task is not None
        assert task.status == "indeterminate"
        assert task.attempt_count == 1
        assert provider.calls == 1

    asyncio.run(scenario())


class _ExhaustedTransientProvider(_NativeTextProvider):
    async def complete(self, model: str, request) -> LLMResponse:
        self.calls += 1
        raise RetryableProviderError("provider already exhausted HTTP retry envelope")


def test_provider_transient_failure_is_not_retried_again_by_task_loop(tmp_path) -> None:
    async def scenario() -> None:
        factory, store, project_id = _workspace(tmp_path)
        provider = _ExhaustedTransientProvider(ProviderName.DEEPSEEK, [])
        registry = ProviderRegistry(
            providers={ProviderName.DEEPSEEK: provider, ProviderName.OPENAI: provider},
            preset="deepseek-only",
        )
        runtime = create_runtime(factory, store, provider_registry=registry, demo_mode=False)
        snapshot = _direct_run(runtime, project_id)
        context = {"value": "x"}
        request = runtime.prompts.build_request(operation="write_chapter", context=context)
        with pytest.raises(RetryableProviderError):
            await runtime._execute_task(
                snapshot,
                logical_key="transient-once",
                operation="write_chapter",
                request=request,
                context=context,
                sequence_index=1,
            )
        with factory() as session:
            task = session.scalar(
                select(Task).where(Task.run_id == str(snapshot.id), Task.logical_key == "transient-once")
            )
        assert task is not None
        assert task.attempt_count == 1
        assert task.status == "failed"
        assert provider.calls == 1

    asyncio.run(scenario())


class _InvalidStructuredProvider(_NativeTextProvider):
    async def complete(self, model: str, request) -> LLMResponse:
        self.calls += 1
        raise StructuredOutputError("schema drift")


class _ValidStructuredProvider(_NativeTextProvider):
    def __init__(self, value: StoryStateV1) -> None:
        super().__init__(ProviderName.OPENAI, [])
        self.value = value
        self.models: list[str] = []

    async def complete(self, model: str, request) -> LLMResponse:
        self.calls += 1
        self.models.append(model)
        return LLMResponse(
            provider=self.name,
            model=model,
            request_id="openai-fallback-1",
            text=json.dumps(self.value.model_dump(mode="json"), ensure_ascii=False),
            parsed=self.value,
            finish_status="completed",
            usage=ModelUsage(input_tokens=20, output_tokens=30, total_tokens=50),
        )


def test_hybrid_structured_schema_failure_escalates_once_to_openai(tmp_path) -> None:
    async def scenario() -> None:
        factory, store, project_id = _workspace(tmp_path)
        deepseek = _InvalidStructuredProvider(ProviderName.DEEPSEEK, [])
        expected = StoryStateV1(project_id=project_id, branch_name="main")
        openai = _ValidStructuredProvider(expected)
        registry = ProviderRegistry(
            providers={ProviderName.DEEPSEEK: deepseek, ProviderName.OPENAI: openai},
            preset="hybrid-quality",
        )
        runtime = create_runtime(factory, store, provider_registry=registry, demo_mode=False)
        snapshot = _direct_run(runtime, project_id, preset="hybrid-quality")
        context = {"candidate": "state"}
        request = runtime.prompts.build_request(
            operation="merge_story_state",
            context=context,
            schema=StoryStateV1,
            max_output_tokens=2_000,
        )
        result = await runtime._execute_task(
            snapshot,
            logical_key="structured-upgrade",
            operation="merge_story_state",
            request=request,
            context=context,
            sequence_index=1,
        )
        assert StoryStateV1.model_validate(result.value).state_id == expected.state_id
        assert deepseek.calls == 3
        assert openai.calls == 1
        assert openai.models == [ROUTING_PRESETS["openai-only"][AgentRole.ANALYZER].model]
        with factory() as session:
            task = session.get(Task, str(result.task_id))
            call = session.scalar(select(ModelCall).where(ModelCall.task_id == str(result.task_id)))
        assert task is not None and task.attempt_count == 3
        assert call is not None
        assert call.provider == "openai"
        assert call.model == openai.models[0]
        assert call.provider_request_id == "openai-fallback-1"

    asyncio.run(scenario())


class _SceneContinuationProvider(_NativeTextProvider):
    def __init__(self) -> None:
        super().__init__(ProviderName.DEEPSEEK, [])

    async def stream_events(self, model: str, request):
        self.calls += 1
        payload = json.loads(request.user_prompt)["input"]
        contract = payload["scene_contract"]
        index = int(contract["sequence_index"])
        previous = str(payload.get("last_complete_text") or "")
        prefix = f"# {payload['chapter_brief']['title']}\n\n" if index == 1 else previous
        new_text = f"【场景{index}】" + (f"第{index}场的因果继续向前推进。" * 30)
        text = prefix + new_text
        midpoint = max(1, len(text) // 2)
        yield LLMStreamEvent(delta=text[:midpoint])
        yield LLMStreamEvent(delta=text[midpoint:])
        yield LLMStreamEvent(
            response=LLMResponse(
                provider=self.name,
                model=model,
                request_id=f"scene-{index}",
                text=text,
                finish_status="stop",
                usage=ModelUsage(input_tokens=10, output_tokens=20, total_tokens=30),
            )
        )


def test_oversized_chapter_continues_by_scene_and_removes_boundary_overlap(tmp_path, monkeypatch) -> None:
    async def scenario() -> None:
        monkeypatch.setenv("NOVEL_DEEPSEEK_WRITER_MAX_OUTPUT_TOKENS", "1000")
        factory, store, project_id = _workspace(tmp_path)
        provider = _SceneContinuationProvider()
        registry = ProviderRegistry(
            providers={ProviderName.DEEPSEEK: provider, ProviderName.OPENAI: provider},
            preset="deepseek-only",
        )
        runtime = create_runtime(factory, store, provider_registry=registry, demo_mode=False)
        snapshot = _direct_run(runtime, project_id)
        brief = ChapterBriefV1(
            number=1,
            title="四场雨",
            target_chars=4_000,
            required_beats=["入场", "追索", "转折", "钩子"],
        )
        context = {
            "run_id": str(snapshot.id),
            "brief": brief,
            "canon": {},
            "style_card": {},
            "mystery_plan": [],
            "previous_delta": None,
            "previous_tail": "",
            "user_requirements": "",
        }
        messages: list[dict[str, object]] = []

        async def collect() -> None:
            completed = 0
            async for message in runtime.events.subscribe(snapshot.id):
                messages.append(message)
                if message["event"] == "chapter_scene_completed":
                    completed += 1
                    if completed == 4:
                        return

        collector = asyncio.create_task(collect())
        await asyncio.sleep(0)
        result = await runtime._write_chapter_draft(snapshot, brief, context)
        await collector
        text = str(result.value)
        assert provider.calls == 4
        assert all(text.count(f"【场景{index}】") == 1 for index in range(1, 5))
        assert text.count("# 四场雨") == 1
        delta_payloads = [
            message["payload"] for message in messages if message["event"] == "chapter_text_delta"
        ]
        assert "".join(str(item["delta"]) for item in delta_payloads) == text
        assert sum(bool(item["reset"]) for item in delta_payloads) == 1
        scene_payloads = [
            message["payload"] for message in messages if message["event"] == "chapter_scene_completed"
        ]
        assert [int(item["scene_index"]) for item in scene_payloads] == [1, 2, 3, 4]
        assert all(int(item["overlap_removed"]) > 0 for item in scene_payloads[1:])
        with factory() as session:
            tasks = session.scalars(
                select(Task).where(Task.run_id == str(snapshot.id), Task.operation == "write_chapter")
            ).all()
            calls = session.scalars(select(ModelCall).where(ModelCall.run_id == str(snapshot.id))).all()
        assert len(tasks) == 4
        assert len(calls) == 4

    asyncio.run(scenario())


def test_scene_overlap_helper_removes_only_exact_boundary_text() -> None:
    existing = "旧段。" + "雨声沿着长街反复回响。" * 4
    repeated = "雨声沿着长街反复回响。" * 4
    appended, overlap = _scene_append_delta(existing, repeated + "新段开始。")
    assert overlap == len(repeated)
    assert appended == "新段开始。"
    unrelated, unrelated_overlap = _scene_append_delta(existing, "另一处钟声响起。")
    assert unrelated_overlap == 0
    assert unrelated == "\n\n另一处钟声响起。"
