from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any


@dataclass(slots=True)
class RunEvent:
    run_id: str
    event: str
    data: dict[str, Any]
    occurred_at: str

    @classmethod
    def create(cls, run_id: str, event: str, data: dict[str, Any] | None = None) -> RunEvent:
        return cls(
            run_id=run_id,
            event=event,
            data=data or {},
            occurred_at=datetime.now(UTC).isoformat(),
        )

    def as_sse(self) -> str:
        return f"event: {self.event}\ndata: {json.dumps(asdict(self), ensure_ascii=False)}\n\n"


class EventBroker:
    """In-memory fan-out for live UI updates; durable truth remains in SQLite."""

    def __init__(self, queue_size: int = 256) -> None:
        self._queue_size = queue_size
        self._subscribers: dict[str, set[asyncio.Queue[RunEvent]]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def publish(self, event: RunEvent) -> None:
        async with self._lock:
            subscribers = tuple(self._subscribers.get(event.run_id, ()))
        for queue in subscribers:
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(event)

    async def subscribe(self, run_id: str) -> AsyncIterator[str]:
        queue: asyncio.Queue[RunEvent] = asyncio.Queue(maxsize=self._queue_size)
        async with self._lock:
            self._subscribers[run_id].add(queue)
        try:
            yield RunEvent.create(run_id, "connected", {"message": "事件流已连接"}).as_sse()
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                    yield event.as_sse()
                except TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            async with self._lock:
                self._subscribers[run_id].discard(queue)
                if not self._subscribers[run_id]:
                    self._subscribers.pop(run_id, None)


event_broker = EventBroker()
