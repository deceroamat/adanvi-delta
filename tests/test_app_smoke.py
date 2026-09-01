"""Humo de rutas y estaticos.

No arranca el `lifespan`, asi que no necesita base de datos: comprueba que el
armazon HTTP (paginas, estaticos, health) esta bien cableado.
"""

import httpx
import pytest

from src.app import create_app


@pytest.fixture
def client():
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest.mark.parametrize(
    "path,needle",
    [
        ("/", "ADANVI"),
        ("/tags", "Dirección"),
        ("/galleries", "Galerías de tendencias"),
        ("/galleries/1", "LIVE"),
    ],
)
async def test_las_paginas_se_sirven(client, path, needle):
    async with client:
        res = await client.get(path)
    assert res.status_code == 200
    assert needle in res.text


@pytest.mark.parametrize(
    "path",
    [
        "/static/css/tokens.css",
        "/static/css/app.css",
        "/static/css/gallery.css",
        "/static/js/viewport.js",
        "/static/js/cache.js",
        "/static/js/chart.js",
        "/static/js/series-table.js",
        "/static/js/gallery-view.js",
        "/static/vendor/uplot/uPlot.iife.min.js",
        "/static/vendor/uplot/uPlot.min.css",
    ],
)
async def test_los_estaticos_existen(client, path):
    async with client:
        res = await client.get(path)
    assert res.status_code == 200, f"falta {path}"
    assert res.content


@pytest.mark.parametrize(
    "path",
    ["/tags", "/static/js/shell.js", "/static/css/app.css"],
)
async def test_el_frontend_se_revalida(client, path):
    async with client:
        res = await client.get(path)
    # Sin esta cabecera el navegador del panel puede seguir usando el JS viejo
    # horas despues de un despliegue, sin pedirle nada al servidor.
    assert res.headers["cache-control"] == "no-cache"


async def test_health_no_necesita_base_de_datos(client):
    async with client:
        res = await client.get("/api/health")
    assert res.status_code == 200
    body = res.json()
    # Sin worker arrancado el estado debe ser 'down', no una excepcion.
    assert body["state"] == "down"
    assert body["worker_alive"] is False
    for key in ("plc_connected", "cycle_jitter_ms_p95", "write_queue_depth", "gap_open_since"):
        assert key in body


async def test_ventana_invalida_da_400_con_mensaje_util(client):
    async with client:
        ok = await client.get("/api/history/window?w=15m")
        bad = await client.get("/api/history/window?w=3x")
    assert ok.status_code == 200
    assert ok.json()["seconds"] == 900
    assert bad.status_code == 400
    assert "minutos" in bad.json()["detail"]


async def test_history_rechaza_peticiones_mal_formadas(client):
    async with client:
        sin_rango = await client.get("/api/history?tags=1")
        tag_malo = await client.get("/api/history?tags=abc&window=1h")
    assert sin_rango.status_code == 400
    assert tag_malo.status_code == 400


async def test_las_respuestas_grandes_viajan_comprimidas(client):
    async with client:
        res = await client.get("/static/js/cache.js", headers={"accept-encoding": "gzip"})
    # Una ventana de 1 h con 9 series son ~350 KB de JSON y comprimen 5.8x. Sin
    # esto, cada pan se los lleva enteros por el tunel de Tailscale.
    assert res.headers.get("content-encoding") == "gzip"


async def test_las_respuestas_pequenas_no_se_comprimen(client):
    async with client:
        res = await client.get("/api/history/window?w=15m", headers={"accept-encoding": "gzip"})
    # Por debajo del minimo de 1 KB no compensa: cuesta CPU y puede salir mas
    # grande de lo que entro.
    assert res.status_code == 200
    assert "content-encoding" not in res.headers
