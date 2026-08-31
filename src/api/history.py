"""GET /api/history — el endpoint del que depende toda la visualizacion."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Query

from ..db.pool import async_pool
from ..db.repo_history import fetch_gaps, fetch_series
from ..timeparse import (
    WindowError,
    choose_layer,
    clamp_max_points,
    format_bucket,
    parse_window,
)

router = APIRouter()


def parse_tag_ids(raw: str) -> list[int]:
    ids: list[int] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            ids.append(int(chunk))
        except ValueError:
            raise HTTPException(400, f"tag id invalido: '{chunk}'") from None
    if not ids:
        raise HTTPException(400, "se requiere al menos un tag")
    if len(ids) > 50:
        raise HTTPException(400, "maximo 50 tags por consulta")
    return ids


def _as_utc(value: datetime) -> datetime:
    """Un `datetime-local` del navegador llega sin zona; se asume UTC."""
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def resolve_range(
    window: str | None, frm: datetime | None, to: datetime | None
) -> tuple[datetime, datetime]:
    now = datetime.now(UTC)
    frm = _as_utc(frm) if frm else None
    to = _as_utc(to) if to else None
    if window:
        try:
            span = parse_window(window)
        except WindowError as exc:
            raise HTTPException(400, str(exc)) from None
        end = to or now
        return end - span, end
    if frm is None or to is None:
        raise HTTPException(400, "se requiere 'window' o el par 'from'/'to'")
    if to <= frm:
        raise HTTPException(400, "'to' debe ser posterior a 'from'")
    return frm, to


@router.get("/api/history")
async def get_history(
    tags: str = Query(..., description="ids de tag separados por coma"),
    window: str | None = Query(None, description="p.ej. 30s, 15m, 1h, 1d, 2w, 1M"),
    frm: datetime | None = Query(None, alias="from"),
    to: datetime | None = Query(None),
    max_points: int | None = Query(None, description="puntos objetivo por serie"),
):
    tag_ids = parse_tag_ids(tags)
    start, end = resolve_range(window, frm, to)

    span_s = (end - start).total_seconds()
    age_s = (datetime.now(UTC) - start).total_seconds()
    points = clamp_max_points(max_points)
    layer, bucket_s = choose_layer(span_s, age_s, points, n_tags=len(tag_ids))

    # Se alinean los bordes a multiplos del bucket para que el pan reutilice los
    # tramos ya cacheados en el cliente en vez de pedirlos de nuevo desplazados.
    aligned_start = datetime.fromtimestamp(
        (start.timestamp() // bucket_s) * bucket_s, tz=UTC
    )
    aligned_end = datetime.fromtimestamp(
        -(-end.timestamp() // bucket_s) * bucket_s, tz=UTC
    )

    pool = async_pool()
    series = await fetch_series(pool, tag_ids, layer, bucket_s, aligned_start, aligned_end)
    gaps = await fetch_gaps(pool, aligned_start, aligned_end)

    return {
        "from": aligned_start.isoformat(),
        "to": aligned_end.isoformat(),
        "requested_from": start.isoformat(),
        "requested_to": end.isoformat(),
        "layer": layer.name,
        "bucket_s": bucket_s,
        "resolution": format_bucket(bucket_s),
        "aggregated": bucket_s > layer.bucket_s or layer.is_cagg,
        "max_points": points,
        "series": [
            {
                "tag_id": tag_id,
                "ts": data["ts"],
                "avg": data["avg"],
                "min": data["min"],
                "max": data["max"],
                "status": data["status"],
            }
            for tag_id, data in series.items()
        ],
        "gaps": gaps,
    }


@router.get("/api/history/window")
async def check_window(w: str):
    """Valida una ventana sin consultar datos. Lo usa la UI para dar feedback."""
    try:
        span = parse_window(w)
    except WindowError as exc:
        raise HTTPException(400, str(exc)) from None
    return {"window": w, "seconds": int(span.total_seconds())}
