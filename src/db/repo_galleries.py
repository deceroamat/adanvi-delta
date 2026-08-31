"""Galerias y su configuracion de series.

Todo lo que se toca en la tabla bajo el grafico vive aqui, no en localStorage:
es lo que hace que un F5 restaure colores, ejes y escalas.
"""

from __future__ import annotations

from typing import Any

from psycopg import sql
from psycopg.rows import dict_row

SERIES_FIELDS = (
    "tag_id",
    "visible",
    "color",
    "axis_group",
    "scale_mode",
    "y_min",
    "y_max",
    "unit_override",
    "decimals",
    "interp",
    "line_width",
    "agg",
    "sort_order",
)


async def list_galleries(pool) -> list[dict[str, Any]]:
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT g.id, g.name, g.description, g.created_at, g.updated_at, "
            "       count(s.id) AS series_count "
            "FROM galleries g LEFT JOIN gallery_series s ON s.gallery_id = g.id "
            "GROUP BY g.id ORDER BY g.name"
        )
        return await cur.fetchall()


async def count_galleries(pool) -> int:
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT count(*) FROM galleries")
        row = await cur.fetchone()
        return row[0]


async def get_gallery(pool, gallery_id: int) -> dict[str, Any] | None:
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT id, name, description, created_at, updated_at "
            "FROM galleries WHERE id = %s",
            (gallery_id,),
        )
        gallery = await cur.fetchone()
        if gallery is None:
            return None

        await cur.execute(
            "SELECT s.id, s.tag_id, s.visible, s.color, s.axis_group, s.scale_mode, "
            "       s.y_min, s.y_max, s.unit_override, s.decimals, s.interp, "
            "       s.line_width, s.agg, s.sort_order, "
            "       t.name AS tag_name, t.label AS tag_label, t.unit AS tag_unit, "
            "       t.decimals AS tag_decimals, t.kind AS tag_kind, t.active AS tag_active "
            "FROM gallery_series s JOIN tags t ON t.id = s.tag_id "
            "WHERE s.gallery_id = %s ORDER BY s.sort_order, s.id",
            (gallery_id,),
        )
        gallery["series"] = await cur.fetchall()
    return gallery


async def create_gallery(pool, name: str, description: str | None) -> dict[str, Any]:
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "INSERT INTO galleries (name, description) VALUES (%s, %s) "
                "RETURNING id, name, description, created_at, updated_at",
                (name, description),
            )
            row = await cur.fetchone()
        await conn.commit()
    row["series_count"] = 0
    return row


async def update_gallery(pool, gallery_id: int, data: dict[str, Any]) -> dict[str, Any] | None:
    fields = {k: v for k, v in data.items() if k in ("name", "description")}
    if not fields:
        gallery = await get_gallery(pool, gallery_id)
        return gallery

    assignments = sql.SQL(", ").join(
        sql.SQL("{} = %s").format(sql.Identifier(name)) for name in fields
    )
    query = sql.SQL(
        "UPDATE galleries SET {assignments}, updated_at = now() "
        "WHERE id = %s RETURNING id, name, description, created_at, updated_at"
    ).format(assignments=assignments)

    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(query, [*fields.values(), gallery_id])
            row = await cur.fetchone()
        await conn.commit()
    return row


async def delete_gallery(pool, gallery_id: int) -> bool:
    async with pool.connection() as conn:
        cur = await conn.execute("DELETE FROM galleries WHERE id = %s", (gallery_id,))
        await conn.commit()
        return cur.rowcount > 0


async def replace_series(pool, gallery_id: int, series: list[dict[str, Any]]) -> None:
    """Reemplazo atomico: la UI manda siempre la lista completa."""
    names = ("gallery_id", *SERIES_FIELDS)
    insert = sql.SQL("INSERT INTO gallery_series ({cols}) VALUES ({vals})").format(
        cols=sql.SQL(", ").join(sql.Identifier(n) for n in names),
        vals=sql.SQL(", ").join(sql.Placeholder() * len(names)),
    )

    async with pool.connection() as conn, conn.transaction():
        await conn.execute(
            "DELETE FROM gallery_series WHERE gallery_id = %s", (gallery_id,)
        )
        for order, item in enumerate(series):
            values = [gallery_id]
            for field in SERIES_FIELDS:
                values.append(order if field == "sort_order" else item.get(field))
            await conn.execute(insert, values)
        await conn.execute(
            "UPDATE galleries SET updated_at = now() WHERE id = %s", (gallery_id,)
        )
