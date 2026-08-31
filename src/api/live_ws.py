"""WebSocket de datos en vivo.

El servidor emite ticks columnares alineados al orden de `tag_ids` que el cliente
declaro al suscribirse, de modo que el frontend hace append directo sobre los
arrays de uPlot sin reordenar nada.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..worker.broadcaster import Subscriber, hub
from ..worker.status import status

log = logging.getLogger(__name__)
router = APIRouter()

MAX_SUBSCRIBED_TAGS = 50


@router.websocket("/ws/live")
async def live(ws: WebSocket) -> None:
    await ws.accept()
    sub = Subscriber()
    hub.add(sub)
    reader = asyncio.create_task(_reader(ws, sub))
    writer = asyncio.create_task(_writer(ws, sub))
    try:
        # En cuanto una de las dos termina (tipicamente el cliente se va) hay que
        # cancelar la otra explicitamente: gather la dejaria viva para siempre.
        done, pending = await asyncio.wait(
            {reader, writer}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        for task in done:
            task.result()
    except WebSocketDisconnect:
        pass
    except Exception:
        log.debug("websocket cerrado con error", exc_info=True)
    finally:
        hub.remove(sub)


async def _reader(ws: WebSocket, sub: Subscriber) -> None:
    while True:
        message = await ws.receive_json()
        if message.get("type") != "subscribe":
            continue
        raw = message.get("tag_ids") or []
        try:
            tag_ids = [int(x) for x in raw][:MAX_SUBSCRIBED_TAGS]
        except (TypeError, ValueError):
            await ws.send_json({"type": "error", "detail": "tag_ids invalido"})
            continue
        sub.tag_ids = tag_ids
        health = status.health()
        await ws.send_json(
            {
                "type": "subscribed",
                "tag_ids": tag_ids,
                # El cliente necesita saber si ya hay un hueco abierto para
                # empezar a pintar la banda sin esperar al proximo evento.
                "gap_open_since": health["gap_open_since"],
                "plc_connected": health["plc_connected"],
            }
        )


async def _writer(ws: WebSocket, sub: Subscriber) -> None:
    while True:
        message = await sub.queue.get()
        await ws.send_json(message)
