"""Estado compartido del worker, seguro entre hilos.

Aqui vive tambien el ultimo valor de cada tag. Deliberadamente NO se escribe en
`tags.last_value` cada ciclo: 100 UPDATE/s sobre una tabla pequena produce bloat
permanente y presion de autovacuum sin aportar nada que la memoria no resuelva.
"""

from __future__ import annotations

import threading
from collections import deque
from datetime import UTC, datetime
from typing import Any

from ..constants import STATUS_NAMES


class WorkerStatus:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._worker_alive = False
        self._plc_connected = False
        self._last_cycle_ts: datetime | None = None
        self._polled_tags = 0
        self._last_error: str | None = None
        self._write_q_depth = 0
        self._dropped_writes = 0
        self._gap_open_since: datetime | None = None
        # Ventana de jitter: detecta degradacion de cadencia antes de que se note.
        self._jitter_ms: deque[float] = deque(maxlen=120)
        # tag_id -> (ts, value, status)
        self._live: dict[int, tuple[datetime, float | None, int]] = {}

    # --- escritura (hilo del worker) ---------------------------------

    def set_alive(self, alive: bool) -> None:
        with self._lock:
            self._worker_alive = alive

    def set_plc_connected(self, connected: bool) -> None:
        with self._lock:
            self._plc_connected = connected
            if connected:
                self._gap_open_since = None

    def set_gap_open(self, since: datetime | None) -> None:
        with self._lock:
            self._gap_open_since = since

    def record_cycle(self, ts: datetime, polled: int, jitter_ms: float) -> None:
        with self._lock:
            self._last_cycle_ts = ts
            self._polled_tags = polled
            self._jitter_ms.append(abs(jitter_ms))

    def record_error(self, message: str | None) -> None:
        with self._lock:
            self._last_error = message

    def set_write_queue(self, depth: int, dropped: int) -> None:
        with self._lock:
            self._write_q_depth = depth
            self._dropped_writes = dropped

    def update_live(self, ts: datetime, readings: list[tuple[int, float | None, int]]) -> None:
        with self._lock:
            for tag_id, value, status in readings:
                self._live[tag_id] = (ts, value, status)

    def forget_tags(self, tag_ids: set[int]) -> None:
        with self._lock:
            for tag_id in tag_ids:
                self._live.pop(tag_id, None)

    # --- lectura (hilo de la API) ------------------------------------

    def snapshot(self, tag_ids: list[int] | None = None) -> list[dict[str, Any]]:
        with self._lock:
            items = self._live.items() if tag_ids is None else (
                (tid, self._live[tid]) for tid in tag_ids if tid in self._live
            )
            return [
                {
                    "tag_id": tag_id,
                    "ts": ts.isoformat(),
                    "value": value,
                    "status": status,
                    "status_name": STATUS_NAMES.get(status, "?"),
                }
                for tag_id, (ts, value, status) in items
            ]

    def health(self) -> dict[str, Any]:
        with self._lock:
            jitter = sorted(self._jitter_ms)
            p95 = jitter[int(len(jitter) * 0.95)] if jitter else 0.0
            last_cycle = self._last_cycle_ts
            stale_s = (
                (datetime.now(UTC) - last_cycle).total_seconds()
                if last_cycle
                else None
            )
            return {
                "worker_alive": self._worker_alive,
                "plc_connected": self._plc_connected,
                "last_cycle_ts": last_cycle.isoformat() if last_cycle else None,
                "seconds_since_last_cycle": round(stale_s, 2) if stale_s is not None else None,
                "polled_tags": self._polled_tags,
                "cycle_jitter_ms_p95": round(p95, 1),
                "write_queue_depth": self._write_q_depth,
                "dropped_writes": self._dropped_writes,
                "gap_open_since": (
                    self._gap_open_since.isoformat() if self._gap_open_since else None
                ),
                "last_error": self._last_error,
            }


status = WorkerStatus()
