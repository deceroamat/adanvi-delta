# ADANVI by emolog

Historiador de tendencias para PLC Allen-Bradley: adquisición continua de tags,
almacenamiento time-series con retención acotada y galerías de tendencias con
navegación temporal en vivo e histórica.

Dimensionado para ~100 tags a 1 Hz sobre un host modesto (i5-6500T, 8 GB RAM,
SSD). Sin framework de frontend, sin build step.

---

## Puesta en marcha

```bash
cp .env.example .env      # ajusta PLC_IP y POSTGRES_PASSWORD
docker-compose up -d
```

La app queda en <http://localhost:8000>. Las migraciones se aplican solas al
arrancar; no hay paso manual.

Si tu usuario no está en el grupo `docker`:

```bash
sudo usermod -aG docker $USER && newgrp docker
```

### Desarrollo local

```bash
docker-compose up -d db                       # solo la base
uv sync
uv run python -m src                          # app en el host
uv run pytest                                 # tests
uv run python scripts/seed.py --days 35 --tags 12 --interval 5
```

Con `DATABASE_URL` apuntando a `localhost:5432` en tu `.env`.

---

## Modelo de datos

| Objeto | Qué es |
|---|---|
| `tags` | Catálogo. **Única fuente de verdad de lo que se pollea.** No hay YAML ni CSV. |
| `readings` | Hypertable cruda, un punto por tag y por ciclo. |
| `readings_1m` / `readings_1h` | Agregados continuos con `avg`, `min`, `max`, `n`, `last`. |
| `acquisition_gaps` | Intervalos sin adquisición (PLC caído, app reiniciada). |
| `galleries` / `gallery_series` | Galerías y la configuración de cada serie. |
| `op_records` | Formulario de operación: una fila por bobina, escrita a mano. |
| `op_record_revisions` | Imagen previa de cada corrección hecha sobre `op_records`. |

### Política de retención

| Capa | Bucket | Retención | Compresión | Disco (100 tags @ 1 Hz) |
|---|---|---|---|---|
| `readings` | 1 s | 90 días | chunks > 2 días | ~13.5 GB |
| `readings_1m` | 1 min | 1 año | chunks > 30 días | ~2.2 GB |
| `readings_1h` | 1 hora | 5 años | chunks > 90 días | ~0.3 GB |

Chunks de 1 día (~8.6 M filas con 100 tags). **Total ≈ 16 GB**, holgado sobre los
~200 GB del host de referencia.

Cifras medidas sobre datos reales en este despliegue, no estimadas: 137 B/fila en
crudo con ratio de compresión **9.4x** (→ 126 MB/día), y 148 B/fila en el agregado
de 1 minuto con ratio **4.6x** (los agregados comprimen peor porque guardan cinco
columnas de flotantes independientes en vez de una sola serie delta-codificada).

Son un techo conservador: se midieron con datos sintéticos con ruido gaussiano,
y una señal de proceso real es más suave y comprime mejor.

Para cambiar una retención hay que reemplazar la policy; editar el `.env` no
basta, porque las policies viven en la base:

```sql
SELECT remove_retention_policy('readings');
SELECT add_retention_policy('readings', INTERVAL '120 days');
```

### Por qué los huecos NO se guardan como ceros

Cuando el PLC no responde, ADANVI **no escribe filas**: registra el intervalo en
`acquisition_gaps` y el gráfico dibuja una banda roja "SIN DATO" con la línea
cortada.

Escribir `0.0` —como hacía el prototipo— tiene tres problemas: destruye el
autoescalado (una temperatura de 180 °C que cae a 0 aplana toda la variación
real), envenena de forma irreversible los agregados continuos, y hace
indistinguible "PLC caído" de "la variable vale cero de verdad". Además, una
caída de 8 horas generaría 2.88 M de filas sin información.

Un tag que falla individualmente con el PLC sano se guarda con `value = NULL` y
`status = 2`: solo se corta esa serie, sin afectar a la escala de las demás.

---

## Navegación temporal

No hay botón "Pausar". El estado de la vista es uno solo:

```
{ desde, hasta, siguiendo }
```

Cualquier interacción con el gráfico apaga `siguiendo`, y el botón **LIVE** es la
única forma de volver a encenderlo (haciendo backfill desde la base, sin dejar
hueco).

| Acción | Efecto |
|---|---|
| Arrastrar el gráfico | Desplazar en el tiempo |
| Rueda | Zoom sobre el instante bajo el cursor |
| Shift + arrastre | Zoom a la selección |
| Doble clic | Volver al ancho de ventana nombrado |
| `←` / `→` | Desplazar media ventana · con Shift, una entera |
| `Inicio` | Volver a LIVE |
| `T` | Plegar / desplegar la tabla |
| `F` | Gráfico a pantalla completa |
| `[‹]` `[›]` | Desplazar media ventana |

Los botones y las flechas mueven lo mismo a propósito: son la misma acción, y
tener dos magnitudes distintas hacía impredecible cuánto te desplazabas.

**El rango visible va en la URL**, así que F5 restaura la vista exacta, las
flechas atrás/adelante del navegador funcionan y el enlace es compartible. La
configuración de las series (color, eje, escala) vive en la base de datos.

Al quedarse la vista quieta se precargan en segundo plano la ventana anterior y
la siguiente, de modo que la pulsación siguiente de `‹` se pinta desde memoria y
no desde la red. Lo que sí está pedido y aún no ha llegado se dibuja como una
banda gris **Cargando…**, nunca en blanco y nunca con el rojo de los huecos:
"todavía no lo tengo" y "aquí no hubo dato" no se pueden confundir.

### Resolución

El servidor elige capa y bucket a partir de cuántos puntos caben en el ancho del
gráfico, no de umbrales fijos, y respeta un presupuesto de filas leídas por
consulta: si una ventana de 30 días con 20 series fuese a escanear ~860 k filas,
sube al agregado horario. Un pan instantáneo con 720 puntos vale más que uno
lento con 1440.

Al alejar el zoom se dibuja la **banda mín-máx** del intervalo detrás de la línea
de promedio, para que un pico de dos segundos siga siendo visible en una ventana
de un mes.

### Ventanas

`30s`, `15m`, `1h`, `8h`, `1d`, `2w`, `1M`. Minúscula `m` es minutos, mayúscula
`M` es meses (30 días). El parser vive **solo en el servidor**.

---

## Configuración de series

Cada fila de la tabla bajo el gráfico:

| Campo | Para qué |
|---|---|
| Visible | Ocultar sin quitar de la galería |
| Color | Paleta de 8 tonos distinguibles sobre fondo oscuro |
| **Eje** | Series con el mismo grupo comparten escala Y. `auto` agrupa por unidad. |
| Escala | `auto` o `manual` con Y mín / Y máx |
| Unidad / Decimales | Formato en eje, cursor y CSV |
| Interpolación | Línea o escalón (los digitales siempre escalón) |
| Grosor | 1–4 px |
| Agregación | Qué representa cada bucket al alejar: promedio, mín, máx o último |

Columnas de solo lectura calculadas en el navegador sobre la ventana visible:
**Cursor** (valor bajo el crosshair), **Último**, **Mín**, **Máx**, **Prom**.

### Lectura del crosshair

Junto al cursor aparece un recuadro con el instante y el valor de cada serie, y
viaja con él. La versión anterior dejaba esa lectura anclada abajo al centro del
gráfico, así que inspeccionar un punto obligaba a apartar la vista de él.

El recuadro se autolimita —un máximo de 12 series, y el resto se cuenta como
`+N más en la tabla`— porque el área de trazo sigue siendo lo que hay que
maximizar: una galería de 30 tags taparía justo el gráfico que se quiere leer.
La tabla inferior no cambia de papel: es la lectura completa y persistente, y la
única cuando la mano no está sobre el gráfico.

### Qué series comparten escala

Compartir escala es compartir marco de referencia, y eso exige estar de acuerdo
en los límites. Con `Eje = auto` la regla completa es: **misma unidad *y* mismos
límites**. Dos series en `%` con rangos manuales 0–10 y 0–100 no pueden convivir
en un eje —una de las dos acabaría dibujada fuera del área y desaparecería del
gráfico—, así que cada una recibe el suyo. Si los límites coinciden, o si ambas
autoescalan, siguen compartiendo eje: son comparables y no hay motivo para
separarlas.

Un grupo de eje **explícito** es una orden y se respeta aunque los límites no
coincidan: ahí la escala toma la unión de los rangos manuales, ensanchada si
hace falta para que quepan los datos de las series en `auto`.

El invariante, en cualquier combinación: ninguna configuración puede dejar una
serie fuera del área de trazo. La regla vive en `src/static/js/scales.js` y está
cubierta por `tests/frontend.test.js`.

Solo se dibujan dos reglas numéricas, izquierda y derecha; los demás grupos
conservan su escala aunque no muestren regla, porque el ancho del trazo vale más
que una tercera columna de números. Los valores exactos están en la tabla.

Los tags marcados como `digital` se dibujan en carriles al pie del área de trazo,
nunca en el eje analógico: un booleano 0/1 aplastaría la escala de una
temperatura.

---

## API

| Método | Ruta | Notas |
|---|---|---|
| GET | `/api/health` | `worker_alive`, `plc_connected`, jitter p95, cola, gap abierto |
| GET | `/api/live/snapshot` | Últimos valores en memoria |
| GET/POST/PATCH/DELETE | `/api/tags` | `DELETE` desactiva; `?purge=true` borra también el histórico |
| GET | `/api/history` | `tags`, `window` o `from`/`to`, `max_points` |
| GET | `/api/history/window` | Valida un token de ventana |
| GET/POST/PATCH/DELETE | `/api/galleries` | Sin límite duro (`MAX_GALLERIES=0`) |
| PUT | `/api/galleries/{id}/series` | Reemplazo atómico |
| GET | `/api/export.csv` | Ventana visible en la resolución que se está viendo |
| GET/POST/PATCH/DELETE | `/api/forms/operation` | Formulario de operación; `?force=true` corrige fuera de plazo |
| GET | `/api/forms/operation.csv` | Registros del día, con el perfil aplanado a 10 columnas |
| WS | `/ws/live` | Ticks columnares + eventos `gap_open` / `gap_close` |

---

## Formularios

Lo que el PLC no puede saber y alguien tiene que escribir. En `/forms` hay tres:
**Operación** (implementado), **Laboratorio** e **Ingeniería** (pendientes).

El de operación se llena bobina a bobina desde el panel de planta: la fila de columnas
está siempre visible y lista, sin ventanas emergentes, porque sustituye a una hoja de
papel. Guarda referencia, consecutivo, horas, velocidad, el perfil de peso de 10 zonas,
peso base, peso de bobina, rupturas y tipo.

Tres decisiones que conviene conocer antes de tocarlo:

- **La referencia admite valores fuera de lista.** Las conocidas están en `REFERENCES`
  (`src/api/forms.py`) —K40, K42, K45, K48, K50, K60, K90, K100, K111— y el formulario las
  pide de ahí por el API, no las repite en el HTML. La opción «Otro…» deja teclear una
  nueva, así que **no hay `CHECK` contra esa lista**; el valor se normaliza a mayúsculas
  para que `k40` y `K40` no cuenten como dos referencias distintas. En la tabla la columna
  es *nullable*: cuando se añadió ya había bobinas registradas a mano, y `NULL` distingue
  «no se registró» de un valor inventado. El formulario sí la exige al crear.

- **Se guardan instantes reales, no solo la hora.** La UI pide `HH:MM` y el servidor los
  compone con la fecha del día en `TZ`; si la hora de fin no es posterior a la de inicio,
  la bobina cruzó la medianoche y el fin cae al día siguiente. Guardar `TIMESTAMPTZ` es lo
  que permite cruzar cada bobina con el histórico de tendencias por rango de tiempo.
- **La corrección es por plazo, no por rol.** El operador tiene `OP_EDIT_WINDOW_MIN`
  minutos (30 por defecto) contados desde `created_at` —editar no reinicia el reloj—; luego
  la fila queda bloqueada. `?force=true` la desbloquea y queda registrado en
  `op_record_revisions` como `source='ingenieria'`. **No es un control de acceso**: mientras
  no haya usuarios es solo una convención, y cuando los haya debe pasar a ser una
  comprobación de permiso.

---

## Arquitectura del worker

Tres hilos desacoplados por colas acotadas (drop-oldest):

1. **Acquirer** — malla `monotonic()` sin deriva, timestamp tomado antes del
   read, multi-read CIP en una llamada. **Nunca toca la base de datos.**
2. **Writer** — único con acceso a Postgres. Lotes por `COPY`, gestión de
   `acquisition_gaps` y recarga del catálogo de tags.
3. **Broadcaster** — reparte a los WebSockets, con cola propia por conexión.

Separar la escritura de la adquisición es lo que impide que un checkpoint de
Postgres o un job de compresión desvíe la cadencia de lectura del PLC.

`tags.last_value` no existe a propósito: actualizar el catálogo 100 veces por
segundo generaría bloat permanente. El estado vivo está en memoria y se expone
por `/api/health` y `/api/live/snapshot`.

---

## Operación

- **Sincroniza la hora por NTP.** En un historiador, un reloj desviado corrompe
  el histórico en silencio. Todo se almacena en UTC; la presentación es
  `America/Bogotá`.
- **Sin autenticación.** Pensado para LAN industrial aislada. No lo expongas a
  internet sin un proxy inverso con auth delante.
- **Backup diario automático** a las 06:00 (America/Bogota) vía systemd
  (`adanvi-backup.timer` → `scripts/backup_database.sh`): `pg_dump` completo en
  formato custom con el rol de solo lectura `adanvi_ro`, validado con
  `pg_restore --list` y checksum antes de publicar. Conserva los 3 dumps más
  recientes en `~/backups/adanvi` y elimina el cuarto solo si el nuevo pasó la
  validación. Los roles globales (`globals`) no se respaldan con `adanvi_ro`;
  conservar aparte el último `pg_dumpall --globals-only` hecho con el admin.
  Unidades en `deploy/systemd/`; instalación:
  `sudo cp deploy/systemd/adanvi-backup.{service,timer} /etc/systemd/system/ &&
  sudo systemctl daemon-reload && sudo systemctl enable --now adanvi-backup.timer`.

### Restauración completa desde `bck/`

Para reconstruir la base en un volumen nuevo, el orden de TimescaleDB es
importante. Ejecutar esto detiene la aplicación y reemplaza la base `adanvi`:

```bash
docker-compose up -d db
docker-compose stop adanvi

# El rol adanvi ya existe porque lo crea Docker; el "already exists" de esa
# línea es esperado y las sentencias ALTER ROLE siguientes sí se aplican.
docker-compose exec -T db psql -X -U adanvi -d postgres < bck/globals_2026-08-18_2142.sql

docker-compose exec -T db psql -X -U adanvi -d postgres \
  -c "DROP DATABASE adanvi WITH (FORCE)"
docker-compose exec -T db psql -X -U adanvi -d postgres \
  -c "CREATE DATABASE adanvi OWNER adanvi"
docker-compose exec -T db psql -X -U adanvi -d adanvi \
  -c "CREATE EXTENSION IF NOT EXISTS timescaledb"
docker-compose exec -T db psql -X -U adanvi -d adanvi \
  -c "SELECT timescaledb_pre_restore()"
docker-compose exec -T db pg_restore --no-owner --exit-on-error \
  -U adanvi -d adanvi < bck/adanvi_2026-08-20_0600.dump
docker-compose exec -T db psql -X -U adanvi -d adanvi \
  -c "SELECT timescaledb_post_restore()"

# Restaura los permisos del usuario externo de solo lectura.
uv run python scripts/grant_ro.py
docker-compose up -d
```

El dump `*.dump` está excluido del versionado por `.gitignore`; para recuperar
desde un clon hay que conservar y copiar `bck/` junto al proyecto. El archivo
`globals_*.sql` contiene hashes de credenciales y debe tratarse como secreto.

### Acceso externo a la base (pgAdmin, DBeaver, notebooks)

Postgres se publica **solo en el loopback** del host (`127.0.0.1:5430` en
`docker-compose.yml`), a propósito: la LAN de planta no debe poder hablar con la
base. Para llegar desde otro equipo se usa el tailnet, con `tailscaled` haciendo
de puerta:

```bash
sudo tailscale serve --bg --tcp 5430 tcp://127.0.0.1:5430
```

La configuración persiste entre reinicios, y el bind de Docker no cambia. Se
prefirió esto a publicar el puerto en la IP de Tailscale porque `docker` y
`tailscaled` no tienen orden garantizado en systemd: en un arranque en frío el
contenedor podría fallar el bind contra una interfaz que aún no existe.

Después, habilitar el rol de solo lectura (existe desde la migración 004, pero
`NOLOGIN` y sin contraseña):

```bash
uv run python scripts/grant_ro.py     # lee ADANVI_RO_PASSWORD del .env
```

Conexión desde la otra máquina: host = la IP de Tailscale del servidor, puerto
`5432`, base `adanvi`, usuario **`adanvi_ro`**.

`adanvi` es superusuario y se reserva para mantenimiento. Un `DELETE FROM
readings` mal escrito desde un cliente gráfico destruye el histórico de forma
irrecuperable, que es justo lo único que esta app existe para custodiar.

**`pg_hba.conf` no sirve como allowlist por IP aquí.** La conexión llega por
`tailscaled → docker-proxy → contenedor`, así que Postgres ve como origen la
gateway del bridge (`172.x`) y no la IP real del cliente. El control de acceso lo
dan Tailscale (WireGuard + ACLs del tailnet) y el rol de solo lectura — no hay una
segunda barrera por IP, conviene no suponerla.

El mismo rol `adanvi_ro` es el gancho previsto para el asistente de consultas en
lenguaje natural de v2.

## Fuera de alcance en v1

Asistente MCP/LLM, autenticación y RBAC, escritura de setpoints al PLC, fórmulas
de proceso, alta disponibilidad, tema claro, compresión por excepción
(deadband), anotaciones de evento.
