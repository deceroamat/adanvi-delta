"""Salud del worker y estado vivo de los tags."""

from __future__ import annotations

from fastapi import APIRouter, Query

from ..api.history import parse_tag_ids
from ..worker.broadcaster import hub
from ..worker.status import status

router = APIRouter()


@router.get("/api/health")
async def health():
    data = status.health()
    data["ws_subscribers"] = hub.subscriber_count
    # Degradado si el worker vive pero el PLC no responde; caido si no hay ciclos.
    stale = data["seconds_since_last_cycle"]
    if not data["worker_alive"] or stale is None or stale > 10:
        data["state"] = "down"
    elif not data["plc_connected"]:
        data["state"] = "degraded"
    else:
        data["state"] = "ok"
    return data


@router.get("/api/live/snapshot")
async def live_snapshot(tags: str | None = Query(None)):
    """Ultimos valores en memoria: la tabla se pinta sin esperar al primer tick."""
    tag_ids = parse_tag_ids(tags) if tags else None
    return {"readings": status.snapshot(tag_ids)}
