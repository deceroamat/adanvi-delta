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
        ("/tags", "Nombre CIP"),
        ("/galleries", "Galerías de tendencias"),
        ("/galleries/1", "LIVE"),
        ("/forms", "Operación"),
        ("/forms/operation", "Consecutivo"),
        ("/forms/operation", "Referencia"),
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
        "/static/css/forms.css",
        "/static/js/forms.js",
        "/static/js/form-operation.js",
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
    ["/forms/operation", "/static/js/shell.js", "/static/css/app.css"],
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


def _op_payload(**overrides):
    payload = {
        "reference": "K40",
        "consecutive": "B-001",
        "shift_date": "2026-08-15",
        "start_time": "06:00",
        "end_time": "06:40",
        "machine_speed": 420.0,
        "weight_profile": [18.0] * 10,
        "base_weight": 18.0,
        "reel_weight": 900.0,
        "breaks": 0,
        "reel_type": "x1",
    }
    payload.update(overrides)
    return payload


async def test_el_formulario_de_operacion_dice_que_zona_esta_mal(client):
    # La validacion de Pydantic corre antes del handler, asi que no toca la BD.
    profile = [18.0] * 10
    profile[3] = 200.0
    async with client:
        res = await client.post("/api/forms/operation", json=_op_payload(weight_profile=profile))
    assert res.status_code == 422
    detail = str(res.json()["detail"])
    # Que diga "la zona 4" es el punto: son diez casillas identicas en pantalla.
    assert "zona 4" in detail


async def test_el_formulario_de_operacion_exige_referencia(client):
    payload = _op_payload()
    del payload["reference"]
    async with client:
        res = await client.post("/api/forms/operation", json=payload)
    # Es opcional en la tabla (habia bobinas registradas antes de que existiera
    # la columna) pero obligatoria al registrar de aqui en adelante.
    assert res.status_code == 422


@pytest.mark.parametrize(
    "overrides",
    [
        {"machine_speed": 900.0},          # tope 600 m/min
        {"base_weight": 5.0},              # minimo 10 g/m2
        {"breaks": 9},                     # maximo 5
        {"reel_type": "x9"},               # solo x1 / x2
        {"weight_profile": [18.0] * 9},    # deben ser 10 zonas
        {"consecutive": "   "},            # consecutivo vacio
        {"reference": "   "},              # referencia vacia
        {"reference": "K" * 21},           # tope 20 caracteres
    ],
)
async def test_el_formulario_de_operacion_rechaza_valores_fuera_de_rango(client, overrides):
    async with client:
        res = await client.post("/api/forms/operation", json=_op_payload(**overrides))
    assert res.status_code == 422


async def test_history_rechaza_peticiones_mal_formadas(client):
    async with client:
        sin_rango = await client.get("/api/history?tags=1")
        tag_malo = await client.get("/api/history?tags=abc&window=1h")
    assert sin_rango.status_code == 400
    assert tag_malo.status_code == 400


@pytest.mark.parametrize(
    "path,comprimido",
    [
        ("/static/js/cache.js", True),   # 13 KB
        ("/static/js/forms.js", False),  # unas pocas lineas
    ],
)
async def test_las_respuestas_grandes_viajan_comprimidas(client, path, comprimido):
    async with client:
        res = await client.get(path, headers={"accept-encoding": "gzip"})
    # Una ventana de 1 h con 9 series son ~350 KB de JSON y comprimen 5.8x. Sin
    # esto, cada pan se los lleva enteros por el tunel de Tailscale.
    if comprimido:
        assert res.headers.get("content-encoding") == "gzip"
    else:
        # Por debajo del minimo no compensa: cuesta CPU y puede salir mas grande.
        assert "content-encoding" not in res.headers
