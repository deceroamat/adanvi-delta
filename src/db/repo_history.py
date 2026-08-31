"""Consultas de historico.

El nombre de tabla se interpola desde `timeparse.LAYERS`, nunca desde entrada del
usuario, y aun asi va por `sql.Identifier`.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from psycopg import sql
from psycopg.rows import tuple_row

from ..timeparse import Layer


def _bucket_expr(layer: Layer, bucket_s: int):
    return sql.SQL("time_bucket({iv}, {col})").format(
        iv=sql.Literal(timedelta(seconds=bucket_s)),
        col=sql.Identifier(layer.time_col),
    )


def _build_query(layer: Layer, bucket_s: int, aggregated: bool):
    table = sql.Identifier(layer.table)
    col = sql.Identifier(layer.time_col)

    if not aggregated and not layer.is_cagg:
        # Crudo sin agregar: solo ocurre cuando el bucket objetivo es 1 s, o sea
        # cuando la ventana ya cabe en el numero de puntos pedido.
        return sql.SQL(
            "SELECT tag_id, {col}, value, value, value, 1, status "
            "FROM {table} "
            "WHERE tag_id = ANY(%s) AND {col} >= %s AND {col} < %s "
            "ORDER BY tag_id, {col}"
        ).format(col=col, table=table)

    if not aggregated and layer.is_cagg:
        return sql.SQL(
            "SELECT tag_id, {col}, avg, min, max, n, 0 "
            "FROM {table} "
            "WHERE tag_id = ANY(%s) AND {col} >= %s AND {col} < %s "
            "ORDER BY tag_id, {col}"
        ).format(col=col, table=table)

    bucket = _bucket_expr(layer, bucket_s)

    if layer.is_cagg:
        # El promedio de promedios debe ponderarse por el conteo de cada bucket,
        # o los buckets incompletos pesarian igual que los llenos.
        return sql.SQL(
            "SELECT tag_id, {bucket} AS b, "
            "       sum(avg * n) / NULLIF(sum(n), 0), min(min), max(max), sum(n), 0 "
            "FROM {table} "
            "WHERE tag_id = ANY(%s) AND {col} >= %s AND {col} < %s "
            "GROUP BY tag_id, b ORDER BY tag_id, b"
        ).format(bucket=bucket, table=table, col=col)

    return sql.SQL(
        "SELECT tag_id, {bucket} AS b, "
        "       avg(value), min(value), max(value), count(value), 0 "
        "FROM {table} "
        "WHERE tag_id = ANY(%s) AND {col} >= %s AND {col} < %s "
        "GROUP BY tag_id, b ORDER BY tag_id, b"
    ).format(bucket=bucket, table=table, col=col)


async def fetch_series(
    pool,
    tag_ids: list[int],
    layer: Layer,
    bucket_s: int,
    start: datetime,
    end: datetime,
) -> dict[int, dict[str, list]]:
    """Devuelve arrays paralelos por tag: ts, avg, min, max, n, status."""
    aggregated = bucket_s > layer.bucket_s
    query = _build_query(layer, bucket_s, aggregated)

    out: dict[int, dict[str, list]] = {
        tag_id: {"ts": [], "avg": [], "min": [], "max": [], "n": [], "status": []}
        for tag_id in tag_ids
    }

    async with pool.connection() as conn, conn.cursor(row_factory=tuple_row) as cur:
        await cur.execute(query, (tag_ids, start, end))
        async for tag_id, bucket, avg, vmin, vmax, n, st in cur:
            series = out.get(tag_id)
            if series is None:
                continue
            series["ts"].append(bucket.timestamp())
            series["avg"].append(float(avg) if avg is not None else None)
            series["min"].append(float(vmin) if vmin is not None else None)
            series["max"].append(float(vmax) if vmax is not None else None)
            series["n"].append(int(n) if n is not None else 0)
            series["status"].append(int(st) if st is not None else 0)
    return out


async def fetch_gaps(pool, start: datetime, end: datetime) -> list[list[float]]:
    """Huecos que solapan la ventana, recortados a ella."""
    async with pool.connection() as conn, conn.cursor(row_factory=tuple_row) as cur:
        await cur.execute(
            "SELECT started_at, ended_at FROM acquisition_gaps "
            "WHERE started_at < %s AND COALESCE(ended_at, 'infinity'::timestamptz) > %s "
            "ORDER BY started_at",
            (end, start),
        )
        rows = await cur.fetchall()

    gaps: list[list[float]] = []
    for started, ended in rows:
        gap_start = max(started, start)
        gap_end = min(ended, end) if ended is not None else end
        if gap_end > gap_start:
            gaps.append([gap_start.timestamp(), gap_end.timestamp()])
    return gaps
