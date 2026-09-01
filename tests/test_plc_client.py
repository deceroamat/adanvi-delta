"""Agrupador de bloques y decodificacion Modbus.

No necesitan PLC ni base de datos, como el resto de la suite. Son los dos sitios
donde un error no se ve: un bloque mal cortado devuelve valores desplazados y un
orden de palabra invertido devuelve un numero plausible y falso.
"""

import struct
from dataclasses import dataclass

import pytest

from src.plc_client import (
    MAX_GAP_REGISTERS,
    MAX_REGISTERS_PER_READ,
    decode,
    plan_blocks,
)


@dataclass(frozen=True)
class T:
    """TagDef minimo para las pruebas."""

    address: int
    data_type: str = "int16"
    area: str = "holding"
    unit_id: int = 1
    word_order: str = "big"
    scale: float = 1.0
    value_offset: float = 0.0
    id: int = 0
    name: str = "t"


# --- agrupador --------------------------------------------------------


def test_los_tags_contiguos_caben_en_una_peticion():
    blocks, index = plan_blocks([T(10), T(11), T(12)])
    assert len(blocks) == 1
    assert (blocks[0].start, blocks[0].count) == (10, 3)
    assert [i[1] for i in index] == [0, 1, 2]


# Lo que se mide es el HUECO, no la distancia entre direcciones: un int16 en 10
# termina en 11, asi que el primer registro desperdiciado es el 11.
def test_un_hueco_justo_en_el_limite_no_parte_el_bloque():
    # Leer los registros intermedios sale mas barato que una segunda ida y vuelta.
    blocks, _ = plan_blocks([T(10), T(11 + MAX_GAP_REGISTERS)])
    assert len(blocks) == 1
    assert blocks[0].count == MAX_GAP_REGISTERS + 2


def test_un_hueco_mayor_que_el_limite_si_parte_el_bloque():
    blocks, index = plan_blocks([T(10), T(11 + MAX_GAP_REGISTERS + 1)])
    assert len(blocks) == 2
    # Cada tag queda en su bloque, con desplazamiento 0.
    assert [i[1] for i in index] == [0, 0]


def test_ningun_bloque_supera_el_tope_del_protocolo():
    # 400 registros consecutivos no caben en una peticion de 125.
    tags = [T(a) for a in range(400)]
    blocks, index = plan_blocks(tags)
    assert all(b.count <= MAX_REGISTERS_PER_READ for b in blocks)
    assert len(blocks) >= 4
    # Y todos los tags siguen localizados dentro de su bloque.
    for pos, tag in enumerate(tags):
        block_id, offset = index[pos]
        assert blocks[block_id].start + offset == tag.address
        assert 0 <= offset < blocks[block_id].count


def test_areas_y_esclavos_distintos_nunca_se_mezclan():
    tags = [T(0, area="holding"), T(0, area="input"), T(0, "bit", "coil"), T(0, unit_id=2)]
    blocks, _ = plan_blocks(tags)
    assert len(blocks) == 4


def test_un_tag_de_32_bits_reserva_sus_dos_registros():
    blocks, _ = plan_blocks([T(10, "float32")])
    assert (blocks[0].start, blocks[0].count) == (10, 2)


def test_el_orden_de_entrada_no_altera_el_plan():
    desordenados = [T(12), T(10), T(11)]
    blocks, index = plan_blocks(desordenados)
    assert len(blocks) == 1
    # El indice va alineado con la lista ORIGINAL, no con la ordenada.
    assert [i[1] for i in index] == [2, 0, 1]


def test_sin_tags_no_hay_peticiones():
    assert plan_blocks([]) == ([], [])


# --- decodificacion ---------------------------------------------------


def _words(fmt, value):
    """Empaqueta un valor y lo devuelve como registros de 16 bits."""
    raw = struct.pack(fmt, value)
    return list(struct.unpack(f">{len(raw) // 2}H", raw))


@pytest.mark.parametrize(
    "data_type,fmt,value",
    [
        ("int16", ">h", -1234),
        ("uint16", ">H", 50000),
        ("int32", ">i", -70000),
        ("uint32", ">I", 3_000_000_000),
        ("float32", ">f", 3.5),
    ],
)
def test_cada_tipo_se_decodifica_a_su_valor(data_type, fmt, value):
    assert decode(T(0, data_type), _words(fmt, value)) == pytest.approx(value)


def test_el_orden_de_palabra_invertido_da_otro_numero():
    palabras = _words(">f", 3.5)
    correcto = decode(T(0, "float32", word_order="big"), palabras)
    invertido = decode(T(0, "float32", word_order="little"), palabras)
    assert correcto == pytest.approx(3.5)
    # El punto del test: leerlo al reves NO falla, devuelve otra cosa. Por eso el
    # orden es configurable por tag y hay que verificarlo contra el PLC.
    assert invertido != pytest.approx(3.5)
    # Y con las palabras ya invertidas, 'little' lo recupera.
    assert decode(T(0, "float32", word_order="little"), palabras[::-1]) == pytest.approx(3.5)


def test_la_escala_y_el_offset_se_aplican():
    # Caso tipico: el PLC publica la temperatura multiplicada por 10.
    tag = T(0, "int16", scale=0.1, value_offset=0.0)
    assert decode(tag, _words(">h", 1805)) == pytest.approx(180.5)
    assert decode(T(0, "int16", scale=2.0, value_offset=5.0), _words(">h", 10)) == 25.0


def test_los_bits_valen_uno_o_cero():
    assert decode(T(0, "bit", "coil"), [True]) == 1.0
    assert decode(T(0, "bit", "coil"), [False]) == 0.0


def test_un_buffer_incompleto_no_inventa_un_valor():
    # Medio float32 no es medio numero: es ningun numero.
    assert decode(T(0, "float32"), [0x4060]) is None
    assert decode(T(0, "int16"), []) is None


def test_un_nan_del_plc_se_trata_como_sin_dato():
    assert decode(T(0, "float32"), _words(">f", float("nan"))) is None


# --- avisos de bloque rechazado ---------------------------------------


class _RespuestaOk:
    def __init__(self, registers):
        self.registers = registers
        self.bits = registers

    def isError(self):
        return False


class _ClienteFalso:
    """Esclavo de mentira que se puede poner a rechazar y a aceptar a voluntad."""

    def __init__(self):
        self.rechaza = False
        self.connected = True

    def _leer(self, address, *, count, device_id):
        if self.rechaza:
            from pymodbus.pdu import ExceptionResponse

            return ExceptionResponse(3, exception_code=2)
        return _RespuestaOk([0] * count)

    read_holding_registers = read_input_registers = _leer
    read_coils = read_discrete_inputs = _leer


def _cliente_con(tags):
    from src.plc_client import PlcClient

    plc = PlcClient("127.0.0.1")
    plc._client = _ClienteFalso()
    return plc, tags


def test_un_bloque_rechazado_avisa_una_sola_vez(caplog):
    """A 1 Hz, un aviso por ciclo entierra el log en horas."""
    plc, tags = _cliente_con((T(10, name="Caudal"),))
    plc._client.rechaza = True

    with caplog.at_level("WARNING", logger="src.plc_client"):
        for _ in range(10):
            lecturas = plc.read_many(tags)

    # El tag sigue degradandose en cada ciclo...
    assert lecturas[0].status == 2 and lecturas[0].value is None
    # ...pero solo se avisa del cambio de estado, una vez.
    avisos = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(avisos) == 1
    # Y el aviso nombra al tag: es la pista para encontrar la direccion mala.
    assert "Caudal" in avisos[0].getMessage()


def test_la_recuperacion_del_bloque_tambien_se_registra(caplog):
    plc, tags = _cliente_con((T(10, name="Caudal"),))

    with caplog.at_level("INFO", logger="src.plc_client"):
        plc._client.rechaza = True
        plc.read_many(tags)
        plc.read_many(tags)
        plc._client.rechaza = False
        plc.read_many(tags)
        plc.read_many(tags)

    niveles = [
        r.levelname
        for r in caplog.records
        if "acepta" in r.getMessage() or "rechaza" in r.getMessage()
    ]
    # Exactamente dos lineas: empieza a fallar y se recupera.
    assert niveles == ["WARNING", "INFO"]
