"""Cliente PLC Allen-Bradley sobre pycomm3.

Reutiliza lo que ya funcionaba bien en el prototipo: multi-read CIP en una sola
llamada y backoff de reconexion con reintentos infinitos.
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass

from .constants import STATUS_GOOD, STATUS_TAG_ERROR

log = logging.getLogger(__name__)

# Backoff de reconexion en segundos; se satura en el ultimo valor.
BACKOFF_S = (1, 2, 4, 8, 15)


@dataclass(slots=True)
class TagRead:
    name: str
    value: float | None
    status: int
    value_type: str | None = None
    error: str | None = None


def _coerce(value: object) -> float | None:
    """Todo se almacena como DOUBLE PRECISION; BOOL entra como 0.0/1.0."""
    if value is None:
        return None
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    return None


class PlcClient:
    def __init__(self, ip: str) -> None:
        self.ip = ip
        self._driver = None
        self._fail_streak = 0

    @property
    def connected(self) -> bool:
        return self._driver is not None and getattr(self._driver, "connected", False)

    def backoff_s(self) -> float:
        idx = min(self._fail_streak, len(BACKOFF_S) - 1)
        return float(BACKOFF_S[idx])

    def connect(self) -> bool:
        from pycomm3 import LogixDriver  # import perezoso: acelera los tests

        self.close()
        try:
            driver = LogixDriver(self.ip)
            driver.open()
            self._driver = driver
            self._fail_streak = 0
            log.info("PLC conectado en %s", self.ip)
            return True
        except Exception as exc:
            self._fail_streak += 1
            self._driver = None
            log.warning(
                "conexion al PLC %s fallo (intento %d): %s", self.ip, self._fail_streak, exc
            )
            return False

    def close(self) -> None:
        if self._driver is not None:
            # Cerrar es best-effort: si el socket ya murio no hay nada que hacer.
            with contextlib.suppress(Exception):
                self._driver.close()
            self._driver = None

    def read_many(self, names: list[str]) -> list[TagRead]:
        """Lee todos los tags en una sola llamada CIP.

        Lanza si la conexion se cayo, para que el ciclo abra un gap. Un tag que
        falla individualmente no lanza: vuelve con value=None y TagError, de modo
        que solo esa serie se corta y la escala del resto no se ve afectada.
        """
        if not names:
            return []
        if self._driver is None:
            raise ConnectionError("driver PLC no abierto")

        results = self._driver.read(*names)
        if not isinstance(results, list):
            results = [results]

        out: list[TagRead] = []
        for name, res in zip(names, results, strict=False):
            if res is None:
                out.append(TagRead(name, None, STATUS_TAG_ERROR, error="sin respuesta"))
                continue
            if getattr(res, "error", None):
                out.append(TagRead(name, None, STATUS_TAG_ERROR, error=str(res.error)))
                continue
            value = _coerce(res.value)
            if value is None:
                out.append(
                    TagRead(name, None, STATUS_TAG_ERROR, error=f"tipo no numerico: {res.type}")
                )
                continue
            out.append(TagRead(name, value, STATUS_GOOD, value_type=res.type))
        return out
