-- 008: cambio de PLC. Allen-Bradley por EtherNet/IP -> Delta AS-200 por Modbus/TCP.
--
-- Con CIP un tag se identificaba por su NOMBRE y el tipo se descubria al leer.
-- Modbus no tiene nombres ni tipos en el cable: hay que declarar area, direccion
-- y como interpretar los registros. Por eso `value_type` (que se rellenaba solo)
-- desaparece y entra el direccionamiento explicito.
--
-- Se retira ademas el formulario de operacion, que sale del alcance.
--
-- ATENCION: esta migracion DESTRUYE el historico existente, por decision
-- expresa. Los datos venian de otro PLC y los tags viejos no tienen direccion
-- Modbus que valga. Hacer backup antes (scripts/backup_database.sh).

DROP TABLE IF EXISTS op_record_revisions;
DROP TABLE IF EXISTS op_records;

-- Cuenta nueva. El DELETE de tags arrastra gallery_series por CASCADE: las
-- galerias sobreviven vacias y hay que volver a componerlas.
TRUNCATE readings;
DELETE FROM tags;

-- NO se reinicia la secuencia de tags.id a proposito. Los agregados continuos
-- conservan buckets materializados de los tag_id viejos hasta que expire su
-- retencion (1 y 5 anos); si la secuencia volviera a 1, un tag nuevo heredaria
-- en las ventanas largas el historico de otro. Dejando que BIGSERIAL siga
-- contando, esas filas quedan simplemente inalcanzables.

ALTER TABLE tags DROP COLUMN IF EXISTS value_type;

ALTER TABLE tags
    -- Esclavo Modbus. Normalmente 1, pero un gateway serie puede exponer varios.
    ADD COLUMN IF NOT EXISTS unit_id SMALLINT NOT NULL DEFAULT 1
        CHECK (unit_id BETWEEN 0 AND 247),
    ADD COLUMN IF NOT EXISTS area TEXT NOT NULL DEFAULT 'holding'
        CHECK (area IN ('coil', 'discrete', 'holding', 'input')),
    ADD COLUMN IF NOT EXISTS address INTEGER NOT NULL DEFAULT 0
        CHECK (address BETWEEN 0 AND 65535),
    ADD COLUMN IF NOT EXISTS data_type TEXT NOT NULL DEFAULT 'int16'
        CHECK (data_type IN ('bit', 'int16', 'uint16', 'int32', 'uint32', 'float32')),
    -- Orden de palabra de los tipos de 32 bits. Leerlo al reves no da error:
    -- da un numero plausible y falso, que es el fallo peor en un historiador.
    ADD COLUMN IF NOT EXISTS word_order TEXT NOT NULL DEFAULT 'big'
        CHECK (word_order IN ('big', 'little')),
    -- valor = crudo * scale + value_offset. El PLC publica enteros escalados
    -- (una temperatura x10) y el historiador debe guardar unidades de ingenieria.
    -- Se llama value_offset y no offset porque OFFSET es palabra reservada.
    ADD COLUMN IF NOT EXISTS scale DOUBLE PRECISION NOT NULL DEFAULT 1
        CHECK (scale <> 0),
    ADD COLUMN IF NOT EXISTS value_offset DOUBLE PRECISION NOT NULL DEFAULT 0;

-- Coherencia area <-> tipo: un float32 sobre una bobina no es un desliz que se
-- note al mirar, produce un valor creible y equivocado. Se bloquea en la tabla.
ALTER TABLE tags
    ADD CONSTRAINT tags_area_tipo_coherentes CHECK (
        (area IN ('coil', 'discrete') AND data_type = 'bit')
        OR (area IN ('holding', 'input') AND data_type <> 'bit')
    );

-- El acquirer recarga el catalogo ordenado por direccion para que el agrupador
-- de bloques reciba los tags ya en orden.
CREATE INDEX IF NOT EXISTS tags_direccion_idx ON tags (unit_id, area, address);
