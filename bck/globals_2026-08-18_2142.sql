--
-- PostgreSQL database cluster dump
--

\restrict jjnaYHGTSEAT9eAIaLSbGpo0gN0BfIdJyGqWmfebDoamR6Llt7XiJjWyQUVKvwm

SET default_transaction_read_only = off;

SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;

--
-- Roles
--

CREATE ROLE adanvi;
ALTER ROLE adanvi WITH SUPERUSER INHERIT CREATEROLE CREATEDB LOGIN REPLICATION BYPASSRLS PASSWORD 'SCRAM-SHA-256$4096:KO3rzaE3/JvnXSj7fndNjQ==$PWpGAoW3RO9NPRKKHqEdIVbYriQMBRlyTQIIuZIlv40=:HaXVgaTu+Qw7gb3iIiLvTiGjK9h4ggJGejOJjW4HCDY=';
CREATE ROLE adanvi_ro;
ALTER ROLE adanvi_ro WITH NOSUPERUSER INHERIT NOCREATEROLE NOCREATEDB LOGIN NOREPLICATION NOBYPASSRLS PASSWORD 'SCRAM-SHA-256$4096:JBnbs/tHAU97KbrOKvGeBQ==$xTlcix2TS7kXfe+SotG4aYDnPh+2r9lZgIGcFRxd3fQ=:YoOYJkOuO1XiOL+mkeLkDf/u452HIxlwaVgK2BDkX0U=';

--
-- User Configurations
--








\unrestrict jjnaYHGTSEAT9eAIaLSbGpo0gN0BfIdJyGqWmfebDoamR6Llt7XiJjWyQUVKvwm

--
-- PostgreSQL database cluster dump complete
--

