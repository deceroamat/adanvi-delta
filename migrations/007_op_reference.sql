-- Referencia de produccion de la bobina (el tipo de papel: K40 = kraft 40 g/m2).
--
-- Se admite NULL a proposito: cuando se anadio esta columna la tabla ya tenia
-- bobinas registradas a mano, y NULL es lo que distingue "no se registro" de un
-- valor inventado. El formulario si la exige de aqui en adelante.
--
-- Sin CHECK contra la lista de referencias conocidas: el formulario permite
-- teclear una fuera de lista ("Otro"), y un CHECK bloquearia justo ese caso.
ALTER TABLE op_records
    ADD COLUMN IF NOT EXISTS reference TEXT
    CHECK (reference IS NULL OR length(reference) BETWEEN 1 AND 20);
