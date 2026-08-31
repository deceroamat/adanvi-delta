"""Constantes compartidas entre worker, API y frontend."""

from __future__ import annotations

# Codigos de calidad almacenados en readings.status (SMALLINT).
STATUS_GOOD = 0
STATUS_DISCONNECTED = 1  # reservado: durante una caida no se insertan filas
STATUS_TAG_ERROR = 2
STATUS_STALE = 3

STATUS_NAMES = {
    STATUS_GOOD: "Good",
    STATUS_DISCONNECTED: "Disconnected",
    STATUS_TAG_ERROR: "TagError",
    STATUS_STALE: "Stale",
}

# Paleta de series. Ocho tonos solidos distinguibles sobre fondo oscuro.
SERIES_PALETTE = [
    "#3987e5",
    "#008300",
    "#d55181",
    "#c98500",
    "#199e70",
    "#d95926",
    "#9085e9",
    "#e66767",
]
