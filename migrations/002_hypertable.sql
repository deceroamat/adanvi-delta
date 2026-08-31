-- 002: hypertable de lecturas crudas + compresion + retencion.
--
-- Codigos de status (SMALLINT, no TEXT: ~10 B/fila menos sobre cientos de
-- millones de filas, y comprime mucho mejor):
--   0 = Good        1 = Disconnected   2 = TagError   3 = Stale
--
-- Nota: durante una desconexion del PLC NO se insertan filas aqui; el hueco
-- se registra en acquisition_gaps. El codigo 1 queda reservado.

CREATE TABLE IF NOT EXISTS readings (
    ts      TIMESTAMPTZ NOT NULL,
    tag_id  BIGINT NOT NULL,
    value   DOUBLE PRECISION,     -- NULL = sin dato valido (error de tag)
    status  SMALLINT NOT NULL DEFAULT 0
);

-- Chunks de 1 dia (~8.6 M filas con 100 tags @ 1 Hz). El default de 7 dias
-- daria chunks de 60 M filas que no caben comodos en shared_buffers.
SELECT create_hypertable(
    'readings',
    by_range('ts', INTERVAL '1 day'),
    if_not_exists => true
);

CREATE INDEX IF NOT EXISTS readings_tag_ts_idx ON readings (tag_id, ts DESC);

-- ---------------------------------------------------------------------
-- Compresion: segmentada por tag_id, ordenada por ts. Es la combinacion que
-- permite delta-of-delta en el tiempo y compresion tipo gorilla en el valor.
-- Sin ella, 90 dias de crudo pasarian de ~5 GB a ~70 GB.
--
-- Timescale 2.18 renombro esta API (compress -> columnstore) manteniendo la
-- clasica como alias. Se intenta la clasica y se cae a la nueva; si ninguna
-- existe se aborta en vez de arrancar sin compresion en silencio.
-- ---------------------------------------------------------------------
DO $$
BEGIN
    BEGIN
        ALTER TABLE readings SET (
            timescaledb.compress,
            timescaledb.compress_segmentby = 'tag_id',
            timescaledb.compress_orderby   = 'ts DESC'
        );
    EXCEPTION WHEN OTHERS THEN
        RAISE NOTICE 'sintaxis clasica no disponible (%), probando columnstore', SQLERRM;
        EXECUTE $ddl$
            ALTER TABLE readings SET (
                timescaledb.enable_columnstore,
                timescaledb.segmentby = 'tag_id',
                timescaledb.orderby   = 'ts DESC'
            )
        $ddl$;
    END;
END
$$;

-- Se comprime a partir de 2 dias: los ultimos 2 dias quedan "calientes" y sin
-- comprimir para el zoom fino y la escritura continua.
DO $$
BEGIN
    PERFORM add_compression_policy('readings', INTERVAL '2 days', if_not_exists => true);
EXCEPTION WHEN undefined_function THEN
    PERFORM add_columnstore_policy('readings', after => INTERVAL '2 days', if_not_exists => true);
END
$$;

-- Retencion cruda: 90 dias (~5.6 GB con compresion). Para cambiarla ver README.
SELECT add_retention_policy('readings', INTERVAL '90 days', if_not_exists => true);
