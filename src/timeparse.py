"""Parseo de ventanas y seleccion de capa/bucket.

Se parsea SOLO en el servidor: una sola verdad. El cliente manda `window=1h` o
`from`/`to` y recibe de vuelta la resolucion que se eligio.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta

WINDOW_RE = re.compile(r"^(\d+)(s|m|h|d|w|M)$")

# 'M' mayuscula es mes y 'm' minuscula es minuto (convencion estilo Grafana).
# Un mes se toma como 30 dias: para una ventana de tendencia importa que sea
# predecible y estable, no que respete el calendario.
_UNIT_SECONDS = {
    "s": 1,
    "m": 60,
    "h": 3_600,
    "d": 86_400,
    "w": 604_800,
    "M": 2_592_000,
}

MIN_WINDOW_S = 5
MAX_WINDOW_S = 5 * 365 * 86_400

DEFAULT_MAX_POINTS = 1_600
MIN_MAX_POINTS = 200
HARD_MAX_POINTS = 5_000

# Presupuesto de filas leidas por consulta. Una ventana de 30 dias sobre el
# agregado de 1 minuto da mas detalle, pero con 20 tags son ~4.3 M de filas: en
# un i5-6500T eso son segundos, y un pan que tarda segundos se siente roto.
# Pasado el presupuesto se sube a una capa mas gruesa y se acepta perder algo de
# resolucion a cambio de que la navegacion siga siendo instantanea.
MAX_SCAN_ROWS = 400_000
MIN_USEFUL_POINTS = 300


class WindowError(ValueError):
    pass


def parse_window(text: str) -> timedelta:
    """`'15m'` -> timedelta. Lanza WindowError con mensaje util si no encaja."""
    if not text:
        raise WindowError("ventana vacia")
    match = WINDOW_RE.match(text.strip())
    if not match:
        raise WindowError(
            f"ventana invalida: '{text}'. Formato esperado <numero><s|m|h|d|w|M>, "
            "por ejemplo 30s, 15m, 1h, 1d, 2w, 1M ('m' es minutos, 'M' es meses)."
        )
    amount, unit = int(match.group(1)), match.group(2)
    seconds = amount * _UNIT_SECONDS[unit]
    if seconds < MIN_WINDOW_S:
        raise WindowError(f"ventana demasiado corta: minimo {MIN_WINDOW_S}s")
    if seconds > MAX_WINDOW_S:
        raise WindowError("ventana demasiado larga: maximo 5 anos")
    return timedelta(seconds=seconds)


# ---------------------------------------------------------------------
# Capas de almacenamiento
# ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Layer:
    name: str
    table: str
    time_col: str
    bucket_s: int
    retention_s: int
    is_cagg: bool


LAYERS: tuple[Layer, ...] = (
    Layer("raw", "readings", "ts", 1, 90 * 86_400, False),
    Layer("1m", "readings_1m", "bucket", 60, 365 * 86_400, True),
    Layer("1h", "readings_1h", "bucket", 3_600, 1_825 * 86_400, True),
)

# Escalera de buckets "redondos". Snapear a estos valores mantiene los bordes de
# bucket estables mientras se hace pan, lo que permite que la cache del cliente
# reutilice los tramos ya cargados en vez de invalidarse en cada arrastre.
BUCKET_LADDER: tuple[int, ...] = (
    1, 2, 5, 10, 15, 30,
    60, 120, 300, 600, 900, 1_800,
    3_600, 7_200, 10_800, 21_600, 43_200,
    86_400, 172_800, 604_800,
)


def clamp_max_points(value: int | None) -> int:
    if not value:
        return DEFAULT_MAX_POINTS
    return max(MIN_MAX_POINTS, min(HARD_MAX_POINTS, int(value)))


def snap_bucket(seconds: float) -> int:
    for step in BUCKET_LADDER:
        if step >= seconds:
            return step
    return BUCKET_LADDER[-1]


def choose_layer(
    span_s: float, age_s: float, max_points: int, n_tags: int = 1
) -> tuple[Layer, int]:
    """Elige capa y tamano de bucket.

    `span_s` es el ancho de la ventana, `age_s` cuanto hacia atras llega su borde
    izquierdo (para descartar capas cuya retencion ya no lo cubre) y `n_tags`
    cuantas series se piden (para acotar el escaneo total).

    La resolucion se deriva de cuantos puntos caben en el chart, no de umbrales
    fijos: un umbral fijo ignora el ancho real en pixeles y acaba pidiendo o
    demasiados puntos o demasiado pocos.
    """
    max_points = clamp_max_points(max_points)
    bucket = snap_bucket(max(span_s / max_points, 1))

    usable = [lyr for lyr in LAYERS if lyr.retention_s >= age_s] or [LAYERS[-1]]

    # La capa mas GRUESA cuyo bucket nativo aun cabe en el objetivo: es la que
    # produce la misma resolucion escaneando menos filas. Si ninguna cabe (se
    # pide mas detalle del disponible a esa antiguedad) se cae a la mas fina
    # que la retencion todavia cubre.
    index = 0
    for i, layer in enumerate(usable):
        if layer.bucket_s <= bucket:
            index = i
        else:
            break

    # Si aun asi el escaneo se dispara, se sube de capa aceptando menos puntos.
    while index + 1 < len(usable):
        if (span_s / usable[index].bucket_s) * max(n_tags, 1) <= MAX_SCAN_ROWS:
            break
        if span_s / usable[index + 1].bucket_s < MIN_USEFUL_POINTS:
            break
        index += 1

    chosen = usable[index]

    # El bucket final debe ser multiplo del nativo de la capa elegida.
    if bucket < chosen.bucket_s:
        bucket = chosen.bucket_s
    elif bucket % chosen.bucket_s:
        bucket = ((bucket // chosen.bucket_s) + 1) * chosen.bucket_s
    return chosen, bucket


def format_bucket(seconds: int) -> str:
    for unit_s, suffix in ((86_400, "d"), (3_600, "h"), (60, "m")):
        if seconds >= unit_s and seconds % unit_s == 0:
            return f"{seconds // unit_s}{suffix}"
    return f"{seconds}s"
