"""Punto de entrada: migraciones -> hilos del worker -> servidor HTTP."""

from __future__ import annotations

import logging
import threading

import uvicorn

from .config import settings
from .db.migrate import run_migrations, wait_for_db
from .db.pool import close_sync_pool
from .worker.acquirer import Acquirer
from .worker.writer import Writer

log = logging.getLogger("adanvi")


def setup_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("pymodbus").setLevel(logging.WARNING)


def main() -> None:
    setup_logging()
    log.info("ADANVI by emolog — arrancando")

    wait_for_db()
    run_migrations()

    stop = threading.Event()
    writer = Writer(stop)
    writer.start()

    if settings.plc_ip:
        acquirer = Acquirer(stop)
        acquirer.start()
    else:
        acquirer = None
        log.warning("PLC_IP sin configurar: la adquisicion queda desactivada")

    from .app import app

    try:
        uvicorn.run(
            app,
            host="0.0.0.0",
            port=settings.http_port,
            log_level=settings.log_level.lower(),
            access_log=False,
            ws_ping_interval=20,
            ws_ping_timeout=20,
        )
    finally:
        log.info("deteniendo worker...")
        stop.set()
        if acquirer is not None:
            acquirer.join(timeout=5)
        writer.join(timeout=10)
        close_sync_pool()
        log.info("ADANVI detenido")


if __name__ == "__main__":
    main()
