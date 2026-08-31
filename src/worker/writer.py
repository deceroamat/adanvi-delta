"""Hilo escritor: es el unico que toca la base de datos desde el worker.

Separarlo del hilo de adquisicion es lo que impide que un checkpoint de Postgres
o un job de compresion de Timescale desvie la cadencia de lectura del PLC.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from datetime import UTC, datetime, timedelta

from ..config import settings
from ..db.pool import open_sync_pool
from .registry import TagDef, registry
from .status import status

log = logging.getLogger(__name__)

# Cada mensaje "rows" es un ciclo completo. 300 mensajes ~ 5 min de buffer si la
# base se pone lenta; pasado eso se descarta lo mas viejo antes que crecer sin fin.
WRITE_QUEUE_MAXSIZE = 300
BATCH_MAX_ROWS = 2000
BATCH_MAX_SECONDS = 1.0


class WriteQueue:
    """Cola acotada con drop-oldest y contador de descartes."""

    def __init__(self, maxsize: int = WRITE_QUEUE_MAXSIZE) -> None:
        self._q: queue.Queue = queue.Queue(maxsize=maxsize)
        self.dropped = 0
        self._last_warn = 0.0

    def put(self, item: tuple) -> None:
        try:
            self._q.put_nowait(item)
            return
        except queue.Full:
            pass
        try:
            self._q.get_nowait()
            self.dropped += 1
            now = time.monotonic()
            if now - self._last_warn > 30:
                self._last_warn = now
                log.warning(
                    "cola de escritura llena; descartados %d mensajes acumulados",
                    self.dropped,
                )
        except queue.Empty:  # pragma: no cover
            pass
        try:
            self._q.put_nowait(item)
        except queue.Full:  # pragma: no cover
            self.dropped += 1

    def get(self, timeout: float):
        return self._q.get(timeout=timeout)

    def qsize(self) -> int:
        return self._q.qsize()


write_q = WriteQueue()


class Writer(threading.Thread):
    def __init__(self, stop_event: threading.Event) -> None:
        super().__init__(name="adanvi-writer", daemon=True)
        self._stop = stop_event
        self._pending: list[tuple] = []
        self._pending_since = 0.0
        self._last_seen_flush = 0.0
        self._last_tag_reload = 0.0
        self._polled_ids: set[int] = set()
        # Gap que quedo abierto de un proceso anterior. El acquirer no puede
        # emitir su cierre: su bandera nace en False con cada arranque.
        self._orphan_gap = False

    # --- ciclo de vida ------------------------------------------------

    def run(self) -> None:
        pool = open_sync_pool()
        self._reload_tags(pool)
        self._bootstrap_gap_check(pool)
        while not self._stop.is_set():
            try:
                item = write_q.get(timeout=0.25)
            except queue.Empty:
                item = None

            try:
                if item is not None:
                    self._handle(pool, item)
                self._maybe_flush(pool)
                self._maybe_reload_tags(pool)
                self._maybe_flush_last_seen(pool)
            except Exception as exc:
                log.exception("error en el hilo escritor")
                status.record_error(f"writer: {exc}")
                time.sleep(1.0)

            status.set_write_queue(write_q.qsize(), write_q.dropped)

        self._flush(pool)

    # --- despacho -----------------------------------------------------

    def _handle(self, pool, item: tuple) -> None:
        kind = item[0]
        if kind == "rows":
            _, ts, readings = item
            if not self._pending:
                self._pending_since = time.monotonic()
            for tag_id, value, st in readings:
                self._pending.append((ts, tag_id, value, st))
                self._polled_ids.add(tag_id)
            return

        # Todo lo demas debe respetar el orden relativo a las lecturas.
        self._flush(pool)
        if kind == "gap_open":
            self._open_gap(pool, item[1], item[2])
        elif kind == "gap_close":
            self._close_gap(pool, item[1])
        elif kind == "value_type":
            self._set_value_type(pool, item[1], item[2])

    def _maybe_flush(self, pool) -> None:
        if not self._pending:
            return
        if (
            len(self._pending) >= BATCH_MAX_ROWS
            or time.monotonic() - self._pending_since >= BATCH_MAX_SECONDS
        ):
            self._flush(pool)

    def _flush(self, pool) -> None:
        if not self._pending:
            return
        rows, self._pending = self._pending, []
        with pool.connection() as conn:
            with conn.cursor() as cur, cur.copy(
                "COPY readings (ts, tag_id, value, status) FROM STDIN"
            ) as copy:
                for row in rows:
                    copy.write_row(row)
            # Un lote escrito prueba que la adquisicion volvio. Es lo unico que
            # cierra un gap heredado de otro proceso, que nadie mas reclamaria.
            if self._orphan_gap:
                conn.execute(
                    "UPDATE acquisition_gaps SET ended_at = %s WHERE ended_at IS NULL",
                    (rows[0][0],),
                )
                self._orphan_gap = False
                status.set_gap_open(None)
                log.info("gap heredado cerrado en %s", rows[0][0].isoformat())
            conn.commit()

    # --- huecos de adquisicion ---------------------------------------

    def _open_gap(self, pool, ts: datetime, reason: str) -> None:
        with pool.connection() as conn:
            open_row = conn.execute(
                "SELECT id FROM acquisition_gaps WHERE ended_at IS NULL LIMIT 1"
            ).fetchone()
            if open_row is not None:
                return
            conn.execute(
                "INSERT INTO acquisition_gaps (started_at, reason) VALUES (%s, %s)",
                (ts, reason),
            )
            conn.commit()
        log.warning("gap de adquisicion abierto en %s (%s)", ts.isoformat(), reason)
        status.set_gap_open(ts)

    def _close_gap(self, pool, ts: datetime) -> None:
        with pool.connection() as conn:
            conn.execute(
                "UPDATE acquisition_gaps SET ended_at = %s WHERE ended_at IS NULL",
                (ts,),
            )
            conn.commit()
        log.info("gap de adquisicion cerrado en %s", ts.isoformat())
        self._orphan_gap = False
        status.set_gap_open(None)

    def _bootstrap_gap_check(self, pool) -> None:
        """Cubre reinicios y cortes de energia.

        Un gap que sigue abierto en la base al arrancar es siempre de otro
        proceso, y el acquirer nunca emitira su cierre: su bandera nace en
        False, asi que un ciclo bueno no le parece una transicion. Sin esto el
        gap queda abierto para siempre y, como `fetch_gaps` lee `ended_at IS
        NULL` como `infinity`, la vista entera se pinta de "SIN DATO" sobre
        datos que si existen. Si ya hay lecturas posteriores a su inicio, el
        hueco termino en la primera de ellas; si no las hay sigue en curso, y
        lo cierra el primer lote que se escriba.

        Si no hay gap abierto y la ultima lectura es mas vieja que 3 ciclos, el
        tiempo que la app estuvo caida es un hueco real y debe verse como tal.
        """
        threshold = timedelta(seconds=max(3 * settings.read_interval_s, 5))
        with pool.connection() as conn:
            row = conn.execute("SELECT max(ts) FROM readings").fetchone()
            last_ts = row[0] if row else None
            open_gap = conn.execute(
                "SELECT id, started_at FROM acquisition_gaps WHERE ended_at IS NULL LIMIT 1"
            ).fetchone()

            if open_gap is not None:
                gap_id, started_at = open_gap
                resumed = conn.execute(
                    "SELECT min(ts) FROM readings WHERE ts > %s", (started_at,)
                ).fetchone()[0]
                if resumed is None:
                    self._orphan_gap = True
                    status.set_gap_open(started_at)
                    log.warning("gap %s sigue abierto desde %s", gap_id, started_at.isoformat())
                    return
                conn.execute(
                    "UPDATE acquisition_gaps SET ended_at = %s WHERE id = %s",
                    (resumed, gap_id),
                )
                conn.commit()
                status.set_gap_open(None)
                log.warning(
                    "gap huerfano %s cerrado en %s (quedo abierto tras un reinicio)",
                    gap_id,
                    resumed.isoformat(),
                )
                return

            if last_ts is None:
                return
            if datetime.now(UTC) - last_ts <= threshold:
                return
            conn.execute(
                "INSERT INTO acquisition_gaps (started_at, reason, detail) "
                "VALUES (%s, 'worker_restart', %s)",
                (last_ts, "app detenida entre ciclos"),
            )
            conn.commit()
        log.warning("registrado gap retroactivo desde %s (reinicio)", last_ts.isoformat())
        status.set_gap_open(last_ts)

    # --- catalogo ------------------------------------------------------

    def _maybe_reload_tags(self, pool) -> None:
        now = time.monotonic()
        if now - self._last_tag_reload < settings.tag_reload_s:
            return
        self._reload_tags(pool)

    def _reload_tags(self, pool) -> None:
        self._last_tag_reload = time.monotonic()
        with pool.connection() as conn:
            rows = conn.execute(
                "SELECT id, name FROM tags WHERE active ORDER BY id"
            ).fetchall()
        previous = {t.id for t in registry.get()}
        if registry.set([TagDef(tag_id, name) for tag_id, name in rows]):
            current = {t.id for t in registry.get()}
            status.forget_tags(previous - current)
            log.info("catalogo recargado: %d tags activos", len(rows))

    def _set_value_type(self, pool, tag_id: int, value_type: str) -> None:
        with pool.connection() as conn:
            conn.execute(
                "UPDATE tags SET value_type = %s WHERE id = %s AND "
                "value_type IS DISTINCT FROM %s",
                (value_type, tag_id, value_type),
            )
            conn.commit()

    def _maybe_flush_last_seen(self, pool) -> None:
        now = time.monotonic()
        if now - self._last_seen_flush < settings.last_seen_flush_s:
            return
        self._last_seen_flush = now
        if not self._polled_ids:
            return
        ids, self._polled_ids = list(self._polled_ids), set()
        with pool.connection() as conn:
            conn.execute(
                "UPDATE tags SET last_seen_ts = now() WHERE id = ANY(%s)",
                (ids,),
            )
            conn.commit()
