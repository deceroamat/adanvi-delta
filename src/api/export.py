"""Exportacion CSV de la ventana visible.

Coste casi nulo y valor alto en planta: la gente se lleva el dato a Excel.
Se exporta en la misma resolucion que se esta viendo, no el crudo completo.
"""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse

from ..config import settings
from ..db import repo_tags
from ..db.pool import async_pool
from ..db.repo_history import fetch_series
from ..timeparse import choose_layer, clamp_max_points, format_bucket
from .history import parse_tag_ids, resolve_range

router = APIRouter()


@router.get("/api/export.csv")
async def export_csv(
    tags: str = Query(...),
    window: str | None = Query(None),
    frm: datetime | None = Query(None, alias="from"),
    to: datetime | None = Query(None),
    max_points: int | None = Query(None),
):
    tag_ids = parse_tag_ids(tags)
    start, end = resolve_range(window, frm, to)
    span_s = (end - start).total_seconds()
    age_s = (datetime.now(UTC) - start).total_seconds()
    layer, bucket_s = choose_layer(
        span_s, age_s, clamp_max_points(max_points), n_tags=len(tag_ids)
    )

    pool = async_pool()
    series = await fetch_series(pool, tag_ids, layer, bucket_s, start, end)
    all_tags = {t["id"]: t for t in await repo_tags.list_tags(pool)}

    # Malla comun: union ordenada de todos los timestamps presentes.
    grid = sorted({ts for data in series.values() for ts in data["ts"]})
    index = {ts: i for i, ts in enumerate(grid)}
    columns: list[list[float | None]] = []
    for tag_id in tag_ids:
        column: list[float | None] = [None] * len(grid)
        data = series.get(tag_id, {"ts": [], "avg": []})
        for ts, value in zip(data["ts"], data["avg"], strict=False):
            column[index[ts]] = value
        columns.append(column)

    tz = ZoneInfo(settings.tz)

    def rows():
        buffer = io.StringIO()
        writer = csv.writer(buffer)

        def flush() -> str:
            chunk = buffer.getvalue()
            buffer.seek(0)
            buffer.truncate(0)
            return chunk

        headers = ["timestamp"]
        for tag_id in tag_ids:
            tag = all_tags.get(tag_id)
            name = tag["name"] if tag else str(tag_id)
            unit = (tag or {}).get("unit")
            headers.append(f"{name} [{unit}]" if unit else name)
        writer.writerow(headers)
        writer.writerow([f"# resolucion: {format_bucket(bucket_s)} (capa {layer.name})"])
        yield flush()

        for i, ts in enumerate(grid):
            local = datetime.fromtimestamp(ts, tz=tz)
            writer.writerow([local.isoformat(timespec="seconds"), *(c[i] for c in columns)])
            if i % 500 == 0:
                yield flush()
        yield flush()

    stamp = datetime.now(tz).strftime("%Y%m%d-%H%M%S")
    return StreamingResponse(
        rows(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="adanvi-{stamp}.csv"'},
    )
