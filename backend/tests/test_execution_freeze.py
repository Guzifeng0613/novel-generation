from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import UUID

from sqlalchemy import select

from backend.app.database import Base, build_engine, make_session_factory
from backend.app.models import Artifact, Project
from backend.app.orchestrator import _immutable_canon_violations, create_runtime
from backend.app.prompting import PROMPT_FILES, PromptStore
from backend.app.providers import ROUTING_PRESETS, AgentRole, ModelRoute, ProviderName
from backend.app.schemas import CanonConflictV1, RunPlanV1, StoryFactV1, StoryStateV1
from backend.app.storage import ArtifactStore


def _workspace(tmp_path):
    engine = build_engine(f"sqlite:///{(tmp_path / 'freeze.sqlite3').as_posix()}")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    store = ArtifactStore(tmp_path / "artifacts")
    with factory.begin() as session:
        project = Project(name="冻结测试", data_path="projects/freeze")
        session.add(project)
        session.flush()
        project_id = UUID(project.id)
    return factory, store, project_id


def _write_prompts(root: Path, version: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for filename in sorted(set(PROMPT_FILES.values())):
        name = Path(filename).stem
        (root / filename).write_text(
            "\n".join(
                [
                    "---",
                    f"name: {name}",
                    f"version: {version}",
                    "---",
                    f"PROMPT-{version} operation=$operation schema=$schema_name",
                ]
            ),
            encoding="utf-8",
        )


def test_approval_freezes_prompt_bodies_routes_and_budget_across_restart(tmp_path, monkeypatch) -> None:
    async def scenario() -> None:
        factory, store, project_id = _workspace(tmp_path)
        prompt_root = tmp_path / "prompts"
        _write_prompts(prompt_root, "1.0.0")
        runtime = create_runtime(
            factory,
            store,
            prompt_store=PromptStore(prompt_root),
            demo_mode=True,
        )
        run = await runtime.create_continuation_run(
            project_id=project_id,
            target_chars=4_000,
            budget={
                "max_calls": 300,
                "max_tokens": 800_000,
                "max_cost_usd": 20,
                "max_wall_time_seconds": 60,
                "max_task_attempts": 3,
                "max_revision_rounds": 3,
            },
        )
        await runtime._build_outline(runtime.repository.get_run(run.id))
        waiting = runtime.repository.get_run(run.id)
        assert waiting.plan_hash
        original_writer_route = ROUTING_PRESETS["hybrid-quality"][AgentRole.WRITER]
        await runtime.approve_outline(run.id, waiting.plan_hash)
        approved = runtime.repository.get_run(run.id)

        _write_prompts(prompt_root, "2.0.0")
        monkeypatch.setitem(
            ROUTING_PRESETS["hybrid-quality"],
            AgentRole.WRITER,
            ModelRoute(ProviderName.OPENAI, "changed-after-approval"),
        )
        restarted = create_runtime(
            factory,
            store,
            prompt_store=PromptStore(prompt_root),
            demo_mode=True,
        )
        restored = restarted.repository.get_run(run.id)
        frozen_writer = restarted._prompt_store_for(restored).for_role(AgentRole.WRITER)

        assert frozen_writer.version == "1.0.0"
        assert "PROMPT-1.0.0" in frozen_writer.body
        assert "PROMPT-2.0.0" in PromptStore(prompt_root).for_role(AgentRole.WRITER).body
        assert restarted._route_for(restored, AgentRole.WRITER) == original_writer_route
        frozen_id = UUID(str(approved.usage["frozen_execution_config_artifact_id"]))
        frozen = restarted.repository.read_json(frozen_id)
        assert frozen["plan_hash"] == waiting.plan_hash
        assert frozen["budget"] == approved.budget
        with factory() as session:
            artifact = session.scalar(select(Artifact).where(Artifact.id == str(frozen_id)))
        assert artifact.kind == "run_execution_config_frozen"

    asyncio.run(scenario())


def test_explicit_canon_override_is_persisted_as_immutable_artifact(tmp_path) -> None:
    async def scenario() -> None:
        factory, store, project_id = _workspace(tmp_path)
        runtime = create_runtime(factory, store, demo_mode=True)
        original_demo = runtime._demo_value

        def scripted(operation, context):
            if operation == "build_plan":
                plan = original_demo(operation, context)
                return RunPlanV1.model_validate(
                    plan.model_copy(
                        update={
                            "canon_conflicts": [
                                CanonConflictV1(
                                    requested_change="让旧事实在独立分支发生变化",
                                    conflicting_fact_ids=["canon-1"],
                                    explanation="用户明确要求分支覆盖",
                                )
                            ]
                        }
                    ).model_dump(mode="python")
                )
            return original_demo(operation, context)

        runtime._demo_value = scripted
        run = await runtime.create_continuation_run(
            project_id=project_id,
            branch_name="alternate",
            target_chars=4_000,
        )
        await runtime._build_outline(runtime.repository.get_run(run.id))
        waiting = runtime.repository.get_run(run.id)
        await runtime.approve_outline(run.id, waiting.plan_hash, allow_canon_overrides=True)
        approved = runtime.repository.get_run(run.id)
        override_id = UUID(str(approved.usage["canon_override_artifact_id"]))
        override = runtime.repository.read_json(override_id)

        assert override["plan_hash"] == waiting.plan_hash
        assert override["allowed_fact_ids"] == ["canon-1"]
        assert override["approved_conflicts"][0]["requested_change"]
        with factory() as session:
            artifact = session.get(Artifact, str(override_id))
        assert artifact.kind == "canon_override_approval"
        assert artifact.immutable is True

    asyncio.run(scenario())


def test_immutable_canon_guard_rejects_deletion_unless_fact_was_approved() -> None:
    project_id = UUID("00000000-0000-0000-0000-000000000001")
    fact = StoryFactV1(
        fact_id="canon-1",
        category="identity",
        statement="角色已经死亡",
        immutable_canon=True,
    )
    previous = StoryStateV1(project_id=project_id, facts=[fact])
    candidate = StoryStateV1(project_id=project_id, facts=[])

    assert _immutable_canon_violations(previous, candidate, allowed_fact_ids=set()) == [
        "不可变事实被删除：canon-1"
    ]
    assert (
        _immutable_canon_violations(
            previous,
            candidate,
            allowed_fact_ids={"canon-1"},
        )
        == []
    )
