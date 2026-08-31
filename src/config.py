"""Configuracion por entorno. Un solo lugar donde se leen variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    plc_ip: str = os.getenv("PLC_IP", "")
    read_interval_ms: int = _int("READ_INTERVAL_MS", 1000)

    database_url: str = os.getenv(
        "DATABASE_URL", "postgresql://adanvi:adanvi@localhost:5432/adanvi"
    )
    pool_min: int = _int("DB_POOL_MIN", 2)
    pool_max: int = _int("DB_POOL_MAX", 8)

    http_port: int = _int("HTTP_PORT", 8000)
    log_level: str = os.getenv("LOG_LEVEL", "INFO").upper()
    tz: str = os.getenv("TZ", "America/Bogota")

    # 0 = sin tope.
    max_galleries: int = _int("MAX_GALLERIES", 0)

    # Minutos que el operador tiene para corregir una fila del formulario de
    # operacion despues de guardarla. Pasado ese plazo la fila queda bloqueada.
    op_edit_window_min: int = _int("OP_EDIT_WINDOW_MIN", 30)

    # Cada cuanto el worker recarga la lista de tags activos desde BD.
    tag_reload_s: int = _int("TAG_RELOAD_S", 15)
    # Cada cuanto se persiste tags.last_seen_ts (no cada ciclo: 100 UPDATE/s
    # sobre una tabla pequena genera bloat permanente).
    last_seen_flush_s: int = _int("LAST_SEEN_FLUSH_S", 30)

    migrations_dir: Path = ROOT / "migrations"
    static_dir: Path = Path(__file__).resolve().parent / "static"

    @property
    def read_interval_s(self) -> float:
        return self.read_interval_ms / 1000.0


settings = Settings()
