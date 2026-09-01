"""Catalogo de tags activos en memoria.

El hilo escritor lo refresca desde la BD; el hilo de adquisicion solo lee. Asi
el ciclo de lectura del PLC nunca depende de la latencia de Postgres.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TagDef:
    """Todo lo que el acquirer necesita para leer un tag por Modbus.

    Con CIP bastaba el nombre y el tipo se descubria al leer. Modbus no dice de
    que tipo es lo que devuelve: el tipo, el orden de palabra y la escala son
    declaraciones del catalogo, y por eso viajan aqui.
    """

    id: int
    name: str
    unit_id: int
    area: str
    address: int
    data_type: str
    word_order: str
    scale: float
    value_offset: float


class TagRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tags: tuple[TagDef, ...] = ()

    def set(self, tags: list[TagDef]) -> bool:
        """Devuelve True si la lista cambio respecto a la anterior."""
        new = tuple(tags)
        with self._lock:
            if new == self._tags:
                return False
            self._tags = new
            return True

    def get(self) -> tuple[TagDef, ...]:
        with self._lock:
            return self._tags


registry = TagRegistry()
