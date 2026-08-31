"""Hilo de adquisicion: malla temporal sin deriva sobre `monotonic()`.

Nunca toca la base de datos. Todo lo que debe persistirse sale por `write_q`, y
todo lo que debe verse en vivo sale por el hub de WebSockets.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import UTC, datetime

from ..config import settings
from ..plc_client import PlcClient
from .broadcaster import hub
from .registry import registry
from .status import status
from .writer import write_q

log = logging.getLogger(__name__)


class Acquirer(threading.Thread):
    def __init__(self, stop_event: threading.Event) -> None:
        super().__init__(name="adanvi-acquirer", daemon=True)
        self._stop = stop_event
        self._plc = PlcClient(settings.plc_ip)
        self._retry_at = 0.0
        self._gap_open = False
        self._reported_types: dict[int, str] = {}

    def run(self) -> None:
        status.set_alive(True)
        interval = settings.read_interval_s
        next_tick = time.monotonic()
        try:
            while not self._stop.is_set():
                next_tick += interval
                scheduled = next_tick
                try:
                    self._cycle()
                except Exception as exc:
                    log.exception("error no esperado en el ciclo de adquisicion")
                    status.record_error(f"acquirer: {exc}")
                    self._fail_cycle(datetime.now(UTC), str(exc))

                slack = scheduled - time.monotonic()
                if slack > 0:
                    self._stop.wait(slack)
                else:
                    # Vamos atrasados: se resincroniza la malla en vez de
                    # disparar una rafaga de ciclos para "recuperar".
                    missed = int(-slack // interval) + 1
                    next_tick += missed * interval
                    log.warning("ciclo atrasado %.0f ms; malla resincronizada", -slack * 1000)
        finally:
            status.set_alive(False)
            self._plc.close()

    # --- un ciclo -----------------------------------------------------

    def _cycle(self) -> None:
        tags = registry.get()
        ts = datetime.now(UTC)  # antes del read, como en el prototipo

        if not tags:
            status.record_cycle(ts, 0, 0.0)
            return

        if not self._plc.connected:
            if time.monotonic() < self._retry_at:
                return
            if not self._plc.connect():
                self._retry_at = time.monotonic() + self._plc.backoff_s()
                self._fail_cycle(ts, "PLC no alcanzable")
                return

        names = [t.name for t in tags]
        started = time.monotonic()
        try:
            results = self._plc.read_many(names)
        except Exception as exc:
            self._plc.close()
            self._retry_at = time.monotonic() + self._plc.backoff_s()
            self._fail_cycle(ts, f"lectura fallida: {exc}")
            return

        readings: list[tuple[int, float | None, int]] = []
        for tag, res in zip(tags, results, strict=False):
            readings.append((tag.id, res.value, res.status))
            if res.value_type and self._reported_types.get(tag.id) != res.value_type:
                self._reported_types[tag.id] = res.value_type
                write_q.put(("value_type", tag.id, res.value_type))

        self._on_success(ts)
        write_q.put(("rows", ts, readings))
        status.update_live(ts, readings)
        hub.publish_tick(ts, readings)
        status.record_cycle(ts, len(readings), (time.monotonic() - started) * 1000)

    # --- transiciones de estado ---------------------------------------

    def _on_success(self, ts: datetime) -> None:
        if self._gap_open:
            self._gap_open = False
            write_q.put(("gap_close", ts))
            hub.publish_event("gap_close", ts)
        status.set_plc_connected(True)
        status.record_error(None)

    def _fail_cycle(self, ts: datetime, reason: str) -> None:
        """No se insertan filas: el hueco se registra una sola vez en gaps.

        Escribir ceros aqui destruiria la autoescala del grafico, envenenaria los
        agregados continuos y seria indistinguible de un cero real del proceso.
        """
        status.set_plc_connected(False)
        status.record_error(reason)
        if not self._gap_open:
            self._gap_open = True
            write_q.put(("gap_open", ts, "plc_disconnected"))
            hub.publish_event("gap_open", ts)
