#!/usr/bin/env bash
# Backup logico diario de la base adanvi.
#
# Solo lectura contra la base: pg_dump con el rol adanvi_ro (sin escrituras,
# sin DDL, sin reinicios). Seguro para correr con el worker adquiriendo.
#
# pg_dump corre DENTRO del contenedor db (misma version que el servidor, sin
# instalar cliente en el host; socket local del contenedor no pide contrasena).
# Requiere acceso a Docker: manualmente se ejecuta con sudo, y el timer de
# systemd corre como root.
#
# Orden de seguridad: crear -> validar -> publicar -> recien entonces aplicar
# retencion. Un backup nuevo solo desplaza a los viejos cuando ya se demostro
# que es legible y contiene las tablas criticas.
#
# Uso manual:   sudo scripts/backup_database.sh
# Programado:   systemd timer adanvi-backup.timer (06:00 America/Bogota)

set -euo pipefail

export TZ=America/Bogota

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$REPO_DIR/docker-compose.yml"
BACKUP_DIR="${ADANVI_BACKUP_DIR:-/home/emolog/backups/adanvi}"
KEEP=3
# Un dump con datos pesa decenas de MB; por debajo de esto algo salio mal.
MIN_BYTES=1048576

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] $*"; }
fail() { log "ERROR: $*"; exit 1; }

[ -r "$REPO_DIR/.env" ] || fail "no se puede leer $REPO_DIR/.env"
set -a
# shellcheck source=/dev/null
. "$REPO_DIR/.env"
set +a

: "${POSTGRES_DB:?falta POSTGRES_DB en .env}"

command -v docker >/dev/null 2>&1 || fail "docker no esta disponible"
[ -r "$COMPOSE_FILE" ] || fail "no se puede leer $COMPOSE_FILE"

# exec dentro del contenedor db; -T porque no hay terminal y se pipea.
db_exec() { docker compose -f "$COMPOSE_FILE" exec -T db "$@"; }

docker compose -f "$COMPOSE_FILE" ps -q --status running db >/dev/null 2>&1 \
    || fail "el contenedor db no esta corriendo"

mkdir -p "$BACKUP_DIR"

# Un solo backup a la vez, aunque el timer y una ejecucion manual coincidan.
exec 9>"$BACKUP_DIR/.backup.lock"
flock -n 9 || fail "otro backup ya esta en curso"

stamp="$(date +%Y-%m-%d_%H%M)"
final="$BACKUP_DIR/adanvi_${stamp}.dump"
manifest="${final}.manifest"
tmp="$(mktemp "$BACKUP_DIR/.tmp_adanvi_${stamp}.XXXX")"
tmp_manifest="${tmp}.manifest"

cleanup() { rm -f "$tmp" "$tmp_manifest"; }
trap cleanup EXIT

log "inicio: pg_dump $POSTGRES_DB como adanvi_ro (contenedor db)"

# --lock-wait-timeout: ante un lock exclusivo retenido, abortar en 5 s en vez
# de quedar colgado detras de el (estamos en produccion).
if ! db_exec pg_dump -U adanvi_ro -d "$POSTGRES_DB" \
        --format=custom --no-owner --no-privileges --no-tablespaces \
        --lock-wait-timeout=5000 > "$tmp"; then
    fail "pg_dump termino con error; el dump parcial se descarta"
fi

# --- Validacion (antes de publicar y antes de tocar backups viejos) --------
[ -s "$tmp" ] || fail "el dump quedo vacio"

size="$(stat -c %s "$tmp")"
[ "$size" -ge "$MIN_BYTES" ] || fail "dump sospechosamente pequeno: $size bytes"

toc="$(db_exec pg_restore --list < "$tmp")" || fail "pg_restore --list no pudo leer el dump (corrupto)"

for t in tags galleries gallery_series \
         acquisition_gaps readings schema_migrations; do
    grep -Eq "TABLE public ${t}( |\$)" <<<"$toc" || fail "falta la tabla '$t' en el dump"
done
grep -Eq "VIEW public readings_1m( |\$)" <<<"$toc" || fail "falta la vista continua 'readings_1m'"
grep -Eq "VIEW public readings_1h( |\$)" <<<"$toc" || fail "falta la vista continua 'readings_1h'"

sha="$(sha256sum "$tmp" | cut -d' ' -f1)"

{
    echo "fecha: $(date -Is)"
    echo "base: $POSTGRES_DB"
    echo "usuario: adanvi_ro"
    echo "pg_dump: $(db_exec pg_dump --version)"
    echo "archivo: $(basename "$final")"
    echo "bytes: $size"
    echo "sha256: $sha"
    echo "validacion: pg_restore --list OK; tablas criticas presentes"
} > "$tmp_manifest"

# --- Publicar (solo despues de validar) -------------------------------------
mv "$tmp" "$final"
mv "$tmp_manifest" "$manifest"
chmod 600 "$final" "$manifest"
# Ejecutado como root (sudo/systemd): dejar los archivos propiedad de emolog.
if [ "$(id -u)" = "0" ]; then
    chown emolog:emolog "$final" "$manifest"
fi
trap - EXIT

db_exec pg_restore --list < "$final" >/dev/null || fail "el dump publicado no se puede leer"
log "ok: $(basename "$final") ($size bytes, sha256 $sha)"

# --- Retencion: conservar los KEEP mas recientes, y solo tras validar --------
mapfile -t viejos < <(ls -1t "$BACKUP_DIR"/adanvi_*.dump 2>/dev/null | tail -n +$((KEEP + 1)))
for f in "${viejos[@]}"; do
    rm -f -- "$f" "$f.manifest"
    log "retencion: eliminado $(basename "$f")"
done

log "fin: $(ls -1 "$BACKUP_DIR"/adanvi_*.dump | wc -l) dumps en $BACKUP_DIR"
