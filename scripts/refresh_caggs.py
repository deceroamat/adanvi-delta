"""Rellena huecos de materializacion en los agregados continuos.

    uv run python scripts/refresh_caggs.py --from '2026-08-16 15:00' --to '2026-08-17 13:00'
    uv run python scripts/refresh_caggs.py --from '2026-08-16' --to '2026-08-18' --dry-run

Los agregados se refrescan solos por policy, pero con `start_offset => 30 minutes`
(migracion 003): si el planificador de jobs deja de correr mas de media hora, lo que
no llego a materializarse se queda asi para siempre. La agregacion en tiempo real
(`materialized_only = false`) tampoco lo tapa, porque solo cubre la cola POSTERIOR al
watermark, no los agujeros que quedan detras. En pantalla el sintoma es una tendencia
que salta horas enteras en las ventanas largas (que leen los agregados) mientras en
las cortas (que leen el crudo) los datos estan.

CUIDADO — por que el rango se acota siempre al crudo existente: refrescar un tramo
donde ya no hay filas en `readings` BORRA los buckets materializados de ese tramo.
Los agregados sobreviven a la retencion del crudo justamente porque nadie los
recalcula (`readings_1h` guarda 5 anos y el crudo 90 dias), asi que un `--from`
demasiado atras destruiria historico real. El tope no es una comodidad: es la unica
barrera que hay.

Es idempotente: volver a ejecutarlo sobre un rango ya materializado no cambia nada.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg
from psycopg import sql

from src.config import settings

# Vista -> tamano de bucket. El rango de refresco se alinea a estos limites; si no,
# Timescale lo alinea por su cuenta y avisa por NOTICE.
CAGGS = {
    "readings_1m": timedelta(minutes=1),
    "readings_1h": timedelta(hours=1),
}


def parse_instant(text: str, tz: ZoneInfo) -> datetime:
    """Acepta '2026-08-16', '2026-08-16 15:00' o ISO completo."""
    try:
        value = datetime.fromisoformat(text.strip())
    except ValueError:
        raise SystemExit(f"fecha no valida: '{text}' (usa 'YYYY-MM-DD HH:MM')") from None
    return value if value.tzinfo else value.replace(tzinfo=tz)


def floor_to(moment: datetime, bucket: timedelta) -> datetime:
    seconds = int(bucket.total_seconds())
    return datetime.fromtimestamp((moment.timestamp() // seconds) * seconds, tz=moment.tzinfo)


def ceil_to(moment: datetime, bucket: timedelta) -> datetime:
    seconds = int(bucket.total_seconds())
    return datetime.fromtimestamp(-(-moment.timestamp() // seconds) * seconds, tz=moment.tzinfo)


def raw_bounds(cur) -> tuple[datetime | None, datetime | None]:
    cur.execute("SELECT min(ts), max(ts) FROM readings")
    return cur.fetchone()


def missing_buckets(cur, view: str, bucket: timedelta, start: datetime, end: datetime) -> int:
    """Buckets que existen en crudo y no en el agregado, dentro del rango."""
    query = sql.SQL(
        "WITH crudo AS ("
        "  SELECT time_bucket(%s, ts) AS b FROM readings"
        "  WHERE ts >= %s AND ts < %s GROUP BY 1"
        "), agg AS ("
        "  SELECT DISTINCT bucket AS b FROM {view} WHERE bucket >= %s AND bucket < %s"
        ") "
        "SELECT count(*) FROM crudo c LEFT JOIN agg a USING (b) WHERE a.b IS NULL"
    ).format(view=sql.Identifier(view))
    cur.execute(query, (bucket, start, end, start, end))
    return cur.fetchone()[0]


def main() -> int:
    tz = ZoneInfo(settings.tz)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="desde", required=True, help="inicio del rango")
    parser.add_argument("--to", dest="hasta", required=True, help="fin del rango")
    parser.add_argument(
        "--cagg",
        choices=sorted(CAGGS),
        action="append",
        help="limitar a una vista (por defecto, las dos)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="solo informar de los huecos, sin refrescar nada",
    )
    args = parser.parse_args()

    desde = parse_instant(args.desde, tz)
    hasta = parse_instant(args.hasta, tz)
    if hasta <= desde:
        raise SystemExit("'--to' debe ser posterior a '--from'")

    views = args.cagg or sorted(CAGGS)

    # autocommit obligatorio: refresh_continuous_aggregate no puede correr dentro
    # de una transaccion.
    with psycopg.connect(settings.database_url, autocommit=True) as conn, conn.cursor() as cur:
        # SET no admite parametros; set_config si, y deja los datetimes que
        # devuelve la base ya en la zona local para los mensajes.
        cur.execute("SELECT set_config('timezone', %s, false)", (settings.tz,))
        primero, ultimo = raw_bounds(cur)
        if primero is None:
            raise SystemExit("no hay datos crudos en 'readings': nada que materializar")

        # El tope que evita borrar agregado sin crudo detras (ver docstring).
        if desde < primero:
            print(f"aviso: se recorta el inicio a la primera lectura cruda ({primero:%d/%m %H:%M})")
            desde = primero
        if hasta > ultimo:
            hasta = ultimo
        if hasta <= desde:
            raise SystemExit("el rango pedido queda fuera del crudo disponible")

        print(f"rango: {desde:%d/%m/%Y %H:%M} -> {hasta:%d/%m/%Y %H:%M} ({settings.tz})")

        for view in views:
            bucket = CAGGS[view]
            inicio = floor_to(desde, bucket)
            fin = ceil_to(hasta, bucket)

            antes = missing_buckets(cur, view, bucket, inicio, fin)
            if antes == 0:
                print(f"  {view}: nada que rellenar")
                continue
            if args.dry_run:
                print(f"  {view}: {antes} buckets sin materializar (dry-run, no se toca)")
                continue

            t0 = time.perf_counter()
            cur.execute(
                "CALL refresh_continuous_aggregate(%s::regclass, %s, %s)", (view, inicio, fin)
            )
            elapsed = time.perf_counter() - t0
            despues = missing_buckets(cur, view, bucket, inicio, fin)
            print(f"  {view}: {antes} -> {despues} buckets sin materializar ({elapsed:.2f}s)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
