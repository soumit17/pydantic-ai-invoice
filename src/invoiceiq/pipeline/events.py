"""In-process pub/sub of pipeline progress, for the SSE endpoint.

Each invoice gets an event history plus live subscribers. A subscriber that connects late
replays the history first, so `curl -N .../stream` sees the whole run whenever it connects.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

Event = dict[str, Any]
DONE = "done"


class EventBus:
    def __init__(self) -> None:
        self._history: dict[str, list[Event]] = {}
        self._subscribers: dict[str, list[asyncio.Queue[Event]]] = {}

    def open(self, invoice_id: str) -> None:
        self._history[invoice_id] = []

    def known(self, invoice_id: str) -> bool:
        return invoice_id in self._history

    def publish(self, invoice_id: str, event: Event) -> None:
        self._history.setdefault(invoice_id, []).append(event)
        for queue in self._subscribers.get(invoice_id, []):
            queue.put_nowait(event)

    def close(self, invoice_id: str, **fields: Any) -> None:
        self.publish(invoice_id, {"type": DONE, **fields})

    async def subscribe(self, invoice_id: str) -> AsyncIterator[Event]:
        queue: asyncio.Queue[Event] = asyncio.Queue()
        history = list(self._history.get(invoice_id, []))
        self._subscribers.setdefault(invoice_id, []).append(queue)
        try:
            for event in history:
                yield event
                if event["type"] == DONE:
                    return
            while True:
                event = await queue.get()
                yield event
                if event["type"] == DONE:
                    return
        finally:
            self._subscribers[invoice_id].remove(queue)
