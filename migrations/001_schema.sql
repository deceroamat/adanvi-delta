-- 001: catalogo, galerias y registro de huecos de adquisicion.

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- ---------------------------------------------------------------------
-- tags: unica fuente de verdad de que se pollea. Sin YAML, sin CSV.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tags (
    id           BIGSERIAL PRIMARY KEY,
    name         TEXT NOT NULL UNIQUE,          -- nombre CIP exacto
    label        TEXT,
    unit         TEXT,                          -- '°C', 'bar', '%' ...
    decimals     SMALLINT NOT NULL DEFAULT 2,
    kind         TEXT NOT NULL DEFAULT 'analog' -- analog | digital | counter
                 CHECK (kind IN ('analog', 'digital', 'counter')),
    active       BOOLEAN NOT NULL DEFAULT true,
    value_type   TEXT,                          -- BOOL/REAL/DINT, descubierto al leer
    last_seen_ts TIMESTAMPTZ,                   -- refrescado cada ~30 s, no cada ciclo
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS tags_active_idx ON tags (active) WHERE active;

-- ---------------------------------------------------------------------
-- acquisition_gaps: fuente de verdad de "aqui no hubo dato".
-- Sustituye a escribir ceros por tag y por ciclo durante una caida.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS acquisition_gaps (
    id         BIGSERIAL PRIMARY KEY,
    started_at TIMESTAMPTZ NOT NULL,
    ended_at   TIMESTAMPTZ,                     -- NULL = gap en curso
    reason     TEXT NOT NULL DEFAULT 'plc_disconnected',
    detail     TEXT
);

-- Cardinalidad baja (decenas/cientos de filas): un btree basta para la
-- consulta por solapamiento con la ventana visible.
CREATE INDEX IF NOT EXISTS acquisition_gaps_started_idx
    ON acquisition_gaps (started_at DESC);

-- A lo sumo un gap abierto a la vez.
CREATE UNIQUE INDEX IF NOT EXISTS acquisition_gaps_one_open_idx
    ON acquisition_gaps ((ended_at IS NULL)) WHERE ended_at IS NULL;

-- ---------------------------------------------------------------------
-- galerias
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS galleries (
    id          BIGSERIAL PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    description TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS gallery_series (
    id            BIGSERIAL PRIMARY KEY,
    gallery_id    BIGINT NOT NULL REFERENCES galleries (id) ON DELETE CASCADE,
    tag_id        BIGINT NOT NULL REFERENCES tags (id) ON DELETE CASCADE,
    visible       BOOLEAN NOT NULL DEFAULT true,
    color         TEXT NOT NULL,
    -- Series con el mismo axis_group comparten escala Y. 'auto' = agrupar
    -- por la unidad del tag. Es el primitivo que permite mezclar °C, bar y rpm.
    axis_group    TEXT NOT NULL DEFAULT 'auto',
    scale_mode    TEXT NOT NULL DEFAULT 'auto' CHECK (scale_mode IN ('auto', 'manual')),
    y_min         DOUBLE PRECISION,
    y_max         DOUBLE PRECISION,
    unit_override TEXT,
    decimals      SMALLINT,                     -- NULL = hereda del tag
    interp        TEXT NOT NULL DEFAULT 'auto'
                  CHECK (interp IN ('auto', 'linear', 'step')),
    line_width    SMALLINT NOT NULL DEFAULT 2 CHECK (line_width BETWEEN 1 AND 5),
    agg           TEXT NOT NULL DEFAULT 'avg'
                  CHECK (agg IN ('avg', 'min', 'max', 'last')),
    sort_order    INT NOT NULL DEFAULT 0,
    UNIQUE (gallery_id, tag_id)
);

CREATE INDEX IF NOT EXISTS gallery_series_gallery_idx
    ON gallery_series (gallery_id, sort_order);
