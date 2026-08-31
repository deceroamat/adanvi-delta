"""Habilita el rol de solo lectura `adanvi_ro` para herramientas externas.

    ADANVI_RO_PASSWORD=... uv run python scripts/grant_ro.py

La migracion 004 crea el rol con SELECT sobre todo pero NOLOGIN y sin
contrasena. Esto no se hace en una migracion a proposito: el runner
(`src/db/migrate.py`) ejecuta SQL plano sin parametros, asi que la contrasena
acabaria escrita en un archivo versionado.

Es idempotente: volver a ejecutarlo simplemente rota la contrasena.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg
from psycopg import sql

from src.config import settings

ROLE = "adanvi_ro"


def main() -> int:
    password = os.getenv("ADANVI_RO_PASSWORD", "").strip()
    if not password:
        print(
            "falta ADANVI_RO_PASSWORD (definela en .env, que ya esta en .gitignore)",
            file=sys.stderr,
        )
        return 1

    with psycopg.connect(settings.database_url) as conn:
        # El DDL no admite placeholders, de ahi el sql.Literal: escapa la
        # contrasena igual que un parametro, sin concatenar a mano.
        conn.execute(
            sql.SQL("ALTER ROLE {role} LOGIN PASSWORD {pw}").format(
                role=sql.Identifier(ROLE), pw=sql.Literal(password)
            )
        )

        # El GRANT de la 004 fue una foto del momento; desde entonces se aplico
        # la 005. Reaplicarlo cubre cualquier objeto creado despues.
        conn.execute(
            sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA public TO {role}").format(
                role=sql.Identifier(ROLE)
            )
        )
        conn.commit()

    print(f"rol {ROLE} habilitado para login, solo SELECT")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
