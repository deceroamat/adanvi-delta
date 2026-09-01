"""Cliente PLC Delta AS-200 sobre Modbus/TCP (pymodbus).

Modbus no se parece a CIP en lo que mas importa aqui: no hay tags con nombre, no
hay tipo en el cable y **no existe la lectura dispersa**. Una peticion lee un
rango contiguo, asi que ~100 tags repartidos por el mapa de memoria no salen en
una llamada como salian por CIP: hay que agrupar.

De ahi que el trabajo real de este modulo sea `plan_blocks`. El plan se calcula
una vez por catalogo y se reutiliza en cada ciclo; recalcularlo a 1 Hz seria
gastar CPU en algo que solo cambia cuando alguien da de alta un tag.

Semantica de fallos, que es la que sostiene el invariante de los huecos:

  - Socket caido o timeout  -> se LANZA, el acquirer abre un gap y no se inserta
                               ninguna fila. "No hubo adquisicion" no es "valio 0".
  - Respuesta de excepcion  -> solo los tags de ESE bloque salen con value=None y
    (direccion ilegal)         STATUS_TAG_ERROR; el resto del ciclo se guarda.

La segunda es mas gruesa que en CIP, donde el error venia tag a tag: Modbus
responde por peticion, no por variable, asi que una direccion mal tecleada
invalida a sus vecinas de bloque. Es una consecuencia del protocolo, no una
decision de diseno.
"""

from __future__ import annotations

import contextlib
import logging
import struct
from dataclasses import dataclass

from .constants import STATUS_GOOD, STATUS_TAG_ERROR

log = logging.getLogger(__name__)

# Backoff de reconexion en segundos; se satura en el ultimo valor.
BACKOFF_S = (1, 2, 4, 8, 15)

AREAS = ("coil", "discrete", "holding", "input")
BIT_AREAS = ("coil", "discrete")
DATA_TYPES = ("bit", "int16", "uint16", "int32", "uint32", "float32")

# Cuantos registros ocupa cada tipo.
WORDS: dict[str, int] = {
    "bit": 1,
    "int16": 1,
    "uint16": 1,
    "int32": 2,
    "uint32": 2,
    "float32": 2,
}

# Topes del protocolo: FC03/FC04 devuelven como mucho 125 registros y FC01/FC02
# 2000 bits, porque el byte de conteo de la respuesta es de 8 bits.
MAX_REGISTERS_PER_READ = 125
MAX_BITS_PER_READ = 2000

# Huecos mas pequenos que esto se leen igual en vez de partir el bloque: traer
# unos registros que nadie usa es mucho mas barato que una segunda ida y vuelta
# TCP, y a 1 Hz lo que hay que economizar son peticiones, no bytes.
MAX_GAP_REGISTERS = 8
MAX_GAP_BITS = 64


@dataclass(slots=True)
class TagRead:
    name: str
    value: float | None
    status: int
    error: str | None = None


@dataclass(frozen=True, slots=True)
class Block:
    """Una peticion Modbus: un rango contiguo de un area de un esclavo."""

    unit_id: int
    area: str
    start: int
    count: int

    @property
    def is_bits(self) -> bool:
        return self.area in BIT_AREAS


def words_for(data_type: str) -> int:
    return WORDS.get(data_type, 1)


def plan_blocks(tags) -> tuple[list[Block], list[tuple[int, int]]]:
    """Agrupa los tags en peticiones contiguas.

    Devuelve los bloques y, alineado con `tags`, en que bloque y con que
    desplazamiento vive cada uno. El indice se calcula aqui para que el ciclo de
    lectura no tenga que buscar nada.
    """
    order = sorted(range(len(tags)), key=lambda i: (tags[i].unit_id, tags[i].area, tags[i].address))

    blocks: list[Block] = []
    index: list[tuple[int, int]] = [(-1, -1)] * len(tags)

    current: list[int] = []          # posiciones (en `tags`) del bloque en curso
    start = end = 0                  # [start, end) cubierto por el bloque en curso
    key: tuple[int, str] | None = None

    def flush() -> None:
        nonlocal current
        if not current:
            return
        block_id = len(blocks)
        blocks.append(Block(key[0], key[1], start, end - start))
        for pos in current:
            index[pos] = (block_id, tags[pos].address - start)
        current = []

    for pos in order:
        tag = tags[pos]
        span = 1 if tag.area in BIT_AREAS else words_for(tag.data_type)
        tag_end = tag.address + span
        tag_key = (tag.unit_id, tag.area)

        limit = MAX_BITS_PER_READ if tag.area in BIT_AREAS else MAX_REGISTERS_PER_READ
        max_gap = MAX_GAP_BITS if tag.area in BIT_AREAS else MAX_GAP_REGISTERS

        fits = (
            key == tag_key
            and current
            and tag.address - end <= max_gap
            and tag_end - start <= limit
        )
        if not fits:
            flush()
            key = tag_key
            start = tag.address
            end = tag_end
        else:
            end = max(end, tag_end)
        current.append(pos)

    flush()
    return blocks, index


def decode(tag, words: list[int] | list[bool]) -> float | None:
    """Convierte los registros crudos de un tag en su valor de ingenieria.

    `words` son las palabras del tag ya recortadas del buffer del bloque. Un
    valor no representable devuelve None (TagError) en vez de un numero
    inventado: en un historiador, un dato falso es peor que un dato ausente.
    """
    if not words:
        return None

    if tag.area in BIT_AREAS or tag.data_type == "bit":
        return 1.0 if words[0] else 0.0

    size = words_for(tag.data_type)
    if len(words) < size:
        return None

    raw = list(words[:size])
    # El orden de palabra solo existe a partir de 32 bits. Delta y la mayoria de
    # los PLC publican la palabra alta primero ('big'), pero no todos, y leerlo
    # al reves da un numero plausible y equivocado: por eso es configurable.
    if size == 2 and tag.word_order == "little":
        raw.reverse()

    packed = struct.pack(f">{size}H", *raw)
    fmt = {"int16": ">h", "uint16": ">H", "int32": ">i", "uint32": ">I", "float32": ">f"}[
        tag.data_type
    ]
    value = struct.unpack(fmt, packed)[0]

    if tag.data_type == "float32" and value != value:  # NaN
        return None
    return float(value) * tag.scale + tag.value_offset


class PlcClient:
    def __init__(self, ip: str, port: int = 502, timeout_s: float = 1.0) -> None:
        self.ip = ip
        self.port = port
        self.timeout_s = timeout_s
        self._client = None
        self._fail_streak = 0
        # Plan de bloques cacheado. `TagRegistry` reemplaza la tupla entera al
        # recargar el catalogo, asi que comparar identidad basta para saber si
        # hay que replanificar.
        self._planned_for = None
        self._plan: tuple[list[Block], list[tuple[int, int]]] = ([], [])
        # Bloques que el esclavo esta rechazando ahora mismo. Solo sirve para no
        # repetir el aviso en cada ciclo: una direccion mal puesta que nadie
        # corrige llenaria el log a razon de una linea por segundo y por bloque,
        # y el ruido acaba escondiendo lo que si es nuevo. Va indexado por
        # posicion en el plan, asi que se limpia al replanificar.
        self._rejected: set[int] = set()

    @property
    def connected(self) -> bool:
        return self._client is not None and self._client.connected

    def backoff_s(self) -> float:
        idx = min(self._fail_streak, len(BACKOFF_S) - 1)
        return float(BACKOFF_S[idx])

    def connect(self) -> bool:
        from pymodbus.client import ModbusTcpClient  # import perezoso: acelera los tests

        self.close()
        try:
            # retries=1 a proposito: el reintento interno de pymodbus se comeria
            # el presupuesto del ciclo de 1 Hz. Reintentar es trabajo del backoff.
            client = ModbusTcpClient(
                self.ip, port=self.port, timeout=self.timeout_s, retries=1
            )
            if not client.connect():
                raise ConnectionError(f"sin respuesta en {self.ip}:{self.port}")
            self._client = client
            self._fail_streak = 0
            log.info("PLC Delta conectado en %s:%d", self.ip, self.port)
            return True
        except Exception as exc:
            self._fail_streak += 1
            self._client = None
            log.warning(
                "conexion al PLC %s:%d fallo (intento %d): %s",
                self.ip,
                self.port,
                self._fail_streak,
                exc,
            )
            return False

    def close(self) -> None:
        if self._client is not None:
            with contextlib.suppress(Exception):
                self._client.close()
            self._client = None

    # --- lectura -------------------------------------------------------

    def _blocks_for(self, tags) -> tuple[list[Block], list[tuple[int, int]]]:
        if tags is not self._planned_for:
            self._plan = plan_blocks(tags)
            self._planned_for = tags
            self._rejected.clear()
            blocks = self._plan[0]
            log.info("plan de lectura: %d tags en %d peticiones", len(tags), len(blocks))
        return self._plan

    def _read_block(self, block: Block) -> list | None:
        """Buffer del bloque, o None si el PLC lo rechazo.

        Lanza si el problema es de transporte: eso es un hueco de adquisicion,
        no un error de un tag.
        """
        from pymodbus.pdu import ExceptionResponse

        readers = {
            "holding": self._client.read_holding_registers,
            "input": self._client.read_input_registers,
            "coil": self._client.read_coils,
            "discrete": self._client.read_discrete_inputs,
        }
        result = readers[block.area](
            block.start, count=block.count, device_id=block.unit_id
        )

        if isinstance(result, ExceptionResponse):
            # El esclavo contesto "esa direccion no existe": es configuracion,
            # no perdida de comunicacion. Se degrada solo este bloque; de avisar
            # se encarga `read_many`, que solo lo hace en los cambios de estado.
            return None
        if result.isError():
            raise ConnectionError(f"lectura {block.area}@{block.start} fallida: {result}")

        return result.bits if block.is_bits else result.registers

    def _note_rejection(self, block_id, block: Block, rechazado: bool, tags, index) -> None:
        """Avisa solo cuando un bloque EMPIEZA o DEJA de ser rechazado.

        Repetirlo cada ciclo no aporta nada —el estado no ha cambiado— y a 1 Hz
        entierra el resto del log en cuestion de horas. Los cambios de estado si
        son noticia, igual que con los huecos de adquisicion.
        """
        estaba = block_id in self._rejected
        if rechazado == estaba:
            return

        rango = f"{block.area}[{block.start}..{block.start + block.count - 1}]"
        afectados = [t.name for pos, t in enumerate(tags) if index[pos][0] == block_id]
        if rechazado:
            self._rejected.add(block_id)
            log.warning(
                "el PLC rechaza %s del esclavo %d: revisa la direccion de %s",
                rango,
                block.unit_id,
                ", ".join(afectados) or "(sin tags)",
            )
        else:
            self._rejected.discard(block_id)
            log.info("el PLC vuelve a aceptar %s: %s", rango, ", ".join(afectados))

    def read_many(self, tags) -> list[TagRead]:
        """Lee todos los tags en el minimo numero de peticiones contiguas."""
        if not tags:
            return []
        if self._client is None:
            raise ConnectionError("cliente Modbus no abierto")

        blocks, index = self._blocks_for(tags)
        buffers: list[list | None] = []
        for block_id, block in enumerate(blocks):
            buffer = self._read_block(block)
            buffers.append(buffer)
            self._note_rejection(block_id, block, buffer is None, tags, index)

        out: list[TagRead] = []
        for pos, tag in enumerate(tags):
            block_id, offset = index[pos]
            buffer = buffers[block_id] if block_id >= 0 else None
            if buffer is None:
                out.append(TagRead(tag.name, None, STATUS_TAG_ERROR, error="bloque rechazado"))
                continue

            size = 1 if tag.area in BIT_AREAS else words_for(tag.data_type)
            words = buffer[offset : offset + size]
            value = decode(tag, words)
            if value is None:
                out.append(
                    TagRead(tag.name, None, STATUS_TAG_ERROR, error="valor no decodificable")
                )
            else:
                out.append(TagRead(tag.name, value, STATUS_GOOD))
        return out
