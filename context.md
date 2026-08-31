# ADANVI by emolog — Plan greenfield

Documento para un agente de programación. Construir la app **desde cero**. El repo `worker-allenbradley` es solo referencia de ideas buenas/malas; no reutilizar su estructura de páginas ni SQLite/YAML como diseño final.

---

## 0. Identidad y objetivo

- **Nombre:** ADANVI by emolog
- **Qué es:** adquisición continua de tags Allen-Bradley + historial time-series + **galerías de tendencias** en tiempo real + asistente MCP en lenguaje natural.
- **Qué no es:** no hay dashboard de tiles, no hay módulo de fórmula/gramaje, no hay `tags.yaml` ni CSV como fuente de verdad.
- **Prioridad #1 de producto:** gráficos de tendencia en vivo, **pausar para analizar** y **reanudar sin huecos** (backfill desde BD).
- **Carga de diseño:** ~100 tags cada 1 s; host típico SSD ~200 GB libres, 8 GB RAM, i5-6500T.

---

## 1. ¿Cambia el stack respecto al prototipo?

**Casi no el núcleo; sí el almacenamiento y el producto UI.**

| Se mantiene | Cambia / se añade | Se elimina |
|-------------|-------------------|------------|
| Python 3.12, `uv`, Docker | **PostgreSQL + TimescaleDB** (no SQLite) | `tags.yaml`, CSV |
| `pycomm3`, worker ~1 s | Tags y galerías **solo en BD** | Dashboard tiles y Fórmula |
| FastAPI + Uvicorn + WebSocket | Páginas: **Home**, **Tags**, **Galerías**, **Vista galería** | Overlay solo en `localStorage` como fuente de verdad |
| uPlot, dark UI | Parser ventana `1m`/`1h`/`1M` + pause/resume con backfill | |
| MCP + LLM | Gaps PLC → valor **0** + status de pérdida | |

Con 100 tags/s, agregados `_1m`/`_2m`, compresión y retención, **Timescale es la elección correcta**. No hace falta React/Vue ni otro framework de frontend.

---

## 2. Stack definitivo

| Capa | Tecnología | Notas |
|------|------------|--------|
| Runtime | Python ≥3.11/3.12, `uv` + lockfile | Un proceso app + contenedor DB |
| PLC | `pycomm3` LogixDriver | CompactLogix / EtherNet/IP `:44818` |
| API | FastAPI + Uvicorn | Solo `lifespan` (prohibido `@app.on_event`) |
| Live | WebSocket + cola **acotada** | Drop oldest si se llena |
| DB | **PostgreSQL 16 + TimescaleDB** | Hypertable + CAGGs + compression + retention |
| UI | HTML/JS vanilla + **uPlot** | CSS tokens dark (ver §7.2) |
| LLM | OpenAI-compatible (OpenCode Go) | Tool-calling con allowlist |
| MCP | FastMCP stdio, **solo SELECT** | Contra Postgres, no SQLite |
| Deploy | Docker Compose | servicios `adanvi` + `db` |
| Config | `.env` | `PLC_IP`, `DATABASE_URL`, LLM, `READ_INTERVAL_MS` |

**Prohibido en v1 como store principal:** SQLite, CSV, archivos planos de tags.

---

## 3. Retención y capas time-series

| Capa | Ventana | Uso en viz |
|------|---------|------------|
| `readings` (raw ~1 s) | **30 días** | Zoom fino, live, backfill de pause corto |
| `readings_1m` | **1 año** | Ventanas medias |
| `readings_2m` | **3 años** | Ventanas largas |
| Compresión raw | chunks **> 7 días** | Ahorro disco; zoom reciente sigue “caliente” |

**Estimación disco (100 tags × 1 Hz, con compresión):** del orden de decenas de GB con esta política — holgado en ~200 GB libres. El límite de rendimiento del host no es el ingest (trivial), sino consultas largas sobre raw sin CAGG y la RAM (tunear Postgres: `shared_buffers` 512MB–1GB, `work_mem` bajo).

**Router de resolución en `/api/history` (servidor):**

| Span (`to - from`) | Fuente |
|--------------------|--------|
| ≤ 6 h (configurable por env) | `readings` raw |
| ≤ 14 d | `readings_1m` |
| > 14 d | `readings_2m` |

Nunca full-scan de meses en raw. Objetivo ~1500–3000 puntos por serie en respuesta; downsampling servidor si hace falta.

---

## 4. Errores del prototipo — NO repetir

1. **Docs desalineados del código** (README hablaba de CSV mientras el código usaba SQLite): documentar solo lo implementado.
2. **Tags en YAML + volume Docker `:ro`** mientras la API intentaba escribir: **fuente de verdad = tabla `tags` en Postgres**.
3. **Sin retención/compresión:** policies Timescale desde el día 1.
4. **Cola live sin tope** (`put_nowait` ilimitado): `maxsize` + descartar lo más viejo + warning en log.
5. **Worker daemon que muere en silencio:** `/api/health` debe exponer `worker_alive`, `plc_connected`, `last_cycle_ts`, `last_error`; la UI lo muestra en el topbar.
6. **Open/close de DB por operación sin pool:** usar pool (`psycopg_pool` o equivalente).
7. **Mezcla `lifespan` + `@app.on_event`:** solo lifespan.
8. **MCP frágil/inseguro** (PRAGMA con f-string, filtro SQL solo por regex): parámetros bindeados; allowlist de tools; timeout y LIMIT en queries.
9. **Config de series solo en `localStorage`:** colores, escalas y membresía de galería **en BD**.
10. **Sin auth** (aceptable en LAN industrial): no exponer a Internet sin proxy/auth; dejar nota en README.
11. **Desconexión PLC sin rastro en la serie:** en ADANVI, si el PLC está caído se **registran ceros** para ver la pérdida en la tendencia (ver §6.1).

### Ideas del prototipo que SÍ reutilizar

- Lectura múltiple CIP en una llamada.
- Timestamp capturado **antes** del `read`.
- Malla temporal con `monotonic()` sin deriva acumulada.
- Backoff de reconexión 1→2→4→8→15 s, reintentos infinitos.
- MCP read-only + agente con máximo de iteraciones y allowlist de tools.
- Estética dark + paleta uPlot del prototipo.
- Modal de chat MCP (UX), no la página de fórmula ni el dashboard de tiles.

---

## 5. Modelo de datos (Postgres / Timescale)

### 5.1 `tags` — catálogo y qué se pollea

| Columna | Tipo | Notas |
|---------|------|--------|
| `id` | BIGSERIAL PK | |
| `name` | TEXT UNIQUE NOT NULL | Nombre CIP exacto |
| `label` | TEXT | Etiqueta humana |
| `active` | BOOLEAN NOT NULL DEFAULT true | Si true, el worker lo lee |
| `value_type` | TEXT | BOOL, REAL, DINT… (al leer) |
| `last_value` | DOUBLE PRECISION | |
| `last_status` | TEXT | Good / error / Disconnected / Pending |
| `last_seen_ts` | TIMESTAMPTZ | |
| `created_at` / `updated_at` | TIMESTAMPTZ | |

- Alta/baja/edición **solo por API** → BD.
- Worker cada ciclo: `SELECT id, name FROM tags WHERE active`.
- **Sin archivo plano.**

### 5.2 `readings` — hypertable raw

| Columna | Tipo | Notas |
|---------|------|--------|
| `ts` | TIMESTAMPTZ NOT NULL | |
| `tag_id` | BIGINT NOT NULL FK → tags | Preferir id, no solo texto |
| `value` | DOUBLE PRECISION | Gaps de desconexión: **0.0** |
| `status` | TEXT NOT NULL | Good / error tag / Disconnected |

- `create_hypertable('readings', by_range('ts'))`.
- Índice `(tag_id, ts DESC)`.
- Compresión: `segmentby = tag_id`, `orderby = ts`.
- Retention raw: **30 días**. Compresión chunks **> 7 días**.

### 5.3 Continuous aggregates

- **`readings_1m`:** bucket 1 minute → `avg`, `min`, `max`, `count`, y si es posible `last(value, ts)`. Retention **365 días**.
- **`readings_2m`:** bucket 2 minutes (directo o sobre `_1m`). Retention **3 años**.
- Refresh policy cada 1–2 minutos.
- Definir si se agregan solo `status = 'Good'` o también ceros de `Disconnected` (recomendado para gaps: **incluir Disconnected con value 0** en raw; en CAGG usar avg de values presentes para no “inventar” suavizados raros — documentar la regla elegida y ser consistente).

### 5.4 Galerías (máximo 3)

**`galleries`**

| Columna | Tipo |
|---------|------|
| `id` | BIGSERIAL PK |
| `name` | TEXT UNIQUE NOT NULL |
| `description` | TEXT NULL |
| `created_at` / `updated_at` | TIMESTAMPTZ |

Enforce en aplicación (y idealmente con trigger o check de conteo): **`COUNT(*) <= 3`**. `POST` de la 4ª → **409**.

**`gallery_series`**

| Columna | Tipo | Notas |
|---------|------|--------|
| `id` | BIGSERIAL PK | |
| `gallery_id` | FK | |
| `tag_id` | FK | |
| `color` | TEXT NOT NULL | hex, ej. `#3987e5` |
| `scale_mode` | TEXT NOT NULL | `auto` \| `manual` |
| `y_min` / `y_max` | DOUBLE PRECISION NULL | si manual |
| `axis` | TEXT DEFAULT `left` | `left` \| `right` (dual axis v1 limitado) |
| `sort_order` | INT | |
| | UNIQUE(gallery_id, tag_id) | |

Todo lo configurado en la tabla inferior de la vista galería **persiste en BD** y se restaura al volver a entrar (no depender de `localStorage` como fuente de verdad).

---

## 6. Worker de adquisición

### 6.1 Ciclo normal

1. Cargar tags `active` desde BD (reload periódico; sin YAML).
2. Si no conectado → `connect()`; si falla → §6.2.
3. `ts = now` (antes del read).
4. Multi-read CIP de todos los names.
5. Batch insert en `readings` + update catálogo `tags`.
6. Push a cola live (acotada).
7. Sleep hasta siguiente tick de malla `monotonic()` (sin deriva).

### 6.2 PLC desconectado o fallo de ciclo completo — gaps visibles

1. `plc_connected = false` en status compartido.
2. **Insertar igual** una fila por cada tag activo:
   - `value = 0.0`
   - `status = 'Disconnected'`
   - `ts` del ciclo
3. Emitir esas lecturas por WS para que la tendencia muestre el valle a 0.
4. Backoff de reconexión; al recuperar, valores reales con `Good` (o error por tag).

**Error de un tag individual (PLC ok):** regla consistente — numéricos → `value = 0` (o NULL si se prefiere; **recomendado 0 para no romper escala de tendencia**) + `status = <error CIP>`; BOOL → 0 + status error.

### 6.3 Cola live

`queue.Queue(maxsize=≈100)`. Si llena: descartar mensaje más viejo y meter el nuevo; log warning. Nunca crecer sin límite.

### 6.4 Salud

`WorkerStatus` thread-safe: `plc_connected`, `last_cycle_ts`, `polled_tags`, `worker_alive`, `last_error`. Expuesto en `GET /api/health`.

---

## 7. API REST (contrato mínimo)

### Tags

- `GET /api/tags`
- `POST /api/tags` `{ "name", "label?" }`
- `PATCH /api/tags/{id}` `{ "label?", "active?" }`
- `DELETE /api/tags/{id}` — documentar si borra histórico o solo desactiva

### Health

- `GET /api/health` → PLC + worker + last cycle + error

### History (núcleo de viz)

- `GET /api/history?tags=...&from=&to=`
- o `GET /api/history?tags=...&window=1h` (now − window → now)
- Backend parsea `window`, elige raw / `_1m` / `_2m`, devuelve por tag puntos `{ ts, value, status }` ASC.
- Límite de puntos; 400 si window inválido.

### Galerías

- `GET /api/galleries`
- `POST /api/galleries` `{ "name" }` → **409 si ya hay 3**
- `PATCH /api/galleries/{id}` `{ "name" }`
- `DELETE /api/galleries/{id}`
- `GET /api/galleries/{id}` → galería + series
- `PUT /api/galleries/{id}/series` → reemplazo atómico de la lista de series (color, escala, order)

### Chat

- `POST /api/ask` `{ "question" }` → `{ answer, trace, rows?, columns? }`
- System prompt con schema Timescale (`tags`, `readings`, `readings_1m`, `readings_2m`, `galleries`); TZ America/Bogota; recomendar CAGGs para rangos largos; no inventar datos.
- Sin API key → 503 claro.

### WebSocket

- `/ws/live`
- Cliente: `{ "type": "subscribe", "tags": ["..."] }` o `tag_ids`
- Server: `{ "cycle_ts", "readings": [{ "tag", "value", "status", "ts" }] }`
- Incluir ciclos `Disconnected` (ceros).

---

## 8. Frontend

### 8.1 Rutas

| Ruta | Página | Rol |
|------|--------|-----|
| `/` | Home | Marca, estado PLC, enlaces, **modal MCP** |
| `/tags` | Administración de tags | CRUD de lo que el worker registra |
| `/galleries` | Galería de tendencias | Lista máx. 3; crear p.ej. “Zona X” |
| `/galleries/{id}` | Vista tendencia | **Prioridad #1:** chart + tabla de series |

**No existen:** dashboard de tiles, formula, analysis como página suelta global.

Nav topbar: `Inicio | Tags | Galerías` (+ botón **Consultar** en Home, opcional global). Branding: **ADANVI** + subtítulo **by emolog**.

### 8.2 Sistema visual (obligatorio)

Dark industrial, sin tema claro en v1. Tokens:

```css
--page-plane:      #0d0d0d;
--surface-1:       #1a1a19;
--surface-2:       #202020;
--text-primary:    #ffffff;
--text-secondary:  #c3c2b7;
--text-muted:      #898781;
--gridline:        #2c2c2a;
--baseline:        #383835;
--border-hairline: rgba(255, 255, 255, 0.10);
--status-good:     #0ca30c;
--status-warning:  #fab219;
--status-critical: #d03b3b;
--status-unknown:  #55534d;
```

Paleta series (8 sólidos; índice ≥ 8 → trazo punteado como encoding secundaria):

```
#3987e5  #008300  #d55181  #c98500  #199e70  #d95926  #9085e9  #e66767
```

- Font: `system-ui, -apple-system, "Segoe UI", sans-serif`.
- Topbar `surface-1`, borde hairline, stats a la derecha (tags activos, último ciclo, PLC) + dot con glow según estado.
- Controles: fondo `surface-2`, bordes hairline, tipografía densa.
- uPlot: ejes `#898781`, grid `#2c2c2a`.
- Chat: modal centrado, overlay oscuro (espíritu del `chat.css` del prototipo).
- Poco adorno; **máximo real estate al chart** en la vista galería.

### 8.3 Home `/`

- Marca ADANVI by emolog.
- Cards/links: Administrar tags · Galería de tendencias.
- Indicador PLC visible.
- Botón **Consultar** → modal MCP (consultas generales al histórico).
- Sin charts en home.

### 8.4 Tags `/tags`

- Tabla: nombre CIP, label, tipo, último valor, status, active, acciones.
- Form alta: `name` + `label` opcional.
- Toggle `active` (dejar de pollear).
- Validar name no vacío; unique → 409.

### 8.5 Galerías `/galleries`

- Hasta 3 cards: nombre, cantidad de series, abrir.
- “Nueva galería” → 409/UI disabled al cuarto intento.
- Rename / delete con confirmación.

### 8.6 Vista galería `/galleries/{id}` — pantalla principal de producto

Layout (evolución del analysis del prototipo, **sin** panel izquierdo de registro global de tags):

```
┌─ topbar: ADANVI | nav | stats PLC ─────────────────────────┐
├─ toolbar tendencias ───────────────────────────────────────┤
│  [ventana: input "1h"] [presets] [ir a fecha] [⏸ Pausar]   │
├─ chart uPlot (vacío hasta tener series) ───────────────────┤
│  legend / tooltip hover                                      │
├─ tabla series (abajo) ─────────────────────────────────────┤
│  + Agregar tag | color | escala auto/manual | min | max | ✕ │
└────────────────────────────────────────────────────────────┘
```

#### Toolbar (prioridad de producto)

1. **Ancho de ventana (input texto)**  
   - Ejemplos: `1m`, `15m`, `1h`, `8h`, `1d`, `1w`, `1M`.  
   - Convención del parser (estilo Grafana) — **una sola verdad, preferible parsear en servidor** si se manda `window=`:

   | Token | Significado |
   |-------|-------------|
   | `s` | segundos (`30s`) |
   | `m` | **minutos** (`1m`, `15m`) |
   | `h` | horas (`1h`) |
   | `d` | días (`1d`) |
   | `w` | semanas (`2w`) |
   | `M` | **meses** (`1M`) — mayúscula, distinta de `m` |

   - Regex: `^(\d+)(s|m|h|d|w|M)$`. Inválido → error claro en UI/API.
   - Internamente se obtiene el span en segundos y se consulta history; **el backend elige** raw / `_1m` / `_2m`.

2. **Presets (chips opcionales):** 5m, 1h, 8h, 1d — rellenan el mismo modelo de ventana.

3. **Ir a fecha**  
   - `datetime-local` o from/to.  
   - Modo **histórico fijo**: pausa implícita del live.  
   - “Volver a vivo” limpia el ancla y reanuda.

4. **Pausar / Reanudar** (crítico — no perder información)  
   - **Pausar:** deja de avanzar el eje con el reloj; no hace trim de ventana deslizante; permite zoom/pan/hover sobre lo cargado.  
   - **Reanudar sin huecos:**  
     - **Obligatorio:** al reanudar, `GET /api/history` desde `last_ts` presente en el chart → `now`, merge ordenado, luego seguir con WS.  
     - **Opcional:** buffer corto en RAM de mensajes WS durante la pausa (con tope); el backfill por history es la fuente de verdad.  
   - No “saltar” el intervalo pausado dejando un vacío si la BD tiene datos.

5. **Live** (no pausado, no custom range)  
   - Ventana deslizante `[now - span, now]`.  
   - Append por WS + `trimBefore`.  
   - Mostrar puntos `Disconnected` (0) como pérdida de datos.

#### Chart

- uPlot multi-serie según `gallery_series` (color y escala por fila).
- Escala: `auto` vs `manual` (`y_min`/`y_max`). Dual axis left/right limitado en v1 si hace falta.
- BOOL: franjas debajo o escalón; respetar color de serie.
- Empty state: “Agrega tags en la tabla inferior”.
- Tooltip: hora local `es-CO`, valores formateados.

#### Tabla inferior (persistida)

| Tag (select de tags active) | Color | Escala auto/manual | Y min | Y max | Quitar |

- Cambios → debounce → `PUT /api/galleries/{id}/series`.
- Al cargar: GET gallery → filas + chart vacío → history + subscribe WS a esos tags.

---

## 9. MCP / LLM

- Subprocess FastMCP, tools read-only sobre Postgres.
- Tools: `list_tables`, `describe_table`, `read_query` (solo SELECT/WITH; timeout; LIMIT ≤ 1000).
- Prompt: schema real; preferir `_1m`/`_2m` en rangos largos; español; no inventar filas.
- Modal principalmente en **Home**.
- Allowlist de tools en el agente; máx. ~6 iteraciones tool-calling.

---

## 10. Deploy

```yaml
services:
  db:
    image: timescale/timescaledb:latest-pg16
    volumes: [tsdata:/var/lib/postgresql/data]
    # POSTGRES_USER/PASSWORD/DB
    # healthcheck obligatorio
  adanvi:
    build: .
    ports: ["8000:8000"]
    env_file: .env
    depends_on:
      db: { condition: service_healthy }
```

- Volumen de datos Postgres en SSD.
- **No** montar `tags.yaml`.
- Red bridge; PLC alcanzable por NAT del host (puerto 44818 del PLC).
- Variables: `PLC_IP`, `DATABASE_URL`, `READ_INTERVAL_MS`, `TZ`, `OPENCODE_API_KEY`, `LLM_MODEL`, `LLM_BASE_URL`, umbrales del router history si se externalizan.

---

## 11. Estructura de repo sugerida

```
adanvi/
  pyproject.toml
  Dockerfile
  docker-compose.yml
  .env.example
  README.md
  migrations/                 # SQL: schema, hypertable, CAGGs, policies
  src/
    __main__.py               # hilo worker + uvicorn
    config.py
    plc_client.py
    worker.py
    db/                       # pool, migrate hook, repositorios
    api/                      # tags, galleries, history, health, ask, ws
    llm.py
    mcp_client.py
    mcp_server.py
    static/
      css/style.css
      css/chat.css
      js/api.js
      js/timeparse.js
      js/charts.js
      js/home.js
      js/tags.js
      js/galleries.js
      js/gallery-view.js      # pause/resume, window, tabla series
      js/chat.js
      vendor/uplot/
      index.html
      tags.html
      galleries.html
      gallery.html
```

Migraciones idempotentes al arranque o script `migrate` explícito.

---

## 12. Orden de implementación

1. Compose Timescale + migrations (hypertable, CAGGs, compression, retention) + pool.
2. Worker: tags desde BD, batch insert, gaps en 0, status, cola acotada, health.
3. API tags + health.
4. API history + parser `window` + router raw/_1m/_2m.
5. WebSocket live.
6. UI Tags + Home + indicador PLC.
7. API galleries (max 3) + series.
8. UI listado galerías + **vista chart** (ventana, presets, ir a fecha, pause/resume+backfill, tabla series).
9. MCP + modal Consultar en Home.
10. Pruebas: 100 tags; pause 5 min y resume sin huecos; window `1h`/`1d`/`1M` elige capa; 4ª galería rechazada; PLC off → línea a 0; F5 restaura series de galería.
11. README real alineado al código.

---

## 13. Criterios de aceptación

- [ ] ~100 tags @ 1 Hz estables; disco acotado por policies Timescale.
- [ ] Sin YAML/CSV de tags; CRUD en `/tags`; worker lee solo `active` en BD.
- [ ] Máximo 3 galerías; series (color, escala) persisten tras F5.
- [ ] Live + pause + resume **sin pérdida** (backfill history).
- [ ] Ir a fecha y `window` (`1m`/`1h`/`1M`) funcionan; backend elige tabla correcta.
- [ ] PLC down → puntos 0 / `Disconnected` visibles en la tendencia.
- [ ] No existen rutas ni UI de formula ni dashboard tiles.
- [ ] MCP en home; SQL solo lectura.
- [ ] UI dark con tokens y paleta definidos; marca ADANVI by emolog.
- [ ] Cola live acotada; health refleja worker y PLC; solo lifespan FastAPI.

---

## 14. Fuera de alcance v1

- Auth multi-usuario / RBAC.
- Escritura de setpoints al PLC.
- Fórmulas de proceso / feed-forward / gramaje.
- Más de 3 galerías.
- Mobile-first (usable, no prioritario).
- HA multi-nodo.
- Tema claro.

---

## 15. Resumen de flujo

```
Postgres tags (active)
        │
        ▼
Worker 1 Hz ──► pycomm3 multi-read
        │            │
        │            ├─ OK  → readings (value real, Good)
        │            └─ DOWN → readings (0, Disconnected)
        │
        ├─► Timescale: raw 30d · _1m 1y · _2m 3y · compress >7d
        └─► cola acotada ──► WS ──► Vista galería (live)
                                      │
UI Tags / Galerías / Home ◄── FastAPI ┤
                                      ├─ /api/history (router de capa)
                                      └─ /api/ask ──► LLM ──► MCP SELECT
```

**Fin del plan.** Implementar en este orden; no reintroducir SQLite, YAML de tags, dashboard ni fórmula.
