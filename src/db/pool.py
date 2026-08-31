"""Pools de conexion.

Dos pools separados a proposito:
  - `async_pool`: lo usa la API (FastAPI/uvicorn, asincrono).
  - `sync_pool`:  lo usa el hilo escritor del worker, que es sincrono y hace
                  COPY por lotes. Aislarlo evita que una consulta larga de la
                  UI compita por la conexion que necesita el ingest.
"""

from __future__ import annotations

import logging

from psycopg_pool import AsyncConnectionPool, ConnectionPool

from ..config import settings

log = logging.getLogger(__name__)

_async_pool: AsyncConnectionPool | None = None
_sync_pool: ConnectionPool | None = None


async def open_async_pool() -> AsyncConnectionPool:
    global _async_pool
    if _async_pool is None:
        _async_pool = AsyncConnectionPool(
            settings.database_url,
            min_size=settings.pool_min,
            max_size=settings.pool_max,
            open=False,
            timeout=10,
        )
        await _async_pool.open(wait=True, timeout=30)
        log.info("pool async abierto (min=%d max=%d)", settings.pool_min, settings.pool_max)
    return _async_pool


async def close_async_pool() -> None:
    global _async_pool
    if _async_pool is not None:
        await _async_pool.close()
        _async_pool = None


def async_pool() -> AsyncConnectionPool:
    if _async_pool is None:
        raise RuntimeError("pool async no inicializado")
    return _async_pool


def open_sync_pool() -> ConnectionPool:
    global _sync_pool
    if _sync_pool is None:
        _sync_pool = ConnectionPool(
            settings.database_url,
            min_size=1,
            max_size=3,
            open=True,
            timeout=10,
        )
        log.info("pool sync abierto (worker)")
    return _sync_pool


def close_sync_pool() -> None:
    global _sync_pool
    if _sync_pool is not None:
        _sync_pool.close()
        _sync_pool = None
