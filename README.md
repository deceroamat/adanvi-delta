# ADANVI by emolog

Historiador de tendencias para PLC **Delta AS-200** por **Modbus/TCP**:
adquisición continua de tags, almacenamiento time-series con retención acotada y
galerías de tendencias con navegación temporal en vivo e histórica.

Dimensionado para ~100 tags a 1 Hz sobre un host modesto (i5-6500T, 8 GB RAM,
SSD). Sin framework de frontend, sin build step.

## Puesta en marcha

```bash
cp .env.example .env      # ajusta PLC_IP, PLC_PORT y POSTGRES_PASSWORD
docker-compose up -d
```

La app queda en <http://localhost:8000> y las migraciones se aplican solas al
arrancar. Si tu usuario no está en el grupo `docker`:
`sudo usermod -aG docker $USER && newgrp docker`.

### Desarrollo local

```bash
docker-compose up -d db                       # solo la base
uv sync
uv run python -m src                          # app en el host
uv run pytest                                 # 87 tests, no requieren base de datos
deno test tests/frontend.test.js              # 38 tests de la lógica de cliente
uv run python scripts/seed.py --days 35 --tags 12 --interval 5
uv run python scripts/modbus_sim.py           # esclavo Modbus falso, para probar sin PLC
```

Con `DATABASE_URL` apuntando a **`localhost:5430`** en tu `.env` — no 5432: el
contenedor publica Postgres en `127.0.0.1:5430` para no chocar con un Postgres
del host ni escuchar en la LAN. Los tests de cliente cubren `cache.js`,
`scales.js` y `viewport.js` (los módulos sin DOM) y necesitan
[Deno](https://deno.com).

## Modelo de datos

| Objeto | Qué es |
|---|---|
| `tags` | Catálogo: qué se pollea y de qué dirección Modbus. **Única fuente de verdad.** No hay YAML ni CSV. |
| `readings` | Hypertable cruda, un punto por tag y por ciclo. |
| `readings_1m` / `readings_1h` | Agregados continuos con `avg`, `min`, `max`, `n`, `last`. |
| `acquisition_gaps` | Intervalos sin adquisición (PLC caído, app reiniciada). |
| `galleries` / `gallery_series` | Galerías y la configuración de cada serie. |

### Política de retención

| Capa | Bucket | Retención | Compresión | Disco (100 tags @ 1 Hz) |
|---|---|---|---|---|
| `readings` | 1 s | 90 días | chunks > 2 días | ~13.5 GB |
| `readings_1m` | 1 min | 1 año | chunks > 30 días | ~2.2 GB |
| `readings_1h` | 1 hora | 5 años | chunks > 90 días | ~0.3 GB |

Chunks de 1 día (~8.6 M filas con 100 tags). **Total ≈ 16 GB** sobre los ~200 GB
del host. Cifras medidas: 137 B/fila en crudo con ratio **9.4x** (→ 126 MB/día) y
148 B/fila en el agregado de 1 min con ratio **4.6x** (comprime peor: cinco
columnas de flotantes en vez de una serie delta-codificada). El disco incluye la
**ventana caliente sin comprimir** (2 días × 1.18 GB más 88 comprimidos, ≈ 13.4 GB),
que es lo que hace que el crudo no sean 126 MB × 90.

Para cambiar una retención hay que reemplazar la policy; editar el `.env` no
basta, porque las policies viven en la base:

```sql
SELECT remove_retention_policy('readings');
SELECT add_retention_policy('readings', INTERVAL '120 days');
```

### Por qué los huecos NO se guardan como ceros

Cuando el PLC no responde, ADANVI **no escribe filas**: registra el intervalo en
`acquisition_gaps` y el gráfico dibuja una banda roja "SIN DATO" con la línea
cortada. Escribir `0.0` —como hacía el prototipo— destruye el autoescalado (una
temperatura de 180 °C que cae a 0 aplana toda la variación real), envenena de
forma irreversible los agregados y hace indistinguible "PLC caído" de "la
variable vale cero de verdad"; una caída de 8 horas generaría además 2.88 M de
filas sin información.

Un tag que falla individualmente con el PLC sano se guarda con `value = NULL` y
`status = 2`: solo se corta esa serie, sin afectar a la escala de las demás.

## Navegación temporal

No hay botón "Pausar". El estado de la vista es uno solo: `{ desde, hasta,
siguiendo }`. Cualquier interacción apaga `siguiendo`, y **LIVE** es la única
forma de reencenderlo (con backfill desde la base).

| Acción | Efecto |
|---|---|
| Arrastrar el gráfico | Desplazar en el tiempo |
| Rueda | Zoom sobre el instante bajo el cursor |
| Shift + arrastre | Zoom a la selección |
| Doble clic · `[⟲]` | Volver al ancho de ventana nombrado |
| `←` / `→` · `[‹]` `[›]` | Desplazar media ventana · con Shift, una entera |
| `Inicio` | Volver a LIVE |
| `T` / `F` | Plegar la tabla / gráfico a pantalla completa |
| `[📅 Rango]` · `[⇩ CSV]` | Ir a un rango concreto · descargar la ventana visible |
| Asa bajo el gráfico | Arrastrar redimensiona la tabla · clic la pliega |

Botones y flechas mueven lo mismo a propósito. **El rango visible va en la URL**:
F5 restaura la vista exacta, las flechas del navegador funcionan y el enlace es
compartible; la configuración de las series vive en la base. Con la vista quieta
se precargan las ventanas contiguas, y lo pedido y aún no recibido se dibuja como
banda gris **Cargando…**, nunca con el rojo de los huecos: "todavía no lo tengo"
y "aquí no hubo dato" no se confunden.

### Resolución y ventanas

El servidor elige capa y bucket según cuántos puntos caben en el ancho del
gráfico, no por umbrales fijos, y respeta un presupuesto de filas por consulta:
30 días con 20 series que fuesen a escanear ~860 k filas suben al agregado
horario —un pan instantáneo con 720 puntos vale más que uno lento con 1440—. Al
alejar se dibuja la **banda mín-máx** detrás del promedio, para que un pico de
dos segundos siga siendo visible en una ventana de un mes.

El campo de ancho acepta `<número><s|m|h|d|w|M>`: `30s`, `15m`, `1h`, `1d`, `2w`,
`1M`. Minúscula `m` es minutos, mayúscula `M` es meses (30 días). El parser vive
**solo en el servidor** (`src/timeparse.py`). Como **botones de preset** hay
cuatro: `5m`, `1h`, `8h`, `1d`; cualquier otro ancho se teclea.

## Configuración de series

| Campo | Para qué |
|---|---|
| Visible | Ocultar sin quitar de la galería |
| Color | Paleta de 8 tonos distinguibles sobre fondo oscuro |
| **Eje** | Series con el mismo grupo comparten escala Y. `auto` agrupa por unidad. |
| Escala | `auto` o `manual` con Y mín / Y máx |
| Unidad / Decimales | Formato en eje, cursor y CSV |
| Interpolación | Línea o escalón (los digitales siempre escalón) |
| Grosor | 1–4 px (el `CHECK` de la tabla admite 5; la UI ofrece 4) |
| Agregación | Promedio, mín, máx o último. **Todavía no surte efecto** ⚠ |

Columnas de solo lectura calculadas en el navegador sobre la ventana visible:
**Cursor**, **Último**, **Mín**, **Máx**, **Prom**.

⚠ **Agregación:** se persiste en `gallery_series.agg`, pero hoy no la lee nadie —
`/api/history` devuelve siempre `avg`/`min`/`max` y el gráfico traza el promedio.
Los caggs ya materializan `last`: implementarla es pasar `agg` al API y elegir
columna en `repo_history`.

Junto al cursor viaja un recuadro con el instante y el valor de cada serie,
autolimitado a 12 (`+N más en la tabla`) porque el área de trazo es lo que hay
que maximizar; la tabla inferior es la lectura completa.

### Qué series comparten escala

Compartir escala es compartir marco de referencia, y eso exige acuerdo sobre los
límites. Con `Eje = auto` la regla es **misma unidad *y* mismos límites**: dos
series en `%` con rangos 0–10 y 0–100 no caben en un eje —una quedaría fuera del
área y desaparecería—, así que cada una recibe el suyo. Un grupo **explícito** es
una orden y se respeta aunque no coincidan: la escala toma la unión, ensanchada
si hace falta para los datos de las series en `auto`.

El invariante: **ninguna configuración puede dejar una serie fuera del área de
trazo.** Vive en `src/static/js/scales.js`, cubierto por `tests/frontend.test.js`.
Solo se dibujan dos reglas numéricas —el ancho del trazo vale más que una tercera
columna de números—, aunque los demás grupos conservan su escala. Los `digital`
van en carriles al pie: un booleano 0/1 aplastaría la escala de una temperatura.

## API

| Método | Ruta | Notas |
|---|---|---|
| GET | `/api/health` | `worker_alive`, `plc_connected`, jitter p95, cola, gap abierto |
| GET | `/api/live/snapshot` | Últimos valores en memoria |
| GET/POST/PATCH/DELETE | `/api/tags` | `DELETE` desactiva; `?purge=true` borra el histórico |
| GET | `/api/history` | `tags`, `window` o `from`/`to`, `max_points`. Máx. 50 tags |
| GET | `/api/history/window` | Valida un token de ventana |
| GET/POST/PATCH/DELETE | `/api/galleries` | Sin límite duro (`MAX_GALLERIES=0`) |
| PUT | `/api/galleries/{id}/series` | Reemplazo atómico, hasta 30 series |
| GET | `/api/export.csv` | Ventana visible en la resolución que se está viendo |
| WS | `/ws/live` | Ticks columnares + `gap_open`/`gap_close`. Máx. 50 tags |

Los topes no son arbitrarios: 50 tags acotan el escaneo junto con
`MAX_SCAN_ROWS`, y 30 series es donde el gráfico deja de ser legible antes que
lento.

## Cómo configurar un tag

Modbus no tiene tags con nombre ni tipo en el cable: **todo lo declara el
catálogo**. Un tag no se lee hasta que existe en la tabla `tags` y está activo,
y el worker recarga el catálogo cada ~15 s, así que **no hay que reiniciar nada**
tras darlo de alta.

Se hace en <http://localhost:8000/tags>, o por el API:

```bash
curl -X POST localhost:8000/api/tags -H 'Content-Type: application/json' \
  -d '{"name":"Temp_Zona1","area":"holding","address":4096,"data_type":"int16","scale":0.1}'
```

### Los campos

Solo **Nombre** y **Dirección** son obligatorios; el resto tiene un valor por
defecto sensato. En el formulario, los cuatro últimos van plegados bajo
«Escala, orden de palabra y esclavo».

| Campo | Por defecto | Qué es |
|---|---|---|
| **Nombre** | — | Identificador único. Sale en la leyenda, la tabla y las cabeceras del CSV. Es un nombre tuyo, no una dirección |
| **Dirección** | — | El registro o bit a leer, **0–65535, base 0** (ver abajo) |
| **Área** | `holding` | `holding` (4x, lectura/escritura), `input` (3x, solo lectura), `coil` (0x, bit R/W), `discrete` (1x, bit solo lectura) |
| **Tipo** | `int16` | Cómo interpretar lo leído: `int16`, `uint16`, `int32`, `uint32`, `float32`, `bit`. Los de 32 bits ocupan **dos** registros |
| **Etiqueta** | — | Nombre legible que se muestra en los gráficos. Si falta, se usa el Nombre |
| **Unidad** | — | `°C`, `bar`, `%`… Se usa en el eje, el cursor y el CSV, y agrupa escalas con `Eje = auto` |
| **Escala** | `1` | `valor = crudo × escala + offset`. No puede ser 0 |
| **Offset** | `0` | Desplazamiento tras la escala |
| **Orden de palabra** | `big` | Solo afecta a los tipos de 32 bits. `big` = palabra alta primero (ABCD), `little` = palabras intercambiadas (CDAB) |
| **Esclavo** | `PLC_UNIT_ID` (1) | `unit_id` Modbus. Solo cambia si hay un gateway con varios esclavos detrás |
| **Naturaleza** | `analog` | Cómo se **dibuja**: `analog`, `digital` o `counter` |
| **Decimales** | `2` | Decimales al mostrar el valor |

**La escala existe porque el PLC publica enteros**. Una temperatura de 180.5 °C
viaja como el entero `1805`; con `data_type: int16` y `scale: 0.1` el historiador
guarda `180.5`, que es lo que hay que archivar. Guardar el crudo obligaría a
recordar el factor cada vez que alguien mire una tendencia de hace ocho meses.

### Qué número va en «Dirección»

Es la **dirección de protocolo, empezando en 0** — la misma que usan `pymodbus` y
casi todas las herramientas de diagnóstico. Si tu documentación usa la notación
clásica de cinco dígitos, resta uno y quita el prefijo:

| La documentación dice | Área | Dirección |
|---|---|---|
| `40001` / `4x0001` | `holding` | `0` |
| `40101` | `holding` | `100` |
| `30001` / `3x0001` | `input` | `0` |
| `00001` / `0x0001` | `coil` | `0` |

En un **Delta AS-200** con el mapa Modbus estándar, la notación de dispositivo se
traduce así (mientras la traducción automática no exista, se hace a mano):

| Dispositivo Delta | Área | Dirección | Estado |
|---|---|---|---|
| `D100` (registro de datos) | `holding` | `100` | ✅ Comprobado contra el AS-200 |
| `M114` (relé auxiliar) | `coil` | `114` | ✅ Comprobado contra el AS-200 |
| `X`, `Y`, `S`, `T`, `C`, `SR`, `HC`, `E` | — | — | ⚠️ Sin confirmar: verifica en tu manual |

> Un `float32` en `D70` ocupa `D70` y `D71`: se da de alta **una sola vez** en la
> dirección `70` con `data_type: float32`, no dos tags.

### «Tipo» y «Naturaleza» no son lo mismo

Es la confusión más fácil de cometer y no da ningún error:

- **Tipo** (`data_type`) decide **cómo se decodifican los bytes**. Es del cable.
- **Naturaleza** (`kind`) decide **cómo se dibuja**. Es de la pantalla.

Un bit leído de una bobina con la Naturaleza por defecto (`analog`) se dibuja en
el eje analógico, y un 0/1 al lado de una temperatura de 180 °C aplasta la escala
de las dos. **A un `data_type: bit` ponle siempre `kind: digital`**: así va a un
carril al pie del gráfico, con interpolación en escalón, sin tocar la escala de
nadie.

### Ejemplos

**Booleano** — una bomba en marcha, relé `M100` del Delta:

| Campo | Valor |
|---|---|
| Nombre / Etiqueta | `Bomba1_ON` / `Bomba 1 en marcha` |
| Área · Dirección | `coil` · `100` |
| Tipo · Naturaleza | `bit` · **`digital`** |

```bash
curl -X POST localhost:8000/api/tags -H 'Content-Type: application/json' \
  -d '{"name":"Bomba1_ON","label":"Bomba 1 en marcha","area":"coil","address":100,
       "data_type":"bit","kind":"digital"}'
```

**Entero escalado** — una temperatura que el PLC publica ×10 en `D4096`:

| Campo | Valor |
|---|---|
| Nombre / Etiqueta | `Temp_Zona1` / `Temperatura zona 1` |
| Área · Dirección | `holding` · `4096` |
| Tipo · Unidad | `int16` · `°C` |
| Escala · Decimales | `0.1` · `1` |

```bash
curl -X POST localhost:8000/api/tags -H 'Content-Type: application/json' \
  -d '{"name":"Temp_Zona1","label":"Temperatura zona 1","area":"holding","address":4096,
       "data_type":"int16","unit":"°C","scale":0.1,"decimals":1}'
```

Con `1805` en el registro, el historiador guarda `180.5`.

**Flotante** — un caudal en `D70`+`D71`, ya en unidades de ingeniería:

| Campo | Valor |
|---|---|
| Nombre / Etiqueta | `Caudal` / `Caudal de entrada` |
| Área · Dirección | `holding` · `70` |
| Tipo · Unidad | `float32` · `l/min` |
| Orden de palabra | `big`, o `little` si sale un número absurdo |
| Escala | `1` — el flotante ya viene escalado |

```bash
curl -X POST localhost:8000/api/tags -H 'Content-Type: application/json' \
  -d '{"name":"Caudal","label":"Caudal de entrada","area":"holding","address":70,
       "data_type":"float32","unit":"l/min","word_order":"big"}'
```

Si el valor sale disparatado, casi siempre es el orden de palabra: cambia a
`little` y vuelve a mirar. Leído del revés, un `100.0` se ve como `2.4e-41` y un
`123.45` como `-2.7e+23` — inconfundibles. Pero **no siempre canta**: un `1450.0`
del revés da `2.0042`, que parece una lectura perfectamente razonable. Los dos
órdenes son válidos y **ninguno da error**; solo uno da el número correcto, y por
eso hay que cotejarlo.

### Comprobar que quedó bien

La página de Tags muestra el último valor y su calidad, refrescados cada 2 s:

| Lo que ves | Qué significa |
|---|---|
| Un valor y **Good** | Se está leyendo bien |
| **Sin leer** | Aún no ha pasado un ciclo, o el tag está inactivo |
| **TagError** | El esclavo rechazó el bloque de esa dirección — revísala |
| Todos en blanco y **PLC desconectado** arriba | No es el tag: es la comunicación |

> **Coteja contra ISPSoft antes de dar de alta una tanda.** Un orden de palabra
> invertido o una dirección desplazada **no dan error**: dan un número plausible
> y falso, que es el peor fallo posible en un historiador. La app comprueba que
> el área y el tipo sean coherentes, pero no puede saber si el registro 4096 es
> la temperatura que crees.

Para probar sin el PLC delante, `scripts/modbus_sim.py` levanta un esclavo falso
con el mismo mapa que siembra `seed.py` (`holding 4096+` analógicos ×10,
`holding 4192+` flotantes, `coil 0+` digitales).

### Por qué hay un agrupador de bloques

Una petición Modbus lee un **rango contiguo**: no existe la lectura dispersa que
CIP sí permitía. Con ~100 tags repartidos por el mapa de memoria, pedirlos uno a
uno serían 100 idas y vueltas TCP por ciclo, imposible a 1 Hz.

`plan_blocks` (en `src/plc_client.py`) los agrupa en el mínimo número de
peticiones, respetando los topes del protocolo —125 registros en FC03/FC04, 2000
bits en FC01/FC02— y fusionando huecos de hasta 8 registros: leer unos registros
que nadie usa sale mucho más barato que una segunda ida y vuelta. El plan se
calcula una vez por catálogo, no en cada ciclo.

### Errores: por bloque, no por tag

| Situación | Resultado |
|---|---|
| Socket caído o timeout | Se abre un **gap**: banda roja «SIN DATO», ninguna fila insertada |
| El esclavo rechaza un bloque (dirección ilegal) | Los tags **de ese bloque** van con `NULL` y `status = 2` |
| Bloque correcto | Valor y `status = 0` |

Que el error sea por bloque es del protocolo, no una decisión: Modbus responde
por petición, no por variable, así que una dirección mal tecleada arrastra a sus
vecinas. Aparecen en la página de Tags como «TagError», que es la pista para
encontrarla.

En el log solo se avisa de los **cambios de estado** —una línea al empezar a ser
rechazado, nombrando los tags afectados, y otra al recuperarse—, nunca una por
ciclo: a 1 Hz, un tag mal configurado que nadie corrija enterraría el resto del
log en unas horas.

## Arquitectura del worker

Tres responsabilidades desacopladas por colas acotadas (drop-oldest):

1. **Acquirer** — malla `monotonic()` sin deriva, timestamp tomado antes del
   read, lectura agrupada en bloques contiguos. **Nunca toca la base de datos.**
2. **Writer** — único con acceso a Postgres. Lotes por `COPY`, gestión de
   `acquisition_gaps` y recarga del catálogo de tags.
3. **Broadcaster** (`hub`) — reparte a los WebSockets, con cola por conexión.

Los dos primeros son **hilos** de verdad (`src/__main__.py`); el broadcaster no:
es un puente al event loop de asyncio vía `call_soon_threadsafe`, porque los
WebSockets viven en el loop de uvicorn y un tercer hilo obligaría a sincronizar
lo que asyncio ya serializa. Separar escritura de adquisición es lo que impide
que un checkpoint o un job de compresión desvíe la cadencia de lectura del PLC.

`tags.last_value` no existe a propósito: 100 UPDATE/s sobre una tabla pequeña
generaría bloat permanente. El estado vivo vive en memoria, expuesto por
`/api/health` y `/api/live/snapshot`.

## Operación

- **Sincroniza la hora por NTP.** Un reloj desviado corrompe el histórico en
  silencio. Todo se almacena en UTC; la presentación es `America/Bogotá`.
- **Sin autenticación.** Pensado para LAN industrial aislada. No lo expongas a
  internet sin un proxy inverso con auth delante.

### Backup diario

A las 06:00 vía systemd (`adanvi-backup.timer` → `scripts/backup_database.sh`):
`pg_dump` en formato custom con el rol de solo lectura `adanvi_ro`, validado con
`pg_restore --list` y checksum **antes** de publicar y de tocar los dumps viejos.
Junto a cada uno queda un `.manifest` (fecha, tamaño, versión, sha256) y se
conservan los 3 más recientes. Los roles globales no los cubre `adanvi_ro`:
guardar aparte el último `pg_dumpall --globals-only` del admin.

```bash
sudo cp deploy/systemd/adanvi-backup.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now adanvi-backup.timer
```

**Las rutas están fijadas al despliegue de referencia**, usuario `emolog`:
destino `/home/emolog/backups/adanvi`, `.service` apuntando a
`/home/emolog/adanvi/scripts/`. En otra máquina hay que exportar
`ADANVI_BACKUP_DIR` y editar `ExecStart` y `ReadWritePaths`.

### Agujeros en los agregados continuos

Las policies refrescan con `start_offset => 30 minutes`: si el planificador de
jobs se detiene más de media hora, lo que no llegó a materializarse se queda así
para siempre. El síntoma es una tendencia que salta horas en las ventanas largas
(leen los agregados) mientras las cortas (leen el crudo) están bien.

```bash
uv run python scripts/refresh_caggs.py --from '2026-08-16 15:00' --to '2026-08-17 13:00' --dry-run
```

**Leer los avisos del script antes de usarlo:** refrescar un tramo sin filas en
`readings` **borra** sus buckets materializados, y los agregados sobreviven a la
retención del crudo justamente porque nadie los recalcula. El rango se acota al
crudo existente; ese tope es la única barrera.

### Restauración completa desde `bck/`

El orden de TimescaleDB importa. Esto detiene la app y reemplaza la base:

```bash
docker-compose up -d db && docker-compose stop adanvi

# El rol adanvi ya existe porque lo crea Docker; ese "already exists" es
# esperado y las sentencias ALTER ROLE siguientes sí se aplican.
docker-compose exec -T db psql -X -U adanvi -d postgres < bck/globals_2026-08-18_2142.sql

docker-compose exec -T db psql -X -U adanvi -d postgres -c "DROP DATABASE adanvi WITH (FORCE)"
docker-compose exec -T db psql -X -U adanvi -d postgres -c "CREATE DATABASE adanvi OWNER adanvi"
docker-compose exec -T db psql -X -U adanvi -d adanvi -c "CREATE EXTENSION IF NOT EXISTS timescaledb"
docker-compose exec -T db psql -X -U adanvi -d adanvi -c "SELECT timescaledb_pre_restore()"
docker-compose exec -T db pg_restore --no-owner --exit-on-error \
  -U adanvi -d adanvi < bck/adanvi_2026-08-20_0600.dump
docker-compose exec -T db psql -X -U adanvi -d adanvi -c "SELECT timescaledb_post_restore()"

uv run python scripts/grant_ro.py     # permisos del usuario de solo lectura
docker-compose up -d
```

Los `*.dump` están excluidos por `.gitignore`: para recuperar desde un clon hay
que copiar `bck/` junto al proyecto. `globals_*.sql` contiene hashes de
credenciales y debe tratarse como secreto.

### Acceso externo a la base (pgAdmin, DBeaver, notebooks)

Postgres se publica **solo en el loopback** (`127.0.0.1:5430`), a propósito: la
LAN de planta no debe poder hablar con la base. Desde otro equipo se llega por el
tailnet:

```bash
sudo tailscale serve --bg --tcp 5430 tcp://127.0.0.1:5430
uv run python scripts/grant_ro.py     # lee ADANVI_RO_PASSWORD del .env
```

Se prefirió `serve` a publicar el puerto en la IP de Tailscale porque `docker` y
`tailscaled` no tienen orden garantizado en systemd: en un arranque en frío el
contenedor podría fallar el bind contra una interfaz que aún no existe.

Conexión: host = la IP de Tailscale del servidor, puerto **`5430`** (el mismo que
escucha en el loopback, no el 5432 interno), base `adanvi`, usuario
**`adanvi_ro`** —existe desde la migración 004, pero `NOLOGIN` hasta que
`grant_ro.py` le pone contraseña—. `adanvi` es superusuario y se reserva para
mantenimiento: un `DELETE FROM readings` mal escrito desde un cliente gráfico
destruye el histórico de forma irrecuperable, que es lo único que esta app existe
para custodiar.

**`pg_hba.conf` no sirve como allowlist por IP aquí:** la conexión llega por
`tailscaled → docker-proxy → contenedor`, así que Postgres ve la gateway del
bridge (`172.x`), no al cliente. El control de acceso lo dan Tailscale (WireGuard
+ ACLs) y el rol de solo lectura; no hay segunda barrera, no la supongas.

## Fuera de alcance en v1

Asistente MCP/LLM, autenticación y RBAC, escritura de setpoints al PLC, fórmulas
de proceso, alta disponibilidad, tema claro, compresión por excepción (deadband),
anotaciones de evento, notación de dispositivo Delta (`D100`, `M50`) sobre el
direccionamiento Modbus.
