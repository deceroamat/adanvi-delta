"""Formularios de captura manual.

Por ahora solo el de operacion (`op_records`). Cada correccion deja una imagen
previa en `op_record_revisions`: sin eso, quien analice los datos no puede
distinguir un valor escrito de un valor corregido tres semanas despues.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from psycopg import sql
from psycopg.rows import dict_row

# Orden canonico de columnas. Se usa para el INSERT y para decidir que se puede
# actualizar: `id`, `created_at` y `updated_at` no estan y por tanto no se tocan.
RECORD_FIELDS = (
    "reference",
    "consecutive",
    "shift_date",
    "started_at",
    "ended_at",
    "machine_speed",
    "weight_profile",
    "base_weight",
    "reel_weight",
    "breaks",
    "reel_type",
)

_SELECT = (
    "SELECT id, reference, consecutive, shift_date, started_at, ended_at, machine_speed, "
    "       weight_profile, base_weight, reel_weight, breaks, reel_type, "
    "       created_at, updated_at "
    "FROM op_records "
)


async def list_records(
    pool,
    shift_date: date | None = None,
    frm: datetime | None = None,
    to: datetime | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    where: list[str] = []
    params: list[Any] = []
    if shift_date is not None:
        where.append("shift_date = %s")
        params.append(shift_date)
    if frm is not None:
        where.append("started_at >= %s")
        params.append(frm)
    if to is not None:
        where.append("started_at < %s")
        params.append(to)

    query = _SELECT
    if where:
        query += "WHERE " + " AND ".join(where) + " "
    query += "ORDER BY started_at DESC, id DESC LIMIT %s"
    params.append(limit)

    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(query, params)
        return await cur.fetchall()


async def get_record(pool, record_id: int) -> dict[str, Any] | None:
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(_SELECT + "WHERE id = %s", (record_id,))
        return await cur.fetchone()


async def create_record(pool, data: dict[str, Any]) -> dict[str, Any]:
    insert = sql.SQL("INSERT INTO op_records ({cols}) VALUES ({vals}) RETURNING *").format(
        cols=sql.SQL(", ").join(sql.Identifier(name) for name in RECORD_FIELDS),
        vals=sql.SQL(", ").join(sql.Placeholder() * len(RECORD_FIELDS)),
    )
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(insert, [data[name] for name in RECORD_FIELDS])
            row = await cur.fetchone()
        await conn.commit()
    return row


async def update_record(
    pool, record_id: int, data: dict[str, Any], source: str
) -> dict[str, Any] | None:
    """Actualiza y archiva la imagen previa en la misma transaccion."""
    fields = {k: v for k, v in data.items() if k in RECORD_FIELDS}
    if not fields:
        return await get_record(pool, record_id)

    assignments = sql.SQL(", ").join(
        sql.SQL("{} = %s").format(sql.Identifier(name)) for name in fields
    )
    query = sql.SQL(
        "UPDATE op_records SET {assignments}, updated_at = now() WHERE id = %s RETURNING *"
    ).format(assignments=assignments)

    async with pool.connection() as conn, conn.transaction():
        # Se archiva antes de tocar la fila; si el UPDATE falla, la transaccion
        # se deshace entera y no queda una revision huerfana.
        cur = await conn.execute(
            "INSERT INTO op_record_revisions (record_id, before, source) "
            "SELECT id, to_jsonb(r), %s FROM op_records r WHERE id = %s",
            (source, record_id),
        )
        if cur.rowcount == 0:
            return None

        async with conn.cursor(row_factory=dict_row) as cur2:
            await cur2.execute(query, [*fields.values(), record_id])
            return await cur2.fetchone()


async def delete_record(pool, record_id: int) -> bool:
    async with pool.connection() as conn:
        cur = await conn.execute("DELETE FROM op_records WHERE id = %s", (record_id,))
        await conn.commit()
        return cur.rowcount > 0
