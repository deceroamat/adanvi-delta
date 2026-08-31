"""Catalogo de tags activos en memoria.

El hilo escritor lo refresca desde la BD; el hilo de adquisicion solo lee. Asi
el ciclo de lectura del PLC nunca depende de la latencia de Postgres.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TagDef:
    id: int
    name: str


class TagRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tags: tuple[TagDef, ...] = ()
        self._version = 0

    def set(self, tags: list[TagDef]) -> bool:
        """Devuelve True si la lista cambio respecto a la anterior."""
        new = tuple(tags)
        with self._lock:
            if new == self._tags:
                return False
            self._tags = new
            self._version += 1
            return True

    def get(self) -> tuple[TagDef, ...]:
        with self._lock:
            return self._tags

    @property
    def version(self) -> int:
        with self._lock:
            return self._version


registry = TagRegistry()
