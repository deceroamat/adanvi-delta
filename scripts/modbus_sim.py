"""Esclavo Modbus/TCP falso, para probar sin el Delta AS-200 delante.

    uv run python scripts/modbus_sim.py                 # escucha en :5020
    uv run python scripts/modbus_sim.py --port 502

Sirve el mismo mapa de memoria que siembra `seed.py`, con senales que se mueven,
de modo que se puede ejercitar el camino completo —agrupador de bloques,
decodificacion, WebSocket en vivo, autoescalado del grafico— sin cablear nada.

Matarlo con Ctrl-C es la forma de comprobar el invariante que mas importa: al
perder el PLC deben aparecer la banda roja "SIN DATO" y un intervalo en
`acquisition_gaps`, y NO deben insertarse filas en cero.

El mapa coincide a proposito con el de `seed.py`:

    holding 4096..  analogicos int16 escalados x10
    holding 4192..  un float32 por si hay que probar el orden de palabra
    coil    0..     digitales
"""

from __future__ import annotations

import argparse
import math
import random
import struct
import threading
import time

from pymodbus.client import ModbusTcpClient
from pymodbus.server import StartTcpServer
from pymodbus.simulator import DataType, SimData, SimDevice

HOLDING_BASE = 4096
FLOAT_BASE = 4192
COIL_BASE = 0

N_ANALOG = 12
N_FLOAT = 4
N_COIL = 8

# Perfiles alineados con seed.py: valor base y amplitud de ruido, ya x10 porque
# el PLC publica enteros escalados y el tag lo devuelve con scale=0.1.
PERFILES = [(1750, 60), (46, 5), (14500, 400), (880, 70)]


def _registros_analogicos(t: float) -> list[int]:
    out = []
    for i in range(N_ANALOG):
        base, ruido = PERFILES[i % len(PERFILES)]
        lento = math.sin(t / 60.0 + i) * ruido
        rapido = math.sin(t / 3.0 + i) * ruido * 0.25
        valor = base + lento + rapido + random.gauss(0, ruido * 0.08)
        # Los registros son de 16 bits con signo: saturar en vez de desbordar,
        # que es justo lo que hace un PLC real. Se envian sin signo porque el
        # cable no tiene signo; el tag los reinterpreta segun su data_type.
        out.append(max(-32768, min(32767, int(valor))) & 0xFFFF)
    return out


def _registros_float(t: float) -> list[int]:
    out: list[int] = []
    for i in range(N_FLOAT):
        valor = 100.0 + math.sin(t / 45.0 + i) * 25.0
        out.extend(struct.unpack(">HH", struct.pack(">f", valor)))
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5020)
    parser.add_argument("--unit-id", type=int, default=1)
    args = parser.parse_args()

    total_hr = FLOAT_BASE + N_FLOAT * 2 + 8
    hr = SimData(0, values=[0] * total_hr, datatype=DataType.REGISTERS)
    ir = SimData(0, values=[0] * 16, datatype=DataType.REGISTERS)
    co = SimData(0, values=[False] * (COIL_BASE + N_COIL), datatype=DataType.BITS)
    di = SimData(0, values=[False] * 16, datatype=DataType.BITS)
    device = SimDevice(args.unit_id, simdata=([co], [di], [hr], [ir]))

    def mover() -> None:
        """Refresca el mapa una vez por segundo, como haria el escaneo del PLC.

        Escribe por el propio protocolo en vez de mutar los `SimData`: el
        servidor toma una copia de estos al arrancar, asi que tocarlos despues
        no cambia lo que se sirve. El sintoma era el peor posible para probar un
        historiador —valores plausibles y CONGELADOS— y por eso se hace asi.
        """
        client = ModbusTcpClient(args.host, port=args.port, timeout=2, retries=1)
        while not client.connect():
            time.sleep(0.2)
        arranque = time.monotonic()
        while True:
            t = time.monotonic() - arranque
            client.write_registers(
                HOLDING_BASE, values=_registros_analogicos(t), device_id=args.unit_id
            )
            client.write_registers(
                FLOAT_BASE, values=_registros_float(t), device_id=args.unit_id
            )
            client.write_coils(
                COIL_BASE,
                values=[(int(t) // (5 + i)) % 2 == 0 for i in range(N_COIL)],
                device_id=args.unit_id,
            )
            time.sleep(1.0)

    threading.Thread(target=mover, daemon=True).start()

    print(f"esclavo Modbus/TCP falso en {args.host}:{args.port} (esclavo {args.unit_id})")
    print(f"  holding {HOLDING_BASE}..{HOLDING_BASE + N_ANALOG - 1}  int16 x10 (scale 0.1)")
    print(f"  holding {FLOAT_BASE}..{FLOAT_BASE + N_FLOAT * 2 - 1}  float32 (orden big)")
    print(f"  coil    {COIL_BASE}..{COIL_BASE + N_COIL - 1}")
    print("Ctrl-C para simular la caida del PLC.")
    StartTcpServer(context=device, address=(args.host, args.port))


if __name__ == "__main__":
    main()
