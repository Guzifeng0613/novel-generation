from __future__ import annotations

from uuid import UUID

from sqlalchemy import select

from backend.app.database import Base, build_engine, make_session_factory
from backend.app.models import Attempt, ModelCall, Project, Task
from backend.app.orchestrator import create_runtime
from backend.app.providers import (
    ROUTING_PRESETS,
    AgentRole,
    LLMRequest,
    LLMResponse,
    ModelUsage,
    ProviderName,
)
from backend.app.storage import ArtifactStore


def _runtime(tmp_path):
    engine = build_engine(f"sqlite:///{(tmp_path / 'recovery.sqlite3').as_posix()}")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    store = ArtifactStore(tmp_path / "artifacts")
    with factory.begin() as session:
        project = Project(name="恢复测试", data_path="projects/recovery")
        session.add(project)
        session.flush()
        project_id = UUID(project.id)
    return create_runtime(factory, store, demo_mode=True), factory, project_id


def _running_attempt(runtime, project_id: UUID, logical_key: str):
    run = runtime.repository.create_run(
        project_id=project_id,
        kind="analysis",
        branch_name="main",
        target_chars=None,
        provider_preset="hybrid-quality",
        user_requirements="",
        budget={"max_calls": 10},
    )
    runtime.repository.update_run(run.id, status="running", stage="analysis")
    task = runtime.repository.ensure_task(
        run_id=run.id,
        logical_key=logical_key,
        operation="analyze_chunk",
        route=ROUTING_PRESETS["hybrid-quality"][AgentRole.ANALYZER],
        sequence_index=1,
        context={},
        input_artifact_ids=(),
        dependencies=(),
        budget={},
        max_attempts=3,
    )
    task, attempt_id = runtime.repository.start_attempt(task.id, lease_owner="crashed-worker")
    return run, task, attempt_id


def test_recovery_requeues_claimed_attempt_before_provider_dispatch(tmp_path) -> None:
    runtime, factory, project_id = _runtime(tmp_path)
    run, task, attempt_id = _running_attempt(runtime, project_id, "claimed")

    resumable = runtime.repository.recover_interrupted()

    assert run.id in resumable
    assert runtime.repository.get_run(run.id).status == "queued"
    with factory() as session:
        stored_task = session.get(Task, str(task.id))
        attempt = session.get(Attempt, str(attempt_id))
    assert stored_task.status == "pending"
    assert attempt.status == "interrupted_before_dispatch"


def test_recovery_marks_dispatched_attempt_indeterminate(tmp_path) -> None:
    runtime, factory, project_id = _runtime(tmp_path)
    run, task, attempt_id = _running_attempt(runtime, project_id, "dispatched")
    runtime.repository.mark_request_dispatched(task.id, attempt_id, request_hash="a" * 64)

    resumable = runtime.repository.recover_interrupted()

    assert run.id not in resumable
    assert runtime.repository.get_run(run.id).status == "needs_human"
    with factory() as session:
        stored_task = session.get(Task, str(task.id))
        attempt = session.get(Attempt, str(attempt_id))
    assert stored_task.status == "indeterminate"
    assert attempt.status == "indeterminate"


def test_recovery_completes_response_that_was_landed_before_crash(tmp_path) -> None:
    runtime, factory, project_id = _runtime(tmp_path)
    run, task, attempt_id = _running_attempt(runtime, project_id, "landed")
    runtime.repository.mark_request_dispatched(task.id, attempt_id, request_hash="b" * 64)
    artifact = runtime.repository.store_text(
        project_id=project_id,
        run_id=run.id,
        task_id=task.id,
        kind="analyze_chunk",
        text="landed response",
    )
    response = LLMResponse(
        provider=ProviderName.DEEPSEEK,
        model="test-model",
        request_id="request-landed",
        text="landed response",
        finish_status="stop",
        usage=ModelUsage(input_tokens=10, output_tokens=5, total_tokens=15),
    )
    request = LLMRequest(role=AgentRole.ANALYZER, system_prompt="system", user_prompt="user")
    runtime.repository.mark_response_landed(
        task.id,
        attempt_id,
        artifact.id,
        response,
        request=request,
        input_hash="d" * 64,
        output_hash=artifact.sha256,
    )

    resumable = runtime.repository.recover_interrupted()

    assert run.id in resumable
    with factory() as session:
        stored_task = session.get(Task, str(task.id))
        attempt = session.get(Attempt, str(attempt_id))
        calls = session.scalars(select(ModelCall).where(ModelCall.attempt_id == str(attempt_id))).all()
    assert stored_task.status == "completed"
    assert stored_task.output_artifact_id == str(artifact.id)
    assert attempt.status == "completed"
    assert attempt.response_artifact_id == str(artifact.id)
    assert len(calls) == 1


def test_recovery_preserves_pause_and_cancel_intent(tmp_path) -> None:
    runtime, _factory, project_id = _runtime(tmp_path)
    paused = runtime.repository.create_run(
        project_id=project_id,
        kind="analysis",
        branch_name="main",
        target_chars=None,
        provider_preset="hybrid-quality",
        user_requirements="",
        budget={"max_calls": 10},
    )
    cancelled = runtime.repository.create_run(
        project_id=project_id,
        kind="analysis",
        branch_name="main",
        target_chars=None,
        provider_preset="hybrid-quality",
        user_requirements="",
        budget={"max_calls": 10},
    )
    runtime.repository.update_run(paused.id, status="pausing", pause_requested=True)
    runtime.repository.update_run(cancelled.id, status="cancelling", cancel_requested=True)

    resumable = runtime.repository.recover_interrupted()

    assert paused.id not in resumable
    assert cancelled.id not in resumable
    assert runtime.repository.get_run(paused.id).status == "paused"
    assert runtime.repository.get_run(cancelled.id).status == "cancelled"


def test_recovery_detects_artifact_written_just_before_phase_checkpoint(tmp_path) -> None:
    runtime, factory, project_id = _runtime(tmp_path)
    run, task, attempt_id = _running_attempt(runtime, project_id, "artifact-window")
    runtime.repository.mark_request_dispatched(task.id, attempt_id, request_hash="c" * 64)
    artifact = runtime.repository.store_text(
        project_id=project_id,
        run_id=run.id,
        task_id=task.id,
        kind="analyze_chunk",
        text="persisted before phase update",
    )

    runtime.repository.recover_interrupted()

    with factory() as session:
        stored_task = session.scalar(select(Task).where(Task.id == str(task.id)))
        call = session.scalar(select(ModelCall).where(ModelCall.attempt_id == str(attempt_id)))
    assert stored_task.status == "completed"
    assert stored_task.output_artifact_id == str(artifact.id)
    assert call.status == "recovered_response"
