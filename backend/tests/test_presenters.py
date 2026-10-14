from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from backend.app import models
from backend.app.database import build_engine, init_db, make_session_factory
from backend.app.presenters import ViewBuilder
from backend.app.storage import ArtifactStore


def test_run_detail_resolves_review_issue_location_and_references(tmp_path: Path) -> None:
    engine = build_engine(f"sqlite:///{(tmp_path / 'presenters.sqlite3').as_posix()}")
    init_db(engine)
    factory = make_session_factory(engine)
    store = ArtifactStore(tmp_path / "artifacts")

    with factory.begin() as session:
        project = models.Project(name="雨站", description="", data_path=str(tmp_path))
        session.add(project)
        session.flush()

        story_state = {
            "facts": [{"fact_id": "canon-injury", "statement": "林岚左臂骨折", "category": "伤势"}],
            "mysteries": [{"mystery_id": "mystery-watch", "label": "坏掉的怀表", "follow_up": "待揭示"}],
            "foreshadowing": [],
        }
        stored_state = store.put_json(story_state)
        state_artifact = models.Artifact(
            project_id=project.id,
            kind="story_state",
            version=1,
            relative_path=stored_state.relative_path,
            sha256=stored_state.sha256,
            media_type=stored_state.media_type,
            byte_size=stored_state.byte_size,
            source_artifact_ids_json=[],
            generation_config_json={},
        )
        session.add(state_artifact)
        session.flush()
        project.current_story_state_artifact_id = state_artifact.id

        run = models.Run(project_id=project.id, kind="continuation", status="completed", stage="done")
        session.add(run)
        session.flush()

        chapter_id = str(uuid4())
        stored_text = store.put_text("雨停了。\n\n林岚抬起完好的左臂。")
        session.add(
            models.Artifact(
                project_id=project.id,
                run_id=run.id,
                kind="chapter_final",
                version=1,
                relative_path=stored_text.relative_path,
                sha256=stored_text.sha256,
                media_type=stored_text.media_type,
                byte_size=stored_text.byte_size,
                source_artifact_ids_json=[],
                generation_config_json={"chapter_id": chapter_id, "chapter_number": 1, "title": "雨停"},
            )
        )

        report = {
            "chapter_id": chapter_id,
            "issues": [
                {
                    "issue_id": "ISS-1",
                    "severity": "major",
                    "dimension": "canon_world_rules",
                    "title": "伤势消失",
                    "description": "骨折无治疗恢复",
                    "start_offset": 5,
                    "end_offset": 13,
                    "canon_fact_ids": ["canon-injury", "missing-canon"],
                    "suspense_ids": ["mystery-watch", "missing-hook"],
                }
            ],
        }
        stored_report = store.put_json(report)
        session.add(
            models.Artifact(
                project_id=project.id,
                run_id=run.id,
                kind="review_report",
                version=1,
                relative_path=stored_report.relative_path,
                sha256=stored_report.sha256,
                media_type=stored_report.media_type,
                byte_size=stored_report.byte_size,
                source_artifact_ids_json=[],
                generation_config_json={},
            )
        )

    detail = ViewBuilder(factory, store).run_detail(run.id)
    issue = detail["review_issues"][0]

    assert issue["chapter_id"] == chapter_id
    assert issue["start_offset"] == 5
    assert issue["end_offset"] == 13
    assert issue["references"] == [
        {"id": "canon-injury", "kind": "canon", "title": "林岚左臂骨折", "detail": "伤势"},
        {"id": "mystery-watch", "kind": "suspense", "title": "坏掉的怀表", "detail": "待揭示"},
    ]
