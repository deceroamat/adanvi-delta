"""Runner de migraciones.

SQL numerado en `migrations/`, aplicado en orden y registrado en
`schema_migrations`. Se descarto Alembic a proposito: el DDL de Timescale
(hypertables, agregados continuos, policies) es SQL puro y el autogenerador
no sabe representarlo.
"""

from __future__ import annotations

import hashlib
import logging
import time

import psycopg

from ..config import settings

log = logging.getLogger(__name__)

_BOOTSTRAP = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     TEXT PRIMARY KEY,
    checksum    TEXT NOT NULL,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def wait_for_db(timeout_s: float = 60.0) -> None:
    """El healthcheck de compose cubre el arranque normal; esto cubre el resto."""
    deadline = time.monotonic() + timeout_s
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with psycopg.connect(settings.database_url, connect_timeout=5) as conn:
                conn.execute("SELECT 1")
            return
        except Exception as exc:
            last = exc
            time.sleep(1.0)
    raise RuntimeError(f"base de datos no disponible tras {timeout_s:.0f}s: {last}")


def run_migrations() -> None:
    files = sorted(settings.migrations_dir.glob("*.sql"))
    if not files:
        raise RuntimeError(f"no hay migraciones en {settings.migrations_dir}")

    with psycopg.connect(settings.database_url, autocommit=False) as conn:
        conn.execute(_BOOTSTRAP)
        conn.commit()

        rows = conn.execute("SELECT version, checksum FROM schema_migrations").fetchall()
        applied = {version: checksum for version, checksum in rows}

        for path in files:
            version = path.stem
            sql = path.read_text(encoding="utf-8")
            checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()[:16]

            if version in applied:
                if applied[version] != checksum:
                    log.warning(
                        "migracion %s cambio tras aplicarse (checksum %s -> %s); "
                        "no se reaplica",
                        version,
                        applied[version],
                        checksum,
                    )
                continue

            log.info("aplicando migracion %s", version)
            started = time.monotonic()
            try:
                # psycopg3 admite varias sentencias en un execute sin parametros,
                # lo que evita partir el archivo por ';' y romper los bloques DO $$.
                conn.execute(sql)
                conn.execute(
                    "INSERT INTO schema_migrations (version, checksum) VALUES (%s, %s)",
                    (version, checksum),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                log.exception("fallo la migracion %s", version)
                raise
            log.info("migracion %s aplicada en %.2fs", version, time.monotonic() - started)
