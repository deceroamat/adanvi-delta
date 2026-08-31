"""Formularios de captura manual.

Solo el de operacion por ahora; laboratorio e ingenieria vendran despues sobre
esta misma estructura.

Los rangos viven aqui y estan duplicados como CHECK en la migracion 006. La
duplicacion es deliberada: el CHECK protege la tabla de cualquier escritura, y
el modelo Pydantic convierte el fallo en un mensaje que el operador entiende en
vez de un 500 de psycopg.
"""

from __future__ import annotations

import csv
import io
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from psycopg import errors
from pydantic import BaseModel, Field, field_validator

from ..config import settings
from ..db import repo_forms
from ..db.pool import async_pool

router = APIRouter()

ZONES = 10
REEL_TYPES = ("x1", "x2")
# Referencias de produccion conocidas, de menor a mayor gramaje para que el
# operador las recorra sin buscar. No es una lista cerrada: el formulario deja
# teclear una fuera de lista, asi que no hay CHECK contra estos valores.
REFERENCES = ("K40", "K42", "K45", "K48", "K50", "K60", "K90", "K100", "K111")
REFERENCE_MAX = 20
SPEED_MIN, SPEED_MAX = 0.0, 600.0
GSM_MIN, GSM_MAX = 10.0, 120.0
REEL_WEIGHT_MIN, REEL_WEIGHT_MAX = 0.0, 5000.0

MAX_ROWS = 500


def _local_tz() -> ZoneInfo:
    return ZoneInfo(settings.tz)


def _clean_reference(value: str) -> str:
    # Normalizada a mayusculas: sin esto "k40", "K40 " y "K40" se cuentan como
    # tres referencias distintas al agrupar para el analisis.
    cleaned = value.strip().upper()
    if not cleaned:
        raise ValueError("la referencia no puede estar vacia")
    return cleaned


def _check_profile(values: list[float]) -> list[float]:
    # Se nombra la zona que falla: "esta mal" no le sirve a quien tiene diez
    # casillas iguales delante.
    for index, value in enumerate(values, start=1):
        if not GSM_MIN <= value <= GSM_MAX:
            raise ValueError(f"la zona {index} debe estar entre {GSM_MIN:.0f} y {GSM_MAX:.0f} g/m2")
    return values


class OpRecordIn(BaseModel):
    reference: str = Field(..., min_length=1, max_length=REFERENCE_MAX)
    consecutive: str = Field(..., min_length=1, max_length=40)
    shift_date: date
    start_time: time
    end_time: time
    machine_speed: float = Field(..., ge=SPEED_MIN, le=SPEED_MAX)
    weight_profile: list[float] = Field(..., min_length=ZONES, max_length=ZONES)
    base_weight: float = Field(..., ge=GSM_MIN, le=GSM_MAX)
    reel_weight: float = Field(..., ge=REEL_WEIGHT_MIN, le=REEL_WEIGHT_MAX)
    breaks: int = Field(..., ge=0, le=5)
    reel_type: str = Field(..., pattern="^(x1|x2)$")

    @field_validator("consecutive")
    @classmethod
    def _strip(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("el consecutivo no puede estar vacio")
        return cleaned

    @field_validator("reference")
    @classmethod
    def _reference(cls, value: str) -> str:
        return _clean_reference(value)

    @field_validator("weight_profile")
    @classmethod
    def _profile(cls, value: list[float]) -> list[float]:
        return _check_profile(value)


class OpRecordPatch(BaseModel):
    """Todo opcional: la UI manda solo lo que cambio."""

    reference: str | None = Field(None, min_length=1, max_length=REFERENCE_MAX)
    consecutive: str | None = Field(None, min_length=1, max_length=40)
    shift_date: date | None = None
    start_time: time | None = None
    end_time: time | None = None
    machine_speed: float | None = Field(None, ge=SPEED_MIN, le=SPEED_MAX)
    weight_profile: list[float] | None = Field(None, min_length=ZONES, max_length=ZONES)
    base_weight: float | None = Field(None, ge=GSM_MIN, le=GSM_MAX)
    reel_weight: float | None = Field(None, ge=REEL_WEIGHT_MIN, le=REEL_WEIGHT_MAX)
    breaks: int | None = Field(None, ge=0, le=5)
    reel_type: str | None = Field(None, pattern="^(x1|x2)$")

    @field_validator("consecutive")
    @classmethod
    def _strip(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("el consecutivo no puede estar vacio")
        return cleaned

    @field_validator("reference")
    @classmethod
    def _reference(cls, value: str) -> str:
        return _clean_reference(value)

    @field_validator("weight_profile")
    @classmethod
    def _profile(cls, value: list[float]) -> list[float]:
        return _check_profile(value)


def _to_timestamps(day: date, start: time, end: time) -> tuple[datetime, datetime]:
    """Compone los instantes reales a partir de la fecha del dia y dos horas.

    La UI solo pide HH:MM. Si la hora de fin no es posterior a la de inicio, la
    bobina cruzo la medianoche y el fin cae el dia siguiente.
    """
    tz = _local_tz()
    started = datetime.combine(day, start, tzinfo=tz)
    ended = datetime.combine(day, end, tzinfo=tz)
    if ended <= started:
        ended += timedelta(days=1)
    return started, ended


def _editable_until(row: dict[str, Any]) -> datetime:
    # Se cuenta desde `created_at`, nunca desde `updated_at`: corregir no puede
    # reiniciar el reloj, o la ventana no cierra jamas.
    return row["created_at"] + timedelta(minutes=settings.op_edit_window_min)


def _decorate(row: dict[str, Any]) -> dict[str, Any]:
    until = _editable_until(row)
    row["editable_until"] = until
    row["editable"] = datetime.now(UTC) < until
    # Las horas ya formateadas en la zona del servidor. Si las derivara el
    # navegador, un panel con la zona horaria mal puesta mostraria —y al editar,
    # guardaria— horas corridas.
    tz = _local_tz()
    row["start_time"] = row["started_at"].astimezone(tz).strftime("%H:%M")
    row["end_time"] = row["ended_at"].astimezone(tz).strftime("%H:%M")
    return row


def _assert_editable(row: dict[str, Any], force: bool) -> None:
    if force:
        return
    if datetime.now(UTC) >= _editable_until(row):
        raise HTTPException(
            409,
            f"el registro ya no se puede modificar: pasaron mas de "
            f"{settings.op_edit_window_min} minutos desde que se guardo",
        )


def _resolve_times(payload: dict[str, Any], current: dict[str, Any] | None = None) -> None:
    """Cambia `shift_date`/`start_time`/`end_time` por `started_at`/`ended_at`.

    En un PATCH parcial hay que recomponer con lo que ya tiene la fila: cambiar
    solo la hora de fin no puede perder la fecha.
    """
    keys = ("shift_date", "start_time", "end_time")
    if not any(key in payload for key in keys):
        return

    if current is None:
        day, start, end = (payload[key] for key in keys)
    else:
        tz = _local_tz()
        day = payload.get("shift_date", current["shift_date"])
        start = payload.get("start_time", current["started_at"].astimezone(tz).time())
        end = payload.get("end_time", current["ended_at"].astimezone(tz).time())

    payload.pop("start_time", None)
    payload.pop("end_time", None)
    payload["shift_date"] = day
    payload["started_at"], payload["ended_at"] = _to_timestamps(day, start, end)


@router.get("/api/forms/operation")
async def list_operation(
    day: date | None = Query(None, alias="date"),
    frm: datetime | None = Query(None, alias="from"),
    to: datetime | None = Query(None),
    limit: int = Query(MAX_ROWS, ge=1, le=MAX_ROWS),
):
    # Sin filtro explicito se muestra el dia de hoy: es lo que el operador
    # necesita ver el 99% de las veces.
    if day is None and frm is None and to is None:
        day = datetime.now(_local_tz()).date()

    rows = await repo_forms.list_records(async_pool(), shift_date=day, frm=frm, to=to, limit=limit)
    return {
        "records": [_decorate(row) for row in rows],
        "edit_window_min": settings.op_edit_window_min,
        "reel_types": list(REEL_TYPES),
        "references": list(REFERENCES),
    }


@router.post("/api/forms/operation", status_code=201)
async def create_operation(payload: OpRecordIn):
    data = payload.model_dump()
    _resolve_times(data)
    try:
        row = await repo_forms.create_record(async_pool(), data)
    except errors.UniqueViolation:
        raise HTTPException(
            409, f"ya existe un registro con el consecutivo '{payload.consecutive}'"
        ) from None
    return _decorate(row)


@router.patch("/api/forms/operation/{record_id}")
async def patch_operation(
    record_id: int,
    payload: OpRecordPatch,
    force: bool = Query(
        False,
        description=(
            "Correccion de ingenieria fuera de la ventana de edicion. "
            "Queda registrada como tal en op_record_revisions."
        ),
    ),
):
    pool = async_pool()
    current = await repo_forms.get_record(pool, record_id)
    if current is None:
        raise HTTPException(404, "registro no encontrado")
    _assert_editable(current, force)

    data = payload.model_dump(exclude_unset=True)
    _resolve_times(data, current)
    # Sin roles todavia, `force` es solo un parametro. Cuando existan, esto pasa
    # a ser una comprobacion de permiso y el parametro desaparece.
    source = "ingenieria" if force else "operador"
    try:
        row = await repo_forms.update_record(pool, record_id, data, source)
    except errors.UniqueViolation:
        raise HTTPException(409, "ya existe un registro con ese consecutivo") from None
    if row is None:
        raise HTTPException(404, "registro no encontrado")
    return _decorate(row)


@router.delete("/api/forms/operation/{record_id}")
async def delete_operation(record_id: int, force: bool = Query(False)):
    pool = async_pool()
    current = await repo_forms.get_record(pool, record_id)
    if current is None:
        raise HTTPException(404, "registro no encontrado")
    _assert_editable(current, force)

    if not await repo_forms.delete_record(pool, record_id):
        raise HTTPException(404, "registro no encontrado")
    return {"deleted": True}


@router.get("/api/forms/operation.csv")
async def export_operation_csv(
    day: date | None = Query(None, alias="date"),
    frm: datetime | None = Query(None, alias="from"),
    to: datetime | None = Query(None),
):
    rows = await repo_forms.list_records(
        async_pool(), shift_date=day, frm=frm, to=to, limit=MAX_ROWS
    )
    tz = _local_tz()

    def stream():
        buffer = io.StringIO()
        writer = csv.writer(buffer)

        def flush() -> str:
            chunk = buffer.getvalue()
            buffer.seek(0)
            buffer.truncate(0)
            return chunk

        writer.writerow(
            [
                "referencia",
                "consecutivo",
                "fecha",
                "hora_inicio",
                "hora_fin",
                "velocidad_m_min",
                *(f"zona_{i}_gm2" for i in range(1, ZONES + 1)),
                "peso_base_gm2",
                "peso_bobina_kg",
                "rupturas",
                "tipo_bobina",
                "registrado_en",
                "corregido_en",
            ]
        )
        yield flush()

        # Orden ascendente en el archivo: se lee como el cuaderno del turno,
        # aunque en pantalla lo mas reciente vaya arriba.
        for i, row in enumerate(reversed(rows)):
            started = row["started_at"].astimezone(tz)
            ended = row["ended_at"].astimezone(tz)
            writer.writerow(
                [
                    row["reference"] or "",
                    row["consecutive"],
                    row["shift_date"].isoformat(),
                    started.strftime("%H:%M"),
                    ended.strftime("%H:%M"),
                    row["machine_speed"],
                    *row["weight_profile"],
                    row["base_weight"],
                    row["reel_weight"],
                    row["breaks"],
                    row["reel_type"],
                    row["created_at"].astimezone(tz).isoformat(timespec="seconds"),
                    row["updated_at"].astimezone(tz).isoformat(timespec="seconds"),
                ]
            )
            if i % 200 == 0:
                yield flush()
        yield flush()

    stamp = datetime.now(tz).strftime("%Y%m%d-%H%M%S")
    return StreamingResponse(
        stream(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="operacion-{stamp}.csv"'},
    )
