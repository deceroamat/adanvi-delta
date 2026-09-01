"""Apagado limpio de los hilos del worker.

`__main__` para la app en un `finally`: `stop.set()` y luego `join()` de cada
hilo. Si un `join()` lanza, el resto del apagado no ocurre —el writer no llega a
vaciar su lote y se pierde hasta un segundo de adquisicion en cada parada— y ni
siquiera se ve el fallo, porque pasa mientras el proceso ya se esta muriendo.

Paso justo eso: `Thread._stop` es un metodo interno que `join()` invoca, y
llamar `self._stop = stop_event` en el constructor lo pisaba.
"""

import threading

import pytest

from src.worker.acquirer import Acquirer
from src.worker.writer import Writer


@pytest.mark.parametrize("cls", [Acquirer, Writer])
def test_ningun_hilo_pisa_los_internos_de_thread(cls):
    """Ningun atributo de instancia puede tapar un miembro de `threading.Thread`."""
    stop = threading.Event()
    worker = cls(stop)

    # Solo lo que anade la subclase: `Thread.__init__` ya deja atributos suyos
    # (`_initialized`, `_name`...) que por supuesto colisionan consigo mismos.
    base = set(vars(threading.Thread()))
    propios = set(vars(worker)) - base
    colisiones = propios & set(dir(threading.Thread))

    assert not colisiones, (
        f"{cls.__name__} tapa {sorted(colisiones)} de threading.Thread. "
        "Renombra el atributo: pisar un interno rompe join() en el apagado."
    )


def test_el_acquirer_se_detiene_y_hace_join():
    """El camino real: arrancar, pedir la parada y esperar al hilo.

    Sin PLC ni base de datos: con el catalogo vacio el ciclo no llega a abrir
    socket, que es justo lo que hace comprobable el apagado aqui.
    """
    stop = threading.Event()
    acquirer = Acquirer(stop)
    acquirer.start()
    assert acquirer.is_alive()

    stop.set()
    acquirer.join(timeout=5)  # antes lanzaba TypeError: 'Event' object is not callable
    assert not acquirer.is_alive(), "el hilo no terminó tras pedirle la parada"
