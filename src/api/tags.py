"""CRUD de tags. El worker solo lee de aqui: no hay YAML ni CSV."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from psycopg import errors
from pydantic import BaseModel, Field, field_validator

from ..db import repo_tags
from ..db.pool import async_pool
from ..worker.status import status

router = APIRouter()


class TagCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    label: str | None = None
    unit: str | None = Field(None, max_length=20)
    decimals: int = Field(2, ge=0, le=6)
    kind: str = Field("analog", pattern="^(analog|digital|counter)$")
    active: bool = True

    @field_validator("name")
    @classmethod
    def _strip(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("el nombre no puede estar vacio")
        return cleaned


class TagPatch(BaseModel):
    label: str | None = None
    unit: str | None = Field(None, max_length=20)
    decimals: int | None = Field(None, ge=0, le=6)
    kind: str | None = Field(None, pattern="^(analog|digital|counter)$")
    active: bool | None = None


@router.get("/api/tags")
async def list_tags(active: bool = False):
    rows = await repo_tags.list_tags(async_pool(), only_active=active)
    live = {item["tag_id"]: item for item in status.snapshot()}
    for row in rows:
        current = live.get(row["id"])
        row["last_value"] = current["value"] if current else None
        row["last_status"] = current["status_name"] if current else "Pending"
    return rows


@router.post("/api/tags", status_code=201)
async def create_tag(payload: TagCreate):
    try:
        return await repo_tags.create_tag(async_pool(), payload.model_dump())
    except errors.UniqueViolation:
        raise HTTPException(409, f"ya existe un tag llamado '{payload.name}'") from None


@router.patch("/api/tags/{tag_id}")
async def patch_tag(tag_id: int, payload: TagPatch):
    data = payload.model_dump(exclude_unset=True)
    row = await repo_tags.update_tag(async_pool(), tag_id, data)
    if row is None:
        raise HTTPException(404, "tag no encontrado")
    return row


@router.delete("/api/tags/{tag_id}")
async def delete_tag(
    tag_id: int,
    purge: bool = Query(
        False,
        description="true borra tambien el historico; por defecto solo desactiva",
    ),
):
    pool = async_pool()
    if purge:
        if not await repo_tags.delete_tag(pool, tag_id):
            raise HTTPException(404, "tag no encontrado")
        return {"deleted": True, "history_purged": True}
    if not await repo_tags.deactivate_tag(pool, tag_id):
        raise HTTPException(404, "tag no encontrado")
    return {"deleted": False, "deactivated": True, "history_purged": False}
