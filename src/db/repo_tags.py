"""Acceso a la tabla `tags`. Unica fuente de verdad de que se pollea."""

from __future__ import annotations

from typing import Any

from psycopg import sql
from psycopg.rows import dict_row

# Lista fija de columnas. Se interpola en los f-string de abajo; no proviene de
# entrada del usuario en ningun caso (de ahi los `noqa: S608`).
COLUMNS = (
    "id, name, label, unit, decimals, kind, active, "
    "unit_id, area, address, data_type, word_order, scale, value_offset, "
    "last_seen_ts, created_at, updated_at"
)

# Unicos campos que un PATCH puede tocar. update_tag compone los nombres con
# sql.Identifier, asi que una clave fuera de esta lista no puede llegar al SQL.
_PATCHABLE = (
    "label", "unit", "decimals", "kind", "active",
    "unit_id", "area", "address", "data_type", "word_order", "scale", "value_offset",
)


async def list_tags(pool, only_active: bool = False) -> list[dict[str, Any]]:
    where = "WHERE active" if only_active else ""
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(f"SELECT {COLUMNS} FROM tags {where} ORDER BY name")  # noqa: S608
        return await cur.fetchall()


async def get_tag(pool, tag_id: int) -> dict[str, Any] | None:
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(f"SELECT {COLUMNS} FROM tags WHERE id = %s", (tag_id,))  # noqa: S608
        return await cur.fetchone()


async def create_tag(pool, data: dict[str, Any]) -> dict[str, Any]:
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "INSERT INTO tags (name, label, unit, decimals, kind, active, "  # noqa: S608
                "  unit_id, area, address, data_type, word_order, scale, value_offset) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                f"RETURNING {COLUMNS}",
                (
                    data["name"],
                    data.get("label"),
                    data.get("unit"),
                    data.get("decimals", 2),
                    data.get("kind", "analog"),
                    data.get("active", True),
                    data["unit_id"],
                    data["area"],
                    data["address"],
                    data["data_type"],
                    data["word_order"],
                    data["scale"],
                    data["value_offset"],
                ),
            )
            row = await cur.fetchone()
        await conn.commit()
    return row


async def update_tag(pool, tag_id: int, data: dict[str, Any]) -> dict[str, Any] | None:
    fields = {k: v for k, v in data.items() if k in _PATCHABLE}
    if not fields:
        return await get_tag(pool, tag_id)

    assignments = sql.SQL(", ").join(
        sql.SQL("{} = %s").format(sql.Identifier(name)) for name in fields
    )
    query = sql.SQL(
        "UPDATE tags SET {assignments}, updated_at = now() "
        "WHERE id = %s RETURNING {columns}"
    ).format(assignments=assignments, columns=sql.SQL(COLUMNS))

    params = [*fields.values(), tag_id]
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(query, params)
            row = await cur.fetchone()
        await conn.commit()
    return row


async def deactivate_tag(pool, tag_id: int) -> bool:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "UPDATE tags SET active = false, updated_at = now() WHERE id = %s", (tag_id,)
        )
        await conn.commit()
        return cur.rowcount > 0


async def delete_tag(pool, tag_id: int) -> bool:
    """Borra el tag y su historico.

    El historico se borra por rango de tag_id, no por CASCADE: `readings` es una
    hypertable sin FK a proposito (una FK por fila costaria una comprobacion en
    cada uno de los ~8.6 M inserts diarios).
    """
    async with pool.connection() as conn:
        await conn.execute("DELETE FROM readings WHERE tag_id = %s", (tag_id,))
        cur = await conn.execute("DELETE FROM tags WHERE id = %s", (tag_id,))
        await conn.commit()
        return cur.rowcount > 0
