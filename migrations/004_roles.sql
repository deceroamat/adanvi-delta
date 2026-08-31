-- 004: rol de solo lectura.
--
-- Gancho para el asistente MCP de v2. La defensa real contra un SQL destructivo
-- generado por un LLM es un rol sin permiso de escritura, no un allowlist en la
-- capa de aplicacion. Se crea NOLOGIN: v2 le asignara password y LOGIN.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'adanvi_ro') THEN
        CREATE ROLE adanvi_ro NOLOGIN;
    END IF;
END
$$;

GRANT USAGE ON SCHEMA public TO adanvi_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO adanvi_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO adanvi_ro;

-- Las vistas continuas viven fuera de public.
GRANT USAGE ON SCHEMA _timescaledb_internal TO adanvi_ro;
