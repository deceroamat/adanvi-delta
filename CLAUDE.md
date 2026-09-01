# CLAUDE.md

Guía para agentes que trabajen en este repositorio.

## Qué es ADANVI

Historiador de tendencias para PLC **Delta AS-200** (**ADANVI by emolog**):
adquiere ~100 tags a 1 Hz por **Modbus/TCP**, los guarda en PostgreSQL +
TimescaleDB con retención acotada, y los muestra en galerías de tendencias con
navegación temporal en vivo e histórica.

Nació contra un Allen-Bradley por EtherNet/IP; la migración a Modbus (migración
008) cambió el modelo de tags de *nombre CIP con tipo descubierto* a *área +
dirección + tipo declarado*. El formulario de operación se retiró en ese mismo
cambio.

Dimensionado para un host modesto (i5-6500T, 8 GB, SSD), LAN industrial aislada,
**sin autenticación** y **sin build step de frontend**.

Contexto adicional: `README.md` (operación y decisiones de producto) y
`context.md` (plan greenfield original; histórico, algunas partes quedaron
superadas — p. ej. hoy los gaps NO se escriben como ceros y no hay tope de 3
galerías).

## Stack

- Python 3.12, `uv` + `uv.lock`, FastAPI + Uvicorn, `psycopg` 3 (pools sync y
  async separados), `pymodbus` 3 (`ModbusTcpClient` síncrono).
- TimescaleDB 2.29.1 sobre PG16, versión fijada en `docker-compose.yml`.
- Frontend: HTML + CSS + ES modules nativos, sin framework, sin bundler. uPlot
  vendorizado en `src/static/vendor/` (sin CDN).
- Todo el código, comentarios y commits están en español.

## Comandos

```bash
docker-compose up -d                 # despliegue completo (app en :8000)
docker-compose up -d db              # solo la base, para desarrollo local
uv sync
uv run python -m src                 # app en el host (DATABASE_URL -> localhost:5430)
uv run pytest                        # 87 tests, no requieren base de datos
deno test tests/frontend.test.js     # tests de la lógica de cliente (requiere Deno)
uv run python scripts/seed.py --days 35 --tags 12 --interval 5
uv run python scripts/modbus_sim.py  # esclavo Modbus falso en :5020, para probar sin PLC
uv run python scripts/grant_ro.py    # habilita el rol de solo lectura adanvi_ro
uv run python scripts/refresh_caggs.py --from ... --to ...  # rellena caggs (leer avisos)
```

`ruff` está configurado en `pyproject.toml` (line-length 100, reglas
`E,F,W,I,B,SIM,UP,RUF,S`) pero no es dependencia del proyecto: ejecutarlo con
`uvx ruff check .` si se quiere lintar.

Postgres se publica en `127.0.0.1:5430` (loopback del host, a propósito). El
acceso remoto va por Tailscale, no por exponer el puerto.

## Arquitectura

```
src/
  __main__.py     wait_for_db -> migraciones -> hilos worker -> uvicorn
  app.py          FastAPI: solo lifespan (prohibido @app.on_event), GZip, estáticos
  config.py       ÚNICO lugar donde se leen variables de entorno (dataclass Settings)
  constants.py    códigos de status (la paleta de series vive en series-table.js)
  timeparse.py    parser de ventanas y elección de capa/bucket (solo servidor)
  plc_client.py   pymodbus: agrupador de bloques + decodificación + backoff (1,2,4,8,15 s)
  worker/         acquirer / writer / broadcaster + registry + status
  api/            health, tags, history, galleries, export, live_ws
  db/             pool, migrate, repo_* (todo el SQL vive aquí)
  static/         páginas HTML + js/ (ES modules) + css/ + vendor/uplot
migrations/       SQL numerado, aplicado al arrancar y registrado en schema_migrations
scripts/          seed, modbus_sim, refresh_caggs, grant_ro, backup_database.sh
deploy/systemd/   timer + service del backup diario 06:00
```

**Worker: tres responsabilidades desacopladas por colas acotadas con drop-oldest.**

1. `Acquirer` — malla temporal sobre `monotonic()` sin deriva; timestamp tomado
   *antes* del read. **Nunca toca la base de datos.**
2. `Writer` — el único que escribe en Postgres: `COPY` por lotes, gestión de
   `acquisition_gaps`, recarga del catálogo de tags cada ~15 s.
3. `Broadcaster` (`hub`) — puente hilo→asyncio hacia los WebSockets, con cola
   propia por conexión.

Los dos primeros son **hilos** (`__main__.py`); el broadcaster no lo es: vive en
el event loop de uvicorn y recibe del worker por `call_soon_threadsafe`. No
buscar un tercer `threading.Thread`, no existe.

Separar escritura de adquisición es lo que impide que un checkpoint de Postgres
o un job de compresión desvíe la cadencia de lectura del PLC. Al añadir trabajo,
respetar el reparto: nada de I/O de base en el acquirer.

## Invariantes que no se deben romper

- **Los huecos no se guardan como ceros.** Si el PLC no responde no se insertan
  filas: se abre un intervalo en `acquisition_gaps` y el gráfico pinta la banda
  "SIN DATO". Un tag que falla con el PLC sano va con `value = NULL` y
  `status = 2`. Escribir `0.0` destruiría el autoescalado, envenenaría los
  agregados continuos de forma irreversible y sería indistinguible de un cero real.
- **`tags` es la única fuente de verdad de qué se pollea.** No hay YAML ni CSV.
- **No existe `tags.last_value`.** El estado vivo está en memoria
  (`worker/status.py`) y se expone por `/api/health` y `/api/live/snapshot`;
  100 UPDATE/s sobre una tabla pequeña generaría bloat permanente.
  `tags.last_seen_ts` se persiste cada ~30 s, no cada ciclo.
- **El parser de ventanas vive solo en el servidor** (`src/timeparse.py`). `m` es
  minutos y `M` es meses (30 días). El cliente manda `window=` o `from`/`to` y
  recibe de vuelta la resolución elegida.
- **La resolución se deriva de puntos que caben en el gráfico, no de umbrales
  fijos**, y respeta `MAX_SCAN_ROWS = 400_000` filas por consulta. Los bordes se
  alinean a múltiplos del bucket para que la caché del cliente reutilice tramos.
- **La configuración de series vive en la base** (`gallery_series`), no en
  `localStorage`; el rango visible vive en la **URL**.
- **Ninguna configuración de escala puede dejar una serie fuera del área de
  trazo.** La regla está en `src/static/js/scales.js` y cubierta por
  `tests/frontend.test.js`. Con `axis_group = 'auto'`: misma unidad *y* mismos
  límites. Un grupo explícito se respeta y toma la unión de los rangos.
- **Solo `lifespan` en FastAPI**, nunca `@app.on_event`.
- Al promediar sobre agregados continuos hay que **ponderar por `n`**
  (`sum(avg*n)/sum(n)`), no promediar promedios.

## Base de datos

| Objeto | Qué es |
|---|---|
| `tags` | Catálogo: qué se pollea y **de qué dirección Modbus** |
| `readings` | Hypertable cruda (chunks de 1 día), 90 d, comprime > 2 d |
| `readings_1m` / `readings_1h` | Caggs con `avg,min,max,n,last`; 1 año / 5 años |
| `acquisition_gaps` | Intervalos sin adquisición; a lo sumo uno abierto |
| `galleries` / `gallery_series` | Galerías y configuración de cada serie |

Migraciones: SQL plano numerado en `migrations/`, aplicado por
`src/db/migrate.py` en orden, con checksum en `schema_migrations`. **Una
migración ya aplicada no se reaplica**: si se edita, solo se registra un warning.
Para cambiar algo hay que **añadir un archivo nuevo**. Se descartó Alembic a
propósito (el DDL de Timescale es SQL puro). El runner no acepta parámetros, así
que **nunca poner secretos en una migración** (ver `scripts/grant_ro.py`).

Las policies de retención/compresión viven **en la base**: editar `.env` no las
cambia, hay que hacer `remove_retention_policy` + `add_retention_policy`.

## API

`/api/health`, `/api/live/snapshot`, `/api/tags` (CRUD; `DELETE` desactiva,
`?purge=true` borra histórico), `/api/history` y `/api/history/window`,
`/api/galleries` (+ `PUT /{id}/series`, reemplazo atómico), `/api/export.csv`,
y `WS /ws/live` (ticks columnares alineados al orden de `tag_ids` suscritos, más
eventos `gap_open`/`gap_close`).

Páginas: `/`, `/tags`, `/galleries`, `/galleries/{id}`.

## Frontend

Módulos sin DOM (testeables con Deno): `cache.js`, `scales.js`, `viewport.js`.

- `viewport.js` — un único estado `{ to, span, follow }`. No hay botón "Pausar":
  cualquier interacción apaga `follow` y solo **LIVE** lo reenciende (con
  backfill). El estado se serializa a la URL.
- `cache.js` — caché de tramos. Dos invariantes: la cobertura es **por tag**, y
  un tramo solo se mezcla con otro de **su misma resolución**. `prefetch()` tiene
  su **propio `AbortController`**: `load()` aborta el suyo en cada llamada, así
  que compartirlo haría que cada pan matase la precarga y al revés.
- `chart.js` — uPlot. Readout flotante junto al crosshair, autolimitado a 12
  series (la tabla sigue siendo la lectura completa); banda mín-máx al alejar;
  digitales en carriles al pie. Lo pedido y no recibido se pinta **gris**
  («Cargando…»), nunca con el rojo de los huecos.
- `series-table.js` — configuración + leyenda + lectura del crosshair; las
  estadísticas se calculan en cliente sobre lo ya cargado.
- **Lo que corre por frame debe escalar con lo visible, no con la caché.** Una
  entrada guarda hasta 7 ventanas (`KEEP_SPANS = 3` a cada lado), así que
  recorrer `u.data[0]` entero en un hook de dibujo o medir su `length` para
  decidir algo es un error: acotar con `lowerBound`.
- Los estáticos se sirven con `cache-control: no-cache` porque los nombres no
  llevan hash de contenido.

## Direccionamiento Modbus

Modbus no tiene tags con nombre ni tipo en el cable. Cada fila de `tags` declara
`unit_id`, `area` (`coil`/`discrete`/`holding`/`input`), `address`, `data_type`,
`word_order`, `scale` y `value_offset`; el valor guardado es
`crudo * scale + value_offset`. `tags.name` pasó a ser un identificador humano,
no una dirección.

- **Una petición lee un rango contiguo**, así que `plan_blocks` (en
  `plc_client.py`) agrupa los tags en bloques respetando los topes del protocolo
  (125 registros en FC03/FC04, 2000 bits en FC01/FC02) y fusionando huecos de
  hasta 8 registros: a 1 Hz lo caro son las idas y vueltas TCP, no los bytes.
- **El plan se cachea por identidad de la tupla del registry** (`tags is
  self._planned_for`), que se reemplaza entera al recargar el catálogo. No hace
  falta un contador de versión.
- **El error es por BLOQUE, no por tag.** Una dirección ilegal hace que el
  esclavo rechace la petición entera, así que sus vecinas de bloque también salen
  con `status = 2`. Es del protocolo, no una decisión de diseño.
- **Distinguir transporte de configuración es lo que sostiene el invariante de
  los huecos**: socket caído → se lanza y se abre un gap; `ExceptionResponse` →
  solo ese bloque va a `TagError`.
- **El orden de palabra invertido no da error**: da un número plausible y falso.
  Por eso es configurable por tag y hay que cotejar contra ISPSoft antes de dar
  de alta una tanda.
- Coherencia área↔tipo duplicada a propósito: `CHECK` en la migración 008 y
  `model_validator` en `api/tags.py`, misma convención que ya había.
- `scripts/modbus_sim.py` levanta un esclavo falso con el mismo mapa que siembra
  `seed.py`: es como se prueba el camino completo sin el PLC.

## Convenciones

- Todo se almacena en **UTC**; la presentación es `America/Bogota` / locale `es-CO`.
- Todo el SQL vive en `src/db/repo_*.py`; los nombres de tabla se interpolan solo
  desde `timeparse.LAYERS`, y aun así con `sql.Identifier`.
- Los comentarios del repo explican **por qué**, no qué. Mantener ese estilo:
  varias decisiones están documentadas junto al código que las implementa.
- `.env` nunca se versiona; `.env.example` sí. `bck/*.dump` está en `.gitignore`
  y `bck/globals_*.sql` contiene hashes de credenciales — tratarlo como secreto.
- Commits en español, en imperativo o descriptivos cortos.

## Fuera de alcance en v1

Asistente MCP/LLM, autenticación y RBAC, escritura de setpoints al PLC, fórmulas
de proceso, alta disponibilidad, tema claro, deadband, anotaciones de evento.
