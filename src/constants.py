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
