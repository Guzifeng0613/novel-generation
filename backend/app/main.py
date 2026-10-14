"""Application factory and local production entry point."""

from __future__ import annotations

import inspect
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from . import orchestrator as orchestration_module
from .api import register_exception_handlers, router
from .config import Settings, get_settings
from .database import SessionLocal, engine, init_db
from .events import event_broker as fallback_event_broker
from .orchestrator import SqlAlchemyOrchestrationRepository
from .presenters import ViewBuilder
from .prompting import PromptStore
from .storage import ArtifactStore

logger = logging.getLogger(__name__)


class IdleRuntime:
    """Fallback used only when the orchestration runtime is unavailable."""

    event_broker = fallback_event_broker

    async def start(self) -> None:
        logger.warning("OrchestratorRuntime is unavailable; queued runs will remain pending")

    async def stop(self) -> None:
        return None

    async def enqueue(self, _run_id: Any) -> None:
        return None


async def _lifecycle_call(target: Any, method: str) -> None:
    callback = getattr(target, method, None)
    if callback is None:
        return
    result = callback()
    if inspect.isawaitable(result):
        await result


def _default_runtime(
    *,
    settings: Settings,
    session_factory: sessionmaker[Session],
    artifact_store: ArtifactStore,
    repository: SqlAlchemyOrchestrationRepository,
) -> Any:
    factory = getattr(orchestration_module, "create_runtime", None)
    if factory is None:
        return IdleRuntime()

    available = {
        "settings": settings,
        "session_factory": session_factory,
        "artifact_store": artifact_store,
        "repository": repository,
    }
    signature = inspect.signature(factory)
    kwargs = {name: available[name] for name in signature.parameters if name in available}
    if "worker_count" in signature.parameters:
        try:
            kwargs["worker_count"] = max(1, int(os.getenv("NOVEL_PROVIDER_CONCURRENCY", "1")))
        except ValueError:
            logger.warning("NOVEL_PROVIDER_CONCURRENCY is invalid; using one worker")
            kwargs["worker_count"] = 1
    return factory(**kwargs)


def _bound_engine(factory: sessionmaker[Session]) -> Engine | None:
    candidate = factory.kw.get("bind")
    return candidate if isinstance(candidate, Engine) else None


def create_app(
    *,
    settings: Settings | None = None,
    db_engine: Engine | None = None,
    session_factory: sessionmaker[Session] | None = None,
    artifact_store: ArtifactStore | None = None,
    runtime: Any | None = None,
    event_broker: Any | None = None,
    frontend_dist: Path | None = None,
) -> FastAPI:
    """Create an app with injectable persistence/runtime dependencies for tests."""

    resolved_settings = settings or get_settings()
    resolved_settings.ensure_directories()
    resolved_factory = session_factory or SessionLocal
    resolved_engine = db_engine or _bound_engine(resolved_factory) or engine
    resolved_store = artifact_store or ArtifactStore(resolved_settings.artifact_dir)
    repository = SqlAlchemyOrchestrationRepository(resolved_factory, resolved_store)
    resolved_runtime = runtime or _default_runtime(
        settings=resolved_settings,
        session_factory=resolved_factory,
        artifact_store=resolved_store,
        repository=repository,
    )
    resolved_broker = (
        event_broker
        or getattr(resolved_runtime, "event_broker", None)
        or getattr(resolved_runtime, "events", None)
        or getattr(resolved_runtime, "broker", None)
        or fallback_event_broker
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        init_db(resolved_engine)
        await _lifecycle_call(resolved_runtime, "start")
        try:
            yield
        finally:
            await _lifecycle_call(resolved_runtime, "stop")
            resolved_engine.dispose()

    app = FastAPI(
        title=resolved_settings.app_name,
        version="0.1.0",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )
    app.state.settings = resolved_settings
    app.state.session_factory = resolved_factory
    app.state.artifact_store = resolved_store
    app.state.repository = repository
    app.state.views = ViewBuilder(resolved_factory, resolved_store)
    app.state.runtime = resolved_runtime
    app.state.event_broker = resolved_broker
    app.state.prompt_store = getattr(resolved_runtime, "prompts", None) or PromptStore()

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://127.0.0.1:5173",
            "http://localhost:5173",
            "http://127.0.0.1:4173",
            "http://localhost:4173",
        ],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(router)
    register_exception_handlers(app)

    dist = (frontend_dist or Path(__file__).resolve().parents[2] / "frontend" / "dist").resolve()
    index_path = dist / "index.html"
    if index_path.is_file():
        assets_path = dist / "assets"
        if assets_path.is_dir():
            app.mount("/assets", StaticFiles(directory=assets_path), name="frontend-assets")

        @app.get("/{full_path:path}", include_in_schema=False)
        async def frontend(request: Request, full_path: str) -> FileResponse:
            if full_path == "api" or full_path.startswith("api/"):
                raise HTTPException(status_code=404, detail="API route not found")
            candidate = (dist / full_path).resolve()
            try:
                candidate.relative_to(dist)
            except ValueError:
                candidate = index_path
            if candidate.is_file():
                return FileResponse(candidate)
            accept = request.headers.get("accept", "")
            if "text/html" not in accept and full_path and "." in Path(full_path).name:
                raise HTTPException(status_code=404, detail="Static file not found")
            return FileResponse(index_path)

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    current = get_settings()
    uvicorn.run("backend.app.main:app", host=current.host, port=current.port, reload=False)
