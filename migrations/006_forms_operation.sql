-- 006: formulario de operacion (captura manual bobina a bobina).
--
-- Lo que el PLC no puede saber: el consecutivo de la bobina, el perfil de peso
-- medido por zonas y las rupturas del turno. Se guardan instantes reales
-- (TIMESTAMPTZ) y no solo la hora, para poder cruzar cada bobina con el
-- historico de tendencias por rango de tiempo.

CREATE TABLE IF NOT EXISTS op_records (
    id             BIGSERIAL PRIMARY KEY,
    consecutive    TEXT NOT NULL UNIQUE,          -- lo teclea el operador
    shift_date     DATE NOT NULL,                 -- dia de captura
    started_at     TIMESTAMPTZ NOT NULL,          -- shift_date + hora inicio, en TZ local
    ended_at       TIMESTAMPTZ NOT NULL,          -- +1 dia si la hora fin <= hora inicio
    machine_speed  REAL NOT NULL CHECK (machine_speed BETWEEN 0 AND 600),   -- m/min
    -- Perfil transversal de la bobina: 10 zonas, g/m2. Un array con CHECK de
    -- cardinalidad en vez de zona_1..zona_10: se indexa igual en SQL
    -- (weight_profile[3], unnest(...)) y el CSV es quien lo aplana a columnas.
    weight_profile REAL[] NOT NULL
                   CHECK (array_length(weight_profile, 1) = 10),
    base_weight    REAL NOT NULL CHECK (base_weight BETWEEN 10 AND 120),    -- g/m2
    reel_weight    REAL NOT NULL CHECK (reel_weight BETWEEN 0 AND 5000),    -- kg
    breaks         SMALLINT NOT NULL CHECK (breaks BETWEEN 0 AND 5),
    reel_type      TEXT NOT NULL CHECK (reel_type IN ('x1', 'x2')),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS op_records_started_idx ON op_records (started_at DESC);
CREATE INDEX IF NOT EXISTS op_records_shift_date_idx ON op_records (shift_date DESC);

-- Rastro de correcciones. Quien analice el dato necesita saber si una fila se
-- edito despues de escribirse y con que autoridad.
CREATE TABLE IF NOT EXISTS op_record_revisions (
    id         BIGSERIAL PRIMARY KEY,
    record_id  BIGINT NOT NULL REFERENCES op_records (id) ON DELETE CASCADE,
    before     JSONB NOT NULL,       -- imagen previa de la fila completa
    source     TEXT NOT NULL         -- dentro de la ventana | correccion forzada
               CHECK (source IN ('operador', 'ingenieria')),
    changed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS op_record_revisions_record_idx
    ON op_record_revisions (record_id, changed_at DESC);
