from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from backend.app import models
from backend.app.config import Settings
from backend.app.database import build_engine, make_session_factory
from backend.app.main import create_app
from backend.app.orchestrator import create_runtime
from backend.app.schemas import AssertionKind, StoryStateV1, SummaryDimension
from backend.app.storage import ArtifactStore

SUMMARY_BUNDLE_DOCUMENT = """# 《无名城》-九维总结

## 一、核心摘要
林岚在雪夜进入只进不出的无名城，并发现自己一直被监视。

## 二、故事大纲
| 时间线 | 事件 | 影响 |
|---|---|---|
| 雪夜 | 林岚进入无名城 | 城门随后关闭 |

## 三、角色
林岚：刚进入无名城的调查者。

## 四、冲突
林岚寻找出口，而守城势力阻止任何人离开。

## 五、核心悬念
城门为何只进不出。

## 六、背景/世界观
城外终年落雪；城门开启规律尚不明确。

## 七、叙事视角
第三人称限知，只呈现林岚能够感知的信息。

## 八、节奏控制
入城前缓慢，城门关闭后加速。

## 九、伏笔追踪
| 伏笔 | 埋设位置 | 当前状态 | 可能发展方向 |
|---|---|---|---|
| 坏掉的怀表 | 入城时 | 已埋设 | 可能关联失踪的守门人 |
"""


class FiniteEventBroker:
    async def subscribe(self, run_id: UUID):
        yield {
            "event": "outline_ready",
            "run_id": str(run_id),
            "payload": {"stage": "test"},
        }


class FakeRuntime:
    """Deterministic runtime that exercises API/runtime ownership boundaries."""

    def __init__(self, session_factory: Any, artifact_store: ArtifactStore) -> None:
        self.session_factory = session_factory
        self.artifact_store = artifact_store
        self.events = FiniteEventBroker()
        self.started = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.started = False

    async def enqueue(self, run_id: UUID) -> None:
        with self.session_factory.begin() as session:
            run = session.get(models.Run, str(run_id))
            assert run is not None
            if run.kind != "continuation" or run.plan_artifact_id:
                return
            chapter_id = str(uuid4())
            plan = {
                "schema_version": "1.0",
                "plan_id": str(uuid4()),
                "run_id": run.id,
                "branch_name": run.branch_name,
                "start_point": "承接源稿结尾",
                "target_chars": run.target_chars,
                "canon_conflicts": [],
                "style_card": {"pov_strategy": ["限知视角"]},
                "chapter_briefs": [
                    {
                        "chapter_id": chapter_id,
                        "number": 1,
                        "title": "第一章 余波",
                        "target_chars": run.target_chars,
                        "pov_characters": ["主角"],
                        "goals": ["承接前文"],
                        "required_beats": ["推进主要冲突"],
                        "dependency_chapter_ids": [],
                    }
                ],
            }
            stored = self.artifact_store.put_json(plan)
            artifact = models.Artifact(
                project_id=run.project_id,
                run_id=run.id,
                kind="run_plan",
                version=1,
                relative_path=stored.relative_path,
                sha256=stored.sha256,
                media_type=stored.media_type,
                byte_size=stored.byte_size,
                source_artifact_ids_json=[],
                generation_config_json={"title": "测试章纲"},
                immutable=True,
            )
            session.add(artifact)
            session.flush()
            run.plan_artifact_id = artifact.id
            run.plan_hash = stored.sha256
            run.status = "awaiting_approval"
            run.stage = "plan_approval"

    async def approve_outline(
        self, run_id: UUID, plan_hash: str, allow_canon_overrides: bool = False
    ) -> None:
        assert not allow_canon_overrides
        with self.session_factory.begin() as session:
            run = session.get(models.Run, str(run_id))
            assert run is not None
            assert run.status == "awaiting_approval"
            assert run.plan_hash == plan_hash
            run.approved_at = datetime.now(UTC)
            run.status = "queued"
            run.stage = "writing_queued"

    async def revise_outline(self, run_id: UUID, feedback: str, chapter_id: str | None = None) -> None:
        assert feedback
        with self.session_factory.begin() as session:
            run = session.get(models.Run, str(run_id))
            assert run is not None
            run.plan_artifact_id = None
            run.plan_hash = None
            run.status = "queued"
            run.stage = "plan_revision_queued"

    def pause(self, run_id: UUID) -> None:
        with self.session_factory.begin() as session:
            run = session.get(models.Run, str(run_id))
            assert run is not None
            run.pause_requested = True
            run.status = "paused"
            run.stage = "paused"

    async def resume(self, run_id: UUID) -> None:
        with self.session_factory.begin() as session:
            run = session.get(models.Run, str(run_id))
            assert run is not None
            run.pause_requested = False
            run.status = "queued"
            run.stage = "resume_queued"

    def cancel(self, run_id: UUID, force: bool = False) -> None:
        with self.session_factory.begin() as session:
            run = session.get(models.Run, str(run_id))
            assert run is not None
            run.cancel_requested = True
            run.status = "cancelled"
            run.stage = "force_cancelled" if force else "cancelled"
            run.finished_at = datetime.now(UTC)

    def retry_task(self, task_id: UUID, acknowledge_indeterminate: bool = False) -> None:
        with self.session_factory.begin() as session:
            task = session.get(models.Task, str(task_id))
            assert task is not None
            if task.status == "indeterminate" and not acknowledge_indeterminate:
                raise RuntimeError("acknowledgement required")
            task.status = "pending"
            task.error_code = None
            task.error_message = None


@pytest.fixture
def api_client(tmp_path: Path):
    db_engine = build_engine(f"sqlite:///{(tmp_path / 'api.sqlite3').as_posix()}")
    factory = make_session_factory(db_engine)
    store = ArtifactStore(tmp_path / "artifacts")
    runtime = FakeRuntime(factory, store)
    settings = Settings(data_dir=tmp_path, env_file=tmp_path / ".env")
    app = create_app(
        settings=settings,
        db_engine=db_engine,
        session_factory=factory,
        artifact_store=store,
        runtime=runtime,
    )
    with TestClient(app) as client:
        yield client, runtime, factory


def _import_project(client: TestClient) -> dict[str, Any]:
    response = client.post(
        "/api/projects/import",
        files={"file": ("前情.txt", "第一章 开端\n雨停之后，城门重新打开。".encode(), "text/plain")},
    )
    assert response.status_code == 201, response.text
    return response.json()["project"]


def test_full_editorial_control_contract(api_client) -> None:
    client, runtime, factory = api_client
    assert runtime.started
    project = _import_project(client)
    assert project["name"] == "前情"
    assert project["source_count"] == 1

    response = client.post(
        f"/api/projects/{project['id']}/continuation-runs",
        json={
            "target_chars": 4_000,
            "user_requirements": "保持限知视角",
            "routing_preset": "hybrid-quality",
        },
    )
    assert response.status_code == 202, response.text
    run = response.json()["run"]
    run_id = run["id"]
    assert run["status"] == "awaiting_outline_approval"
    assert run["plan"]["chapters"][0]["title"] == "第一章 余波"
    plan_hash = run["plan"]["approved_hash"] or run["artifacts"][0]["sha256"]

    assert client.post(f"/api/runs/{run_id}/pause", json={}).status_code == 409
    assert client.post(f"/api/runs/{run_id}/resume", json={}).status_code == 409

    mismatch = client.post(f"/api/runs/{run_id}/outline/approve", json={"plan_hash": "0" * 64})
    assert mismatch.status_code == 409

    unsafe_override = client.post(
        f"/api/runs/{run_id}/outline/approve",
        json={"plan_hash": plan_hash, "allow_canon_overrides": True},
    )
    assert unsafe_override.status_code == 409
    assert "新的分支名" in unsafe_override.json()["detail"]

    approved = client.post(f"/api/runs/{run_id}/outline/approve", json={"plan_hash": plan_hash})
    assert approved.status_code == 200, approved.text
    assert approved.json()["run"]["status"] == "queued"
    assert approved.json()["run"]["plan"]["approved_hash"] == plan_hash

    assert client.post(f"/api/runs/{run_id}/pause", json={}).json()["run"]["status"] == "paused"
    assert client.post(f"/api/runs/{run_id}/resume", json={}).json()["run"]["status"] == "queued"

    stream = client.get(f"/api/runs/{run_id}/events")
    assert stream.status_code == 200
    assert stream.headers["content-type"].startswith("text/event-stream")
    assert "event: snapshot" in stream.text
    assert "event: stage" in stream.text
    assert '"type": "outline_ready"' in stream.text
    assert '"run"' in stream.text

    with factory.begin() as session:
        task = models.Task(
            run_id=run_id,
            operation="write_chapter",
            status="failed",
            logical_key="test-retry",
            error_code="test",
            error_message="temporary failure",
        )
        session.add(task)
        session.flush()
        task_id = task.id
    task_summary = next(
        item for item in client.get(f"/api/runs/{run_id}").json()["run"]["tasks"] if item["id"] == task_id
    )
    assert {
        key: task_summary[key]
        for key in (
            "operation",
            "status",
            "retryable",
            "requires_acknowledgement",
            "error_code",
            "error_message",
        )
    } == {
        "operation": "write_chapter",
        "status": "failed",
        "retryable": True,
        "requires_acknowledgement": False,
        "error_code": "test",
        "error_message": "temporary failure",
    }
    retried = client.post(
        f"/api/runs/{run_id}/tasks/{task_id}/retry",
        json={"acknowledge_indeterminate": False},
    )
    assert retried.status_code == 202
    with factory() as session:
        assert session.get(models.Task, task_id).status == "pending"

    with factory.begin() as session:
        uncertain_task = models.Task(
            run_id=run_id,
            operation="revise_chapter",
            status="indeterminate",
            logical_key="test-indeterminate-retry",
            attempt_count=1,
            max_attempts=3,
            error_code="transport_interrupted",
            error_message="request sent; response persistence is uncertain",
        )
        session.add(uncertain_task)
        session.flush()
        uncertain_task_id = uncertain_task.id
    uncertain_summary = next(
        item
        for item in client.get(f"/api/runs/{run_id}").json()["run"]["tasks"]
        if item["id"] == uncertain_task_id
    )
    assert uncertain_summary["retryable"] is True
    assert uncertain_summary["requires_acknowledgement"] is True
    acknowledged = client.post(
        f"/api/runs/{run_id}/tasks/{uncertain_task_id}/retry",
        json={"acknowledge_indeterminate": True},
    )
    assert acknowledged.status_code == 202
    assert (
        next(item for item in acknowledged.json()["run"]["tasks"] if item["id"] == uncertain_task_id)[
            "status"
        ]
        == "pending"
    )

    cancelled = client.post(f"/api/runs/{run_id}/cancel", json={"force": False})
    assert cancelled.status_code == 200
    assert cancelled.json()["run"]["status"] == "cancelled"


def test_artifact_download_and_unsafe_index_rejection(api_client) -> None:
    client, _, factory = api_client
    project = _import_project(client)
    source = project["sources"][0]

    response = client.get(f"/api/artifacts/{source['id']}/download")
    assert response.status_code == 200
    assert "雨停之后" in response.text
    assert "attachment" in response.headers["content-disposition"]

    preview = client.get(f"/api/artifacts/{source['id']}/preview")
    assert preview.status_code == 200
    assert "雨停之后" in preview.text
    assert "inline" in preview.headers["content-disposition"]

    with factory.begin() as session:
        unsafe = models.Artifact(
            project_id=project["id"],
            kind="source_document",
            relative_path="../outside.txt",
            sha256="0" * 64,
            media_type="text/plain",
            byte_size=0,
            source_artifact_ids_json=[],
            generation_config_json={},
        )
        session.add(unsafe)
        session.flush()
        unsafe_id = unsafe.id
    rejected = client.get(f"/api/artifacts/{unsafe_id}/download")
    assert rejected.status_code == 410

    rejected_preview = client.get(f"/api/artifacts/{unsafe_id}/preview")
    assert rejected_preview.status_code == 410


def test_chapter_diff_uses_previous_authored_revision_not_final_promotion(api_client) -> None:
    client, runtime, factory = api_client
    project = _import_project(client)
    created = client.post(
        f"/api/projects/{project['id']}/continuation-runs",
        json={"target_chars": 4_000, "routing_preset": "hybrid-quality"},
    ).json()["run"]
    run_id = created["id"]
    chapter_id = created["plan"]["chapters"][0]["id"]

    def add_text(kind: str, text: str, *, revision: int, version: int, status: str = "") -> None:
        stored = runtime.artifact_store.put_text(
            text,
            suffix=".md",
            media_type="text/markdown; charset=utf-8",
        )
        with factory.begin() as session:
            session.add(
                models.Artifact(
                    project_id=project["id"],
                    run_id=run_id,
                    kind=kind,
                    version=version,
                    relative_path=stored.relative_path,
                    sha256=stored.sha256,
                    media_type=stored.media_type,
                    byte_size=stored.byte_size,
                    source_artifact_ids_json=[],
                    generation_config_json={
                        "chapter_id": chapter_id,
                        "chapter_number": 1,
                        "title": "第一章 余波",
                        "revision": revision,
                        "status": status,
                    },
                )
            )

    add_text("write_chapter", "初稿正文", revision=0, version=1, status="assembled")
    add_text("revise_chapter", "修订后正文", revision=1, version=2)
    # chapter_final promotes revision 1 without authoring another textual version.
    add_text("chapter_final", "修订后正文", revision=1, version=3, status="passed")

    chapter = client.get(f"/api/runs/{run_id}").json()["run"]["chapters"][0]
    assert chapter["content"] == "修订后正文"
    assert chapter["previous_content"] == "初稿正文"
    assert chapter["revision"] == 1


def test_human_chapter_edit_versions_content_and_invalidates_delivery(api_client) -> None:
    client, runtime, factory = api_client
    project = _import_project(client)
    created = client.post(
        f"/api/projects/{project['id']}/continuation-runs",
        json={"target_chars": 4_000, "routing_preset": "hybrid-quality"},
    ).json()["run"]
    run_id = created["id"]
    chapter_id = created["plan"]["chapters"][0]["id"]
    source = runtime.artifact_store.put_text("初稿正文")
    delivery = runtime.artifact_store.put_bytes(b"delivery", suffix=".zip")
    with factory.begin() as session:
        session.add_all(
            [
                models.Artifact(
                    project_id=project["id"],
                    run_id=run_id,
                    kind="write_chapter",
                    version=1,
                    relative_path=source.relative_path,
                    sha256=source.sha256,
                    media_type=source.media_type,
                    byte_size=source.byte_size,
                    source_artifact_ids_json=[],
                    generation_config_json={
                        "chapter_id": chapter_id,
                        "chapter_number": 1,
                        "title": "第一章 余波",
                        "revision": 0,
                        "status": "assembled",
                    },
                ),
                models.Artifact(
                    project_id=project["id"],
                    run_id=run_id,
                    kind="delivery_reading_bundle",
                    version=1,
                    relative_path=delivery.relative_path,
                    sha256=delivery.sha256,
                    media_type=delivery.media_type,
                    byte_size=delivery.byte_size,
                    source_artifact_ids_json=[],
                    generation_config_json={"title": "阅读包"},
                ),
            ]
        )
        run = session.get(models.Run, run_id)
        assert run is not None
        run.status = "queued"
        session.flush()
        delivery_id = next(
            artifact.id
            for artifact in session.query(models.Artifact).filter_by(run_id=run_id).all()
            if artifact.kind == "delivery_reading_bundle"
        )

    base = client.get(f"/api/runs/{run_id}").json()["run"]["chapters"][0]
    rejected = client.patch(
        f"/api/runs/{run_id}/chapters/{chapter_id}/content",
        json={
            "content": "人工修订正文",
            "base_revision": base["revision"],
            "base_sha256": base["content_sha256"],
        },
    )
    assert rejected.status_code == 409

    with factory.begin() as session:
        run = session.get(models.Run, run_id)
        assert run is not None
        run.status = "paused"
        run.stage = "paused"
    saved = client.patch(
        f"/api/runs/{run_id}/chapters/{chapter_id}/content",
        json={
            "content": "人工修订正文",
            "base_revision": base["revision"],
            "base_sha256": base["content_sha256"],
            "reason": "修正人物动机",
        },
    )
    assert saved.status_code == 200, saved.text
    run = saved.json()["run"]
    chapter = run["chapters"][0]
    assert chapter["content"] == "人工修订正文"
    assert chapter["previous_content"] == "初稿正文"
    assert chapter["revision"] == 1
    assert chapter["human_edited"] is True
    assert chapter["version_source"] == "human"
    assert run["status"] == "needs_human"
    assert run["content_revision_invalidated"] is True
    assert run["delivery_valid"] is False
    assert client.get(f"/api/artifacts/{delivery_id}/download").status_code == 409

    conflict = client.patch(
        f"/api/runs/{run_id}/chapters/{chapter_id}/content",
        json={
            "content": "覆盖别人的版本",
            "base_revision": base["revision"],
            "base_sha256": base["content_sha256"],
        },
    )
    assert conflict.status_code == 409


def test_validation_and_not_found_contract(api_client) -> None:
    client, _, _ = api_client
    providers = client.get("/api/providers/status").json()["providers"]
    assert all(item["configured"] is False and item["available"] is False for item in providers)
    prompts = client.get("/api/prompts/versions").json()["prompts"]
    assert len(prompts) == 5
    assert all("sha256" in item and "content" not in item for item in prompts)

    missing = str(uuid4())
    assert client.get(f"/api/projects/{missing}").status_code == 404
    assert (
        client.post(
            f"/api/projects/{missing}/continuation-runs",
            json={"target_chars": 3_999, "routing_preset": "hybrid-quality"},
        ).status_code
        == 422
    )
    unsupported = client.post(
        "/api/projects/import",
        files={"file": ("book.epub", b"not-an-epub", "application/epub+zip")},
    )
    assert unsupported.status_code == 415


def test_provider_keys_are_saved_without_being_returned(api_client) -> None:
    client, _, _ = api_client
    response = client.post(
        "/api/providers/keys",
        json={"openai_api_key": "test-openai-key", "deepseek_api_key": "test-deepseek-key"},
    )
    assert response.status_code == 200, response.text
    providers = {item["id"]: item for item in response.json()["providers"]}
    assert providers["openai"]["configured"] is True
    assert providers["deepseek"]["configured"] is True
    assert "test-openai-key" not in response.text
    assert "test-deepseek-key" not in response.text
    env_text = (client.app.state.settings.env_file).read_text(encoding="utf-8")
    assert 'OPENAI_API_KEY="test-openai-key"' in env_text
    assert 'DEEPSEEK_API_KEY="test-deepseek-key"' in env_text


def test_import_inspection_is_read_only_and_reports_encoding(api_client) -> None:
    client, _, _ = api_client
    before = client.get("/api/projects").json()["projects"]
    response = client.post(
        "/api/projects/import/inspect",
        files={
            "file": (
                "雨站.txt",
                "第一章 雨站\n凌晨两点，车站仍亮着灯。".encode("utf-16"),
                "text/plain",
            )
        },
    )

    assert response.status_code == 200, response.text
    inspection = response.json()["inspection"]
    assert inspection["kind"] == "raw_text"
    assert inspection["inferred_title"] == "雨站"
    assert inspection["document_count"] == 1
    assert inspection["documents"][0]["source_name"] == "雨站.txt"
    assert inspection["documents"][0]["encoding"].startswith("utf-16")
    assert "凌晨两点" in inspection["documents"][0]["text"]
    assert client.get("/api/projects").json()["projects"] == before


def test_batch_import_preserves_requested_source_order(api_client) -> None:
    client, _, factory = api_client
    inspected = client.post(
        "/api/projects/import/inspect",
        files=[
            ("files", ("第10章.txt", "第十章", "text/plain")),
            ("files", ("第2章.md", "第二章", "text/markdown")),
        ],
    )
    assert inspected.status_code == 200, inspected.text
    documents = inspected.json()["inspection"]["documents"]
    assert [item["source_name"] for item in documents] == ["第2章.md", "第10章.txt"]

    imported = client.post(
        "/api/projects/import",
        data={"order": json.dumps(["第10章.txt", "第2章.md"])},
        files=[
            ("files", ("第10章.txt", "第十章", "text/plain")),
            ("files", ("第2章.md", "第二章", "text/markdown")),
        ],
    )
    assert imported.status_code == 201, imported.text
    project_id = imported.json()["project"]["id"]
    with factory() as session:
        sources = (
            session.query(models.Artifact)
            .filter_by(project_id=project_id, kind="source_document")
            .all()
        )
        sources.sort(key=lambda item: item.generation_config_json["sequence_index"])
        assert [item.generation_config_json["sequence_index"] for item in sources] == [1, 2]
        assert [item.generation_config_json["source_name"] for item in sources] == ["第10章.txt", "第2章.md"]


def test_batch_import_preserves_relative_paths_and_allows_same_file_names(api_client) -> None:
    client, _, factory = api_client
    files = [
        ("files", ("原稿/第一卷/第1章.txt", "第一卷第一章", "text/plain")),
        ("files", ("原稿/第二卷/第1章.txt", "第二卷第一章", "text/plain")),
    ]
    inspected = client.post("/api/projects/import/inspect", files=files)
    assert inspected.status_code == 200, inspected.text
    assert [item["source_name"] for item in inspected.json()["inspection"]["documents"]] == [
        "原稿/第一卷/第1章.txt",
        "原稿/第二卷/第1章.txt",
    ]

    imported = client.post(
        "/api/projects/import",
        data={"order": json.dumps(["原稿/第二卷/第1章.txt", "原稿/第一卷/第1章.txt"])},
        files=files,
    )
    assert imported.status_code == 201, imported.text
    project_id = imported.json()["project"]["id"]
    with factory() as session:
        sources = (
            session.query(models.Artifact)
            .filter_by(project_id=project_id, kind="source_document")
            .all()
        )
        sources.sort(key=lambda item: item.generation_config_json["sequence_index"])
        assert [item.generation_config_json["source_name"] for item in sources] == [
            "原稿/第二卷/第1章.txt",
            "原稿/第一卷/第1章.txt",
        ]


def test_summary_bundle_import_is_immediately_ready_for_continuation(api_client) -> None:
    client, runtime, factory = api_client
    imported = client.post(
        "/api/projects/import",
        files={
            "file": (
                "无名城-九维总结.md",
                SUMMARY_BUNDLE_DOCUMENT.encode("utf-8"),
                "text/markdown",
            )
        },
    )

    assert imported.status_code == 201, imported.text
    project_view = imported.json()["project"]
    project_id = project_view["id"]
    assert project_view["active_branch"] == "main"
    assert len(project_view["story_state"]["canon"]) >= len(SummaryDimension)

    with factory() as session:
        project = session.get(models.Project, project_id)
        assert project is not None
        assert project.current_story_state_artifact_id is not None
        branch = (
            session.query(models.BranchHead)
            .filter_by(
                project_id=project_id,
                branch_name="main",
            )
            .one()
        )
        assert branch.artifact_id == project.current_story_state_artifact_id
        state_artifact = session.get(models.Artifact, project.current_story_state_artifact_id)
        assert state_artifact is not None
        assert state_artifact.generation_config_json["imported_from_summary"] is True
        manifest_artifact = (
            session.query(models.Artifact)
            .filter_by(
                project_id=project_id,
                kind="import_manifest",
            )
            .one()
        )
        state_path = state_artifact.relative_path
        manifest_path = manifest_artifact.relative_path
        branch_artifact_id = branch.artifact_id

    state = StoryStateV1.model_validate_json(runtime.artifact_store.read_text(state_path))
    assert state.branch_name == "main"
    assert all(fact.immutable_canon is False for fact in state.facts)
    assert {fact.assertion_kind for fact in state.facts} >= {
        AssertionKind.INFERENCE,
        AssertionKind.SPECULATION,
    }
    assert all(
        evidence.span.source_name == "无名城-九维总结.md" and evidence.span.source_artifact_id is not None
        for fact in state.facts
        for evidence in fact.evidence
    )
    import_manifest = json.loads(runtime.artifact_store.read_text(manifest_path))
    assert set(import_manifest["summaries"]) == {item.value for item in SummaryDimension}
    assert import_manifest["summary_story_state_artifact_id"] == branch_artifact_id

    continued = client.post(
        f"/api/projects/{project_id}/continuation-runs",
        json={
            "target_chars": 4_000,
            "routing_preset": "hybrid-quality",
            "user_requirements": "承接城门关闭后的雪夜。",
        },
    )
    assert continued.status_code == 202, continued.text
    assert continued.json()["run"]["status"] == "awaiting_outline_approval"
    assert continued.json()["run"]["plan"]["chapters"]
    with factory() as session:
        runs = session.query(models.Run).filter_by(project_id=project_id).all()
        assert [run.kind for run in runs] == ["continuation"]
        analyze_tasks = (
            session.query(models.Task)
            .join(models.Run, models.Task.run_id == models.Run.id)
            .filter(models.Run.project_id == project_id, models.Task.operation == "analyze_chunk")
            .all()
        )
        assert analyze_tasks == []


def test_real_runtime_plans_from_imported_summary_state_without_analysis(tmp_path: Path) -> None:
    db_engine = build_engine(f"sqlite:///{(tmp_path / 'summary-runtime.sqlite3').as_posix()}")
    factory = make_session_factory(db_engine)
    store = ArtifactStore(tmp_path / "summary-runtime-artifacts")
    runtime = create_runtime(factory, store, demo_mode=True, worker_count=1)
    app = create_app(
        settings=Settings(data_dir=tmp_path / "summary-runtime-data"),
        db_engine=db_engine,
        session_factory=factory,
        artifact_store=store,
        runtime=runtime,
    )

    with TestClient(app) as client:
        imported = client.post(
            "/api/projects/import",
            files={
                "file": (
                    "无名城-九维总结.md",
                    SUMMARY_BUNDLE_DOCUMENT.encode("utf-8"),
                    "text/markdown",
                )
            },
        )
        assert imported.status_code == 201, imported.text
        project_id = imported.json()["project"]["id"]
        created = client.post(
            f"/api/projects/{project_id}/continuation-runs",
            json={"target_chars": 4_000, "routing_preset": "hybrid-quality"},
        )
        assert created.status_code == 202, created.text
        run_id = created.json()["run"]["id"]

        deadline = time.monotonic() + 5
        run_view = created.json()["run"]
        while run_view["status"] != "awaiting_outline_approval" and time.monotonic() < deadline:
            time.sleep(0.02)
            run_view = client.get(f"/api/runs/{run_id}").json()["run"]

        assert run_view["status"] == "awaiting_outline_approval", run_view
        assert run_view["plan"]["chapters"]
        with factory() as session:
            project = session.get(models.Project, project_id)
            run = session.get(models.Run, run_id)
            assert project is not None and run is not None
            assert run.usage_json["base_story_state_artifact_id"] == (project.current_story_state_artifact_id)
            operations = {
                item[0]
                for item in session.query(models.Task.operation).filter(models.Task.run_id == run_id).all()
            }
        assert "build_plan" in operations
        assert "analyze_chunk" not in operations
