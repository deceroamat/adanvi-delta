"""Genera datos sinteticos para validar la app sin un PLC delante.

    uv run python scripts/seed.py --days 35 --tags 12

Crea tags realistas (temperaturas, presiones, rpm, booleanos), llena `readings`
por COPY y registra un par de huecos de adquisicion para poder comprobar que la
banda "SIN DATO" se pinta bien.
"""

from __future__ import annotations

import argparse
import math
import random
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg

from src.config import settings

PROFILES = [
    ("Temp_Zona{n}", "Temperatura zona {n}", "°C", "analog", 175.0, 6.0, 1),
    ("Presion_L{n}", "Presión línea {n}", "bar", "analog", 4.6, 0.5, 2),
    ("Vel_Motor{n}", "Velocidad motor {n}", "rpm", "analog", 1450.0, 40.0, 0),
    ("Flujo_{n}", "Flujo {n}", "l/min", "analog", 88.0, 7.0, 1),
    ("Bomba{n}_ON", "Bomba {n} en marcha", None, "digital", 0.0, 0.0, 0),
]


def build_tags(conn, count: int) -> list[tuple[int, str, float, float, str]]:
    created = []
    for i in range(count):
        name_tpl, label_tpl, unit, kind, base, noise, decimals = PROFILES[i % len(PROFILES)]
        n = i // len(PROFILES) + 1
        name = name_tpl.format(n=n)
        row = conn.execute(
            "INSERT INTO tags (name, label, unit, decimals, kind, value_type) "
            "VALUES (%s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (name) DO UPDATE SET label = EXCLUDED.label "
            "RETURNING id",
            (
                name,
                label_tpl.format(n=n),
                unit,
                decimals,
                kind,
                "BOOL" if kind == "digital" else "REAL",
            ),
        ).fetchone()
        created.append((row[0], name, base, noise, kind))
    conn.commit()
    return created


def generate(conn, tags, days: int, interval_s: int) -> None:
    end = datetime.now(UTC).replace(microsecond=0)
    start = end - timedelta(days=days)
    total_steps = int((end - start).total_seconds() // interval_s)

    # Dos huecos: uno largo hace 3 dias y uno corto hace 6 horas.
    outages = [
        (end - timedelta(days=3), end - timedelta(days=3) + timedelta(minutes=40)),
        (end - timedelta(hours=6), end - timedelta(hours=6) + timedelta(minutes=4)),
    ]
    conn.execute("DELETE FROM acquisition_gaps")
    for gap_start, gap_end in outages:
        conn.execute(
            "INSERT INTO acquisition_gaps (started_at, ended_at, reason, detail) "
            "VALUES (%s, %s, 'plc_disconnected', 'datos sinteticos')",
            (gap_start, gap_end),
        )
    conn.commit()

    print(f"generando {total_steps * len(tags):,} filas ({days} días, {len(tags)} tags)…")

    phases = [random.random() * math.tau for _ in tags]
    written = 0
    with conn.cursor() as cur, cur.copy(
        "COPY readings (ts, tag_id, value, status) FROM STDIN"
    ) as copy:
        for step in range(total_steps):
            ts = start + timedelta(seconds=step * interval_s)
            if any(a <= ts < b for a, b in outages):
                continue  # durante un hueco NO se escriben filas
            for (tag_id, _name, base, noise, kind), phase in zip(tags, phases, strict=True):
                if kind == "digital":
                    value = float((step // 900 + tag_id) % 3 > 0)
                else:
                    slow = math.sin(step / 3600.0 + phase) * noise
                    fast = math.sin(step / 47.0 + phase) * noise * 0.25
                    spike = noise * 3 if random.random() < 0.00002 else 0.0
                    value = base + slow + fast + spike + random.gauss(0, noise * 0.08)
                copy.write_row((ts, tag_id, round(value, 4), 0))
                written += 1
            if step % 20000 == 0 and step:
                print(f"  {written:,} filas…")
    conn.commit()
    print(f"listo: {written:,} filas escritas")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=35)
    parser.add_argument("--tags", type=int, default=12)
    parser.add_argument(
        "--interval",
        type=int,
        default=1,
        help="segundos entre muestras; súbelo a 5 o 10 para sembrar más rápido",
    )
    args = parser.parse_args()

    with psycopg.connect(settings.database_url) as conn:
        tags = build_tags(conn, args.tags)
        generate(conn, tags, args.days, args.interval)

        print("refrescando agregados continuos…")
        conn.autocommit = True
        for view in ("readings_1m", "readings_1h"):
            conn.execute(f"CALL refresh_continuous_aggregate('{view}', NULL, NULL)")
        print("hecho")


if __name__ == "__main__":
    main()
