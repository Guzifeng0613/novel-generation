"""SQLAlchemy setup with SQLite durability and concurrency pragmas."""

from __future__ import annotations

from collections.abc import Generator, Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

from .config import get_settings


class Base(DeclarativeBase):
    pass


def _sqlite_connect_args(database_url: str) -> dict[str, Any]:
    if not database_url.startswith("sqlite"):
        return {}
    return {"check_same_thread": False, "timeout": 30}


def _install_sqlite_pragmas(db_engine: Engine) -> None:
    if db_engine.dialect.name != "sqlite":
        return

    @event.listens_for(db_engine, "connect")
    def set_sqlite_pragmas(dbapi_connection: Any, _connection_record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=30000")
        finally:
            cursor.close()


def build_engine(database_url: str | None = None, *, echo: bool = False) -> Engine:
    """Create an engine and attach the pragmas to every SQLite connection."""

    url = database_url or get_settings().database_url
    options: dict[str, Any] = {
        "connect_args": _sqlite_connect_args(url),
        "echo": echo,
        "future": True,
        "pool_pre_ping": True,
    }
    if url in {"sqlite://", "sqlite:///:memory:"}:
        options["poolclass"] = StaticPool
    db_engine = create_engine(url, **options)
    _install_sqlite_pragmas(db_engine)
    return db_engine


_settings = get_settings()
_settings.ensure_directories()
engine = build_engine(_settings.database_url)
SessionLocal = sessionmaker(bind=engine, class_=Session, expire_on_commit=False, autoflush=False)


def init_db(db_engine: Engine | None = None) -> None:
    """Create tables for a new local installation.

    Importing models here avoids a circular import while guaranteeing the
    metadata has been populated before ``create_all`` runs. Alembic can use the
    same ``Base.metadata`` for future migrations.
    """

    from . import models as _models  # noqa: F401

    Base.metadata.create_all(bind=db_engine or engine)


def make_session_factory(db_engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=db_engine, class_=Session, expire_on_commit=False, autoflush=False)


def get_db() -> Generator[Session, None, None]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@contextmanager
def session_scope(factory: sessionmaker[Session] = SessionLocal) -> Iterator[Session]:
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
