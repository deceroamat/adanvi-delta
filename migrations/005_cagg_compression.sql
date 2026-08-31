-- 005: compresion de los agregados continuos.
--
-- Medido sobre datos reales: readings_1m ocupa ~148 B/fila, o sea ~7.8 GB al ano
-- con 100 tags. No es despreciable, y comprime igual de bien que el crudo.
-- Se comprime a partir de 30 dias para que el ultimo mes siga siendo barato de
-- refrescar (un chunk comprimido es mas caro de re-materializar).

DO $$
BEGIN
    BEGIN
        ALTER MATERIALIZED VIEW readings_1m SET (
            timescaledb.compress,
            timescaledb.compress_segmentby = 'tag_id',
            timescaledb.compress_orderby   = 'bucket DESC'
        );
        ALTER MATERIALIZED VIEW readings_1h SET (
            timescaledb.compress,
            timescaledb.compress_segmentby = 'tag_id',
            timescaledb.compress_orderby   = 'bucket DESC'
        );
    EXCEPTION WHEN OTHERS THEN
        RAISE NOTICE 'sintaxis clasica no disponible (%), probando columnstore', SQLERRM;
        EXECUTE $ddl$
            ALTER MATERIALIZED VIEW readings_1m SET (
                timescaledb.enable_columnstore,
                timescaledb.segmentby = 'tag_id',
                timescaledb.orderby   = 'bucket DESC')
        $ddl$;
        EXECUTE $ddl$
            ALTER MATERIALIZED VIEW readings_1h SET (
                timescaledb.enable_columnstore,
                timescaledb.segmentby = 'tag_id',
                timescaledb.orderby   = 'bucket DESC')
        $ddl$;
    END;
END
$$;

DO $$
BEGIN
    PERFORM add_compression_policy('readings_1m', INTERVAL '30 days', if_not_exists => true);
    PERFORM add_compression_policy('readings_1h', INTERVAL '90 days', if_not_exists => true);
EXCEPTION WHEN undefined_function THEN
    PERFORM add_columnstore_policy('readings_1m', after => INTERVAL '30 days', if_not_exists => true);
    PERFORM add_columnstore_policy('readings_1h', after => INTERVAL '90 days', if_not_exists => true);
END
$$;
