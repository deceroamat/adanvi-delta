"""CRUD de tags. El worker solo lee de aqui: no hay YAML ni CSV."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from psycopg import errors
from pydantic import BaseModel, Field, field_validator, model_validator

from ..config import settings
from ..db import repo_tags
from ..db.pool import async_pool
from ..plc_client import AREAS, BIT_AREAS, DATA_TYPES
from ..worker.status import status

router = APIRouter()

_AREA_RE = "^(" + "|".join(AREAS) + ")$"
_TYPE_RE = "^(" + "|".join(DATA_TYPES) + ")$"


def _check_area_tipo(area: str | None, data_type: str | None) -> None:
    """Espeja el CHECK de la migracion 008 con un mensaje entendible.

    La duplicacion es la convencion del proyecto: el CHECK protege la tabla de
    cualquier escritura y Pydantic evita que el operador reciba un 500 de psycopg
    cuando lo unico que pasa es que eligio mal el area.
    """
    if area is None or data_type is None:
        return
    if area in BIT_AREAS and data_type != "bit":
        raise ValueError(f"el area '{area}' es de bits: el tipo debe ser 'bit', no '{data_type}'")
    if area not in BIT_AREAS and data_type == "bit":
        raise ValueError(f"el tipo 'bit' necesita un area de bits (coil/discrete), no '{area}'")


class TagCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    label: str | None = None
    unit: str | None = Field(None, max_length=20)
    decimals: int = Field(2, ge=0, le=6)
    kind: str = Field("analog", pattern="^(analog|digital|counter)$")
    active: bool = True

    # --- direccionamiento Modbus ---
    unit_id: int = Field(default_factory=lambda: settings.plc_unit_id, ge=0, le=247)
    area: str = Field("holding", pattern=_AREA_RE)
    address: int = Field(..., ge=0, le=65535)
    data_type: str = Field("int16", pattern=_TYPE_RE)
    word_order: str = Field("big", pattern="^(big|little)$")
    scale: float = 1.0
    value_offset: float = 0.0

    @field_validator("name")
    @classmethod
    def _strip(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("el nombre no puede estar vacio")
        return cleaned

    @field_validator("scale")
    @classmethod
    def _scale(cls, value: float) -> float:
        if value == 0:
            raise ValueError("la escala no puede ser 0: anularia la lectura")
        return value

    @model_validator(mode="after")
    def _coherentes(self):
        _check_area_tipo(self.area, self.data_type)
        return self


class TagPatch(BaseModel):
    label: str | None = None
    unit: str | None = Field(None, max_length=20)
    decimals: int | None = Field(None, ge=0, le=6)
    kind: str | None = Field(None, pattern="^(analog|digital|counter)$")
    active: bool | None = None

    unit_id: int | None = Field(None, ge=0, le=247)
    area: str | None = Field(None, pattern=_AREA_RE)
    address: int | None = Field(None, ge=0, le=65535)
    data_type: str | None = Field(None, pattern=_TYPE_RE)
    word_order: str | None = Field(None, pattern="^(big|little)$")
    scale: float | None = None
    value_offset: float | None = None

    @field_validator("scale")
    @classmethod
    def _scale(cls, value: float | None) -> float | None:
        if value == 0:
            raise ValueError("la escala no puede ser 0: anularia la lectura")
        return value

    @model_validator(mode="after")
    def _coherentes(self):
        # En un PATCH parcial solo se puede comprobar si vienen los dos campos;
        # el CHECK de la tabla cubre el resto de los casos.
        _check_area_tipo(self.area, self.data_type)
        return self


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
