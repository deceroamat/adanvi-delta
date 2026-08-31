"""Galerias de tendencias y configuracion de sus series."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from psycopg import errors
from pydantic import BaseModel, Field, field_validator

from ..config import settings
from ..db import repo_galleries
from ..db.pool import async_pool

router = APIRouter()


class GalleryCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    description: str | None = None

    @field_validator("name")
    @classmethod
    def _strip(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("el nombre no puede estar vacio")
        return cleaned


class GalleryPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=120)
    description: str | None = None


class SeriesItem(BaseModel):
    tag_id: int
    visible: bool = True
    color: str = Field("#3987e5", pattern="^#[0-9a-fA-F]{6}$")
    # Series con el mismo grupo comparten escala Y. 'auto' agrupa por unidad.
    axis_group: str = Field("auto", max_length=40)
    scale_mode: str = Field("auto", pattern="^(auto|manual)$")
    y_min: float | None = None
    y_max: float | None = None
    unit_override: str | None = Field(None, max_length=20)
    decimals: int | None = Field(None, ge=0, le=6)
    interp: str = Field("auto", pattern="^(auto|linear|step)$")
    line_width: int = Field(2, ge=1, le=5)
    agg: str = Field("avg", pattern="^(avg|min|max|last)$")

    @field_validator("y_max")
    @classmethod
    def _range(cls, value: float | None, info):
        y_min = info.data.get("y_min")
        if value is not None and y_min is not None and value <= y_min:
            raise ValueError("y_max debe ser mayor que y_min")
        return value


class SeriesReplace(BaseModel):
    series: list[SeriesItem] = Field(default_factory=list, max_length=30)

    @field_validator("series")
    @classmethod
    def _unique_tags(cls, value: list[SeriesItem]) -> list[SeriesItem]:
        seen = {item.tag_id for item in value}
        if len(seen) != len(value):
            raise ValueError("hay tags repetidos en la galeria")
        return value


@router.get("/api/galleries")
async def list_galleries():
    pool = async_pool()
    return {
        "galleries": await repo_galleries.list_galleries(pool),
        "max_galleries": settings.max_galleries,
    }


@router.post("/api/galleries", status_code=201)
async def create_gallery(payload: GalleryCreate):
    pool = async_pool()
    limit = settings.max_galleries
    if limit and await repo_galleries.count_galleries(pool) >= limit:
        raise HTTPException(409, f"limite de {limit} galerias alcanzado")
    try:
        return await repo_galleries.create_gallery(pool, payload.name, payload.description)
    except errors.UniqueViolation:
        raise HTTPException(409, f"ya existe una galeria llamada '{payload.name}'") from None


@router.get("/api/galleries/{gallery_id}")
async def get_gallery(gallery_id: int):
    gallery = await repo_galleries.get_gallery(async_pool(), gallery_id)
    if gallery is None:
        raise HTTPException(404, "galeria no encontrada")
    return gallery


@router.patch("/api/galleries/{gallery_id}")
async def patch_gallery(gallery_id: int, payload: GalleryPatch):
    data = payload.model_dump(exclude_unset=True)
    try:
        row = await repo_galleries.update_gallery(async_pool(), gallery_id, data)
    except errors.UniqueViolation:
        raise HTTPException(409, "ya existe una galeria con ese nombre") from None
    if row is None:
        raise HTTPException(404, "galeria no encontrada")
    return row


@router.delete("/api/galleries/{gallery_id}")
async def delete_gallery(gallery_id: int):
    if not await repo_galleries.delete_gallery(async_pool(), gallery_id):
        raise HTTPException(404, "galeria no encontrada")
    return {"deleted": True}


@router.put("/api/galleries/{gallery_id}/series")
async def put_series(gallery_id: int, payload: SeriesReplace):
    pool = async_pool()
    if await repo_galleries.get_gallery(pool, gallery_id) is None:
        raise HTTPException(404, "galeria no encontrada")
    try:
        await repo_galleries.replace_series(
            pool, gallery_id, [item.model_dump() for item in payload.series]
        )
    except errors.ForeignKeyViolation:
        raise HTTPException(400, "alguna serie referencia un tag inexistente") from None
    return await repo_galleries.get_gallery(pool, gallery_id)
