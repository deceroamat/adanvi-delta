from datetime import timedelta

import pytest

from src.timeparse import (
    HARD_MAX_POINTS,
    WindowError,
    choose_layer,
    clamp_max_points,
    format_bucket,
    parse_window,
    snap_bucket,
)

DAY = 86_400


class TestParseWindow:
    @pytest.mark.parametrize(
        "text,seconds",
        [
            ("30s", 30),
            ("1m", 60),
            ("15m", 900),
            ("1h", 3_600),
            ("8h", 28_800),
            ("1d", DAY),
            ("2w", 14 * DAY),
            ("1M", 30 * DAY),
        ],
    )
    def test_valid(self, text, seconds):
        assert parse_window(text) == timedelta(seconds=seconds)

    def test_m_mayuscula_es_mes_y_minuscula_es_minuto(self):
        assert parse_window("1M") == timedelta(days=30)
        assert parse_window("1m") == timedelta(minutes=1)

    @pytest.mark.parametrize("text", ["", "3x", "h", "1 h", "-1h", "1.5h", "10y", "1s"])
    def test_invalid(self, text):
        with pytest.raises(WindowError):
            parse_window(text)

    def test_mensaje_de_error_es_util(self):
        with pytest.raises(WindowError, match="minutos"):
            parse_window("3x")


class TestChooseLayer:
    def test_ventana_corta_usa_crudo_sin_agregar(self):
        layer, bucket = choose_layer(span_s=600, age_s=600, max_points=1_600)
        assert layer.name == "raw"
        assert bucket == 1

    def test_una_hora_cabe_en_crudo(self):
        layer, bucket = choose_layer(span_s=3_600, age_s=3_600, max_points=1_600)
        assert layer.name == "raw"
        assert bucket == 5  # 3600/1600 = 2.25 -> escalon 5 s

    def test_un_dia_usa_el_agregado_de_un_minuto(self):
        layer, bucket = choose_layer(span_s=DAY, age_s=DAY, max_points=1_600)
        assert layer.name == "1m"
        assert bucket == 60

    def test_un_mes_con_pocas_series_conserva_el_detalle_de_un_minuto(self):
        # 30 dias / 1600 puntos = buckets de 30 min. Con 1 tag son solo ~43 k
        # filas leidas del agregado de 1 min: sale a cuenta el detalle extra.
        layer, bucket = choose_layer(span_s=30 * DAY, age_s=30 * DAY, max_points=1_600)
        assert layer.name == "1m"
        assert bucket == 1_800

    def test_un_mes_con_muchas_series_sube_al_agregado_horario(self):
        # Con 20 tags serian ~864 k filas: pasa el presupuesto de escaneo y se
        # prefiere una consulta instantanea con 720 puntos a una lenta con 1440.
        layer, bucket = choose_layer(
            span_s=30 * DAY, age_s=30 * DAY, max_points=1_600, n_tags=20
        )
        assert layer.name == "1h"
        assert bucket == 3_600

    def test_el_presupuesto_de_escaneo_se_respeta(self):
        for n_tags in (1, 5, 20, 50):
            for span in (3_600, 8 * 3_600, DAY, 30 * DAY, 365 * DAY):
                layer, _ = choose_layer(span, span, 1_600, n_tags=n_tags)
                scanned = (span / layer.bucket_s) * n_tags
                # Solo se admite pasarse si ya no queda capa mas gruesa util.
                assert scanned <= 400_000 or layer.name == "1h"

    def test_un_ano_agrega_sobre_el_horario(self):
        layer, bucket = choose_layer(span_s=365 * DAY, age_s=365 * DAY, max_points=1_600)
        assert layer.name == "1h"
        assert bucket == 21_600
        assert bucket % layer.bucket_s == 0

    def test_nunca_se_pide_crudo_mas_alla_de_su_retencion(self):
        # 120 dias atras el crudo ya se borro: debe caer al agregado de 1 min.
        layer, _ = choose_layer(span_s=600, age_s=120 * DAY, max_points=1_600)
        assert layer.name == "1m"

    def test_mas_de_un_ano_atras_solo_queda_el_horario(self):
        layer, _ = choose_layer(span_s=600, age_s=400 * DAY, max_points=1_600)
        assert layer.name == "1h"

    @pytest.mark.parametrize(
        "span_s",
        [60, 600, 3_600, 8 * 3_600, DAY, 7 * DAY, 30 * DAY, 180 * DAY, 365 * DAY],
    )
    def test_el_numero_de_puntos_nunca_desborda(self, span_s):
        max_points = 1_600
        _, bucket = choose_layer(span_s, span_s, max_points)
        # El snap redondea hacia arriba, asi que el conteo real siempre cabe.
        assert span_s / bucket <= max_points

    def test_el_bucket_siempre_es_multiplo_del_nativo_de_la_capa(self):
        for span in (600, 3_600, DAY, 10 * DAY, 100 * DAY, 900 * DAY):
            layer, bucket = choose_layer(span, span, 1_600)
            assert bucket % layer.bucket_s == 0

    def test_max_points_mas_alto_da_mas_detalle(self):
        _, coarse = choose_layer(DAY, DAY, 400)
        _, fine = choose_layer(DAY, DAY, 3_000)
        assert fine <= coarse


class TestBucketSnapping:
    def test_los_bordes_son_estables_al_hacer_pan(self):
        """Un desplazamiento pequeno no debe cambiar el tamano de bucket.

        Si cambiara, la cache de tramos del cliente se invalidaria en cada
        arrastre y el pan se sentiria lento.
        """
        base = choose_layer(3_600, 3_600, 1_600)[1]
        for shift in range(0, 600, 37):
            assert choose_layer(3_600, 3_600 + shift, 1_600)[1] == base

    def test_snap_redondea_hacia_arriba(self):
        assert snap_bucket(2.25) == 5
        assert snap_bucket(61) == 120
        assert snap_bucket(1) == 1

    def test_clamp(self):
        assert clamp_max_points(None) == 1_600
        assert clamp_max_points(10) == 200
        assert clamp_max_points(99_999) == HARD_MAX_POINTS


class TestFormatBucket:
    @pytest.mark.parametrize(
        "seconds,expected",
        [(1, "1s"), (30, "30s"), (60, "1m"), (900, "15m"), (3_600, "1h"), (86_400, "1d")],
    )
    def test_formato(self, seconds, expected):
        assert format_bucket(seconds) == expected
