from sqlalchemy import text

from backend.app.database import Base, build_engine, make_session_factory
from backend.app.models import Attempt, Project, Run, Task


def test_sqlite_wal_foreign_keys_and_core_relations(tmp_path) -> None:
    database_path = tmp_path / "test.sqlite3"
    engine = build_engine(f"sqlite:///{database_path.as_posix()}")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)

    with engine.connect() as connection:
        assert connection.execute(text("PRAGMA journal_mode")).scalar_one().lower() == "wal"
        assert connection.execute(text("PRAGMA foreign_keys")).scalar_one() == 1

    with factory() as session:
        project = Project(name="无名城", data_path="projects/p1")
        run = Run(project=project, kind="continuation", target_chars=40_000)
        task = Task(run=run, operation="plan", logical_key="plan:v1")
        task.attempts.append(Attempt(attempt_no=1))
        session.add(project)
        session.commit()
        assert session.get(Project, project.id) is not None
        assert task.run_id == run.id
        assert task.attempts[0].task_id == task.id
