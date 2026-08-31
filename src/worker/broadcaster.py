"""Puente entre el hilo del worker (sincrono) y los WebSockets (asyncio).

Cada conexion tiene su propia cola acotada con politica drop-oldest: una pestana
lenta o una red mala degradan solo a ese cliente, nunca al ingest ni al resto.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from datetime import datetime
from typing import Any

log = logging.getLogger(__name__)

QUEUE_MAXSIZE = 50


class Subscriber:
    """Una conexion WebSocket con su seleccion de tags."""

    __slots__ = ("dropped", "queue", "tag_ids")

    def __init__(self) -> None:
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
        self.tag_ids: list[int] = []
        self.dropped = 0

    def offer(self, message: dict[str, Any]) -> None:
        """Encola descartando lo mas viejo si esta llena. Nunca bloquea."""
        if self.queue.full():
            try:
                self.queue.get_nowait()
                self.dropped += 1
            except asyncio.QueueEmpty:
                pass
        try:
            self.queue.put_nowait(message)
        except asyncio.QueueFull:  # pragma: no cover - carrera improbable
            self.dropped += 1


class LiveHub:
    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._subs: set[Subscriber] = set()
        self._lock = threading.Lock()

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def add(self, sub: Subscriber) -> None:
        with self._lock:
            self._subs.add(sub)

    def remove(self, sub: Subscriber) -> None:
        with self._lock:
            self._subs.discard(sub)

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)

    # --- publicacion desde el hilo del worker -------------------------

    def publish_tick(
        self, ts: datetime, readings: list[tuple[int, float | None, int]]
    ) -> None:
        by_tag = {tag_id: (value, st) for tag_id, value, st in readings}
        self._dispatch(lambda sub: _project_tick(sub, ts, by_tag))

    def publish_event(self, event: str, ts: datetime) -> None:
        payload = {"type": event, "ts": ts.timestamp()}
        self._dispatch(lambda _sub: payload)

    def _dispatch(self, build) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        with self._lock:
            subs = list(self._subs)
        if not subs:
            return
        # RuntimeError = el loop se esta cerrando; el mensaje simplemente se pierde.
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(_deliver, subs, build)


def _deliver(subs: list[Subscriber], build) -> None:
    for sub in subs:
        message = build(sub)
        if message is not None:
            sub.offer(message)


def _project_tick(
    sub: Subscriber,
    ts: datetime,
    by_tag: dict[int, tuple[float | None, int]],
) -> dict[str, Any] | None:
    """Formato columnar alineado a la suscripcion del cliente.

    Un objeto por lectura costaria ~5x mas payload y obligaria al cliente a
    reordenar; asi se hace append directo sobre los arrays de uPlot.
    """
    if not sub.tag_ids:
        return None
    values: list[float | None] = []
    statuses: list[int] = []
    hit = False
    for tag_id in sub.tag_ids:
        entry = by_tag.get(tag_id)
        if entry is None:
            values.append(None)
            statuses.append(2)
        else:
            hit = True
            values.append(entry[0])
            statuses.append(entry[1])
    if not hit:
        return None
    return {"type": "tick", "ts": ts.timestamp(), "values": values, "status": statuses}


hub = LiveHub()
