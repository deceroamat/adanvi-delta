"""Aplicacion FastAPI.

Solo `lifespan`: nada de `@app.on_event` (mezclarlos fue uno de los errores del
prototipo y produce inicializaciones que se ejecutan dos veces o ninguna).
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .api import export, forms, galleries, health, history, live_ws, tags
from .config import settings
from .db.pool import close_async_pool, open_async_pool
from .worker.broadcaster import hub

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    hub.bind_loop(asyncio.get_running_loop())
    await open_async_pool()
    log.info("API lista en el puerto %d", settings.http_port)
    try:
        yield
    finally:
        await close_async_pool()


class EstaticosRevalidados(StaticFiles):
    """Estaticos que el navegador debe revalidar en cada carga.

    Los nombres no llevan hash de contenido, asi que sin esto el navegador
    aplica su heuristica de cache y puede seguir sirviendo el JS viejo durante
    horas despues de un despliegue. "no-cache" no prohibe cachear: obliga a
    preguntar, y con el ETag que Starlette ya manda la respuesta habitual es un
    304 sin cuerpo.
    """

    def file_response(self, *args, **kwargs) -> Response:
        # La cabecera va sobre lo que devuelve super(), que puede ser el 200 o
        # el 304; asi vale para los dos caminos.
        response = super().file_response(*args, **kwargs)
        response.headers["cache-control"] = "no-cache"
        return response


def create_app() -> FastAPI:
    app = FastAPI(title="ADANVI by emolog", version="1.0.0", lifespan=lifespan)

    # /api/history son cientos de KB de JSON numerico —352 KB una ventana de 1 h
    # con 9 series— que comprimen ~5.8x. En la LAN de planta se nota poco, pero
    # las consultas remotas por Tailscale van por WireGuard y ahi cada pan se
    # llevaba el payload entero. Nivel 6 y no el 9 por defecto: el ratio es
    # practicamente el mismo y cuesta bastante menos CPU en un i5-6500T que
    # ademas esta poleando el PLC a 1 Hz.
    app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=6)

    app.include_router(health.router)
    app.include_router(tags.router)
    app.include_router(history.router)
    app.include_router(galleries.router)
    app.include_router(export.router)
    app.include_router(forms.router)
    app.include_router(live_ws.router)

    static_dir = settings.static_dir
    app.mount("/static", EstaticosRevalidados(directory=static_dir), name="static")

    def page(filename: str):
        async def handler():
            # Misma razon que en EstaticosRevalidados: el HTML tampoco lleva hash.
            return FileResponse(static_dir / filename, headers={"cache-control": "no-cache"})

        return handler

    app.get("/", include_in_schema=False)(page("index.html"))
    app.get("/tags", include_in_schema=False)(page("tags.html"))
    app.get("/galleries", include_in_schema=False)(page("galleries.html"))
    app.get("/galleries/{gallery_id}", include_in_schema=False)(page("gallery.html"))
    app.get("/forms", include_in_schema=False)(page("forms.html"))
    app.get("/forms/operation", include_in_schema=False)(page("form-operation.html"))

    return app


app = create_app()
