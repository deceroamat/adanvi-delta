-- 003: agregados continuos.
--
-- Jerarquia raw (1 s) -> _1m -> _1h. Se descarto un _2m: sobre _1m es solo 2x
-- de reduccion, gasta disco y no compra nada. _1h son ~876 k filas/ano.
--
-- Ambos guardan avg + min + max: downsamplear solo con avg esconde los picos,
-- que es justo lo que se busca al diagnosticar un proceso. El chart pinta la
-- banda min-max detras de la linea de avg.
--
-- WHERE value IS NOT NULL: un bucket sin ningun dato valido no produce fila,
-- de modo que el chart dibuja un hueco en vez de inventar un valor.
--
-- materialized_only = false (real-time aggregation): la vista une lo
-- materializado con lo que aun no se ha refrescado, para que una ventana de
-- 1 dia recien cargada llegue hasta "ahora" sin esperar al refresh.

CREATE MATERIALIZED VIEW IF NOT EXISTS readings_1m
WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS
SELECT
    time_bucket(INTERVAL '1 minute', ts) AS bucket,
    tag_id,
    avg(value)        AS avg,
    min(value)        AS min,
    max(value)        AS max,
    count(value)      AS n,
    last(value, ts)   AS last
FROM readings
WHERE value IS NOT NULL
GROUP BY bucket, tag_id
WITH NO DATA;

CREATE MATERIALIZED VIEW IF NOT EXISTS readings_1h
WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS
SELECT
    time_bucket(INTERVAL '1 hour', ts) AS bucket,
    tag_id,
    avg(value)        AS avg,
    min(value)        AS min,
    max(value)        AS max,
    count(value)      AS n,
    last(value, ts)   AS last
FROM readings
WHERE value IS NOT NULL
GROUP BY bucket, tag_id
WITH NO DATA;

-- ---------------------------------------------------------------------
-- Refresh policies.
-- start_offset acotado a proposito: el refresco nunca debe alcanzar la zona
-- que la retention policy ya borro, o el agregado se vaciaria.
-- ---------------------------------------------------------------------
SELECT add_continuous_aggregate_policy('readings_1m',
    start_offset      => INTERVAL '30 minutes',
    end_offset        => INTERVAL '1 minute',
    schedule_interval => INTERVAL '1 minute',
    if_not_exists     => true);

SELECT add_continuous_aggregate_policy('readings_1h',
    start_offset      => INTERVAL '6 hours',
    end_offset        => INTERVAL '1 hour',
    schedule_interval => INTERVAL '10 minutes',
    if_not_exists     => true);

-- Retenciones de los agregados. Sobreviven al borrado del raw: los buckets ya
-- materializados no se pierden cuando la retention policy elimina los chunks.
SELECT add_retention_policy('readings_1m', INTERVAL '365 days', if_not_exists => true);
SELECT add_retention_policy('readings_1h', INTERVAL '1825 days', if_not_exists => true);

-- No se crean indices a mano: con create_group_indexes activo (default),
-- Timescale ya genera (tag_id, bucket DESC) sobre cada hypertable materializada.
