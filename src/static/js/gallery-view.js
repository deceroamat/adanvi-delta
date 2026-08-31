/**
 * Orquestador de la vista de galeria.
 *
 * Conecta viewport (que rango se ve) -> cache (que datos hacen falta) ->
 * chart (como se dibuja) -> tabla (configuracion y lectura).
 */

import { api, reportError, toast } from "./api.js";
import { HistoryCache, lowerBound } from "./cache.js";
import { TrendChart } from "./chart.js";
import { fmtDateTime, fmtDuration, fmtNumber, fromDatetimeLocal, toDatetimeLocal } from "./format.js";
import { scaleKeyFor } from "./scales.js";
import { SeriesTable } from "./series-table.js";
import { mountTopbar } from "./shell.js";
import { PRESETS, Viewport } from "./viewport.js";

const galleryId = Number(location.pathname.split("/").pop());
const REFINE_DEBOUNCE_MS = 150;
const SAVE_DEBOUNCE_MS = 500;
const TABLE_HEIGHT_KEY = "adanvi.tableHeight";

// Acciones de un solo evento (clic en '‹', preset, rango): pedir datos al
// instante. El debounce existe por el arrastre, que dispara ~60 pan/s; aplicarlo
// tambien a una pulsacion suelta es tiempo muerto antes de empezar siquiera.
const IMMEDIATE_REASONS = new Set(["pan-step", "window", "range"]);

// Tras este rato sin tocar nada se precargan las ventanas contiguas, para que
// la siguiente pulsacion de '‹' se pinte desde memoria y no desde la red.
const PREFETCH_IDLE_MS = 400;

// Tope de series en el readout flotante. Sin el, una galeria con 30 tags taparia
// justo el grafico que se esta intentando leer.
const READOUT_MAX_ITEMS = 12;
const READOUT_OFFSET_PX = 14;

mountTopbar("galleries");

// --- estado -----------------------------------------------------------

const { viewport, pendingWindow } = Viewport.fromUrl();
const cache = new HistoryCache();
let rows = [];
let allTags = [];
let socket = null;
// Orden de tags que confirmo el servidor. Los ticks vienen alineados a EL, no a
// la lista local: si no, al ocultar una serie los valores se asignarian a la
// serie equivocada durante el intervalo entre el cambio y la resuscripcion.
let subscribedIds = [];
let loadToken = 0;
let refineTimer = null;
let saveTimer = null;
let pendingLive = null;
let rafHandle = null;
let paintHandle = null;
let prefetchTimer = null;
let lastStats = new Map();

// Readout del crosshair. `readoutStale` lo levanta applyRowsToChart(), que es
// por donde pasa cualquier cambio de series: asi el DOM se rehace ahi y no en
// cada movimiento del raton.
const readoutEl = document.getElementById("cursor-readout");
const readoutTimeEl = readoutEl.querySelector("[data-readout-time]");
const readoutItemsEl = readoutEl.querySelector("[data-readout-items]");
const readoutMoreEl = readoutEl.querySelector("[data-readout-more]");
let readoutCells = new Map();
let readoutStale = true;
let cursorState = null;
let cursorHandle = null;

const chart = new TrendChart(document.getElementById("chart"), {
  onPan: (fraction) => viewport.pan(fraction),
  onZoom: (factor, ratio) => viewport.zoom(factor, ratio),
  onSelect: (from, to) => viewport.setRange(from, to),
  onReset: () => viewport.resetZoom(),
  onCursor: (idx, left, top) => showCursor(idx, left, top),
});

const table = new SeriesTable(document.getElementById("series-table"), {
  onChange: (row, field) => {
    applyRowsToChart();
    if (field === "visible") {
      subscribe(); // resuscribir ya, sin esperar al guardado con debounce
      reload();
    } else if (field === "color") {
      refresh({ redrawOnly: true });
    }
    scheduleSave();
  },
  onRemove: (tagId) => {
    rows = rows.filter((r) => r.tagId !== tagId);
    table.setRows(rows);
    applyRowsToChart();
    scheduleSave();
    reload();
  },
  onAdd: (tagId) => addTag(tagId),
});

// --- carga inicial ----------------------------------------------------

async function boot() {
  try {
    const [gallery, tags] = await Promise.all([
      api.get(`/api/galleries/${galleryId}`),
      api.get("/api/tags?active=true"),
    ]);
    document.title = `${gallery.name} · ADANVI`;
    document.getElementById("gallery-title").textContent = gallery.name;

    allTags = tags;
    table.setTags(tags);
    rows = gallery.series.map(toRow);
    table.setRows(rows);
    applyRowsToChart();

    if (pendingWindow) {
      // El parser de ventanas vive en el servidor: se resuelve alli el token
      // que venia en la URL antes de pedir datos.
      try {
        const { seconds } = await api.get(`/api/history/window?w=${encodeURIComponent(pendingWindow)}`);
        viewport.setSpan(seconds, pendingWindow);
      } catch {
        toast(`Ventana "${pendingWindow}" no válida; se usa 1h`, true);
      }
    }

    renderToolbar();
    connectSocket();
    await reload();
  } catch (err) {
    reportError(err);
  }
}

function toRow(series) {
  return {
    tagId: series.tag_id,
    tagName: series.tag_name,
    tagKind: series.tag_kind,
    tagDecimals: series.tag_decimals,
    label: series.tag_label || series.tag_name,
    visible: series.visible,
    color: series.color,
    axisGroup: series.axis_group,
    scaleMode: series.scale_mode,
    yMin: series.y_min,
    yMax: series.y_max,
    unit: series.unit_override ?? series.tag_unit ?? "",
    decimals: series.decimals,
    interp: series.interp,
    lineWidth: series.line_width,
    agg: series.agg,
  };
}

function addTag(tagId) {
  const tag = allTags.find((t) => t.id === tagId);
  if (!tag) return;
  rows.push({
    tagId,
    tagName: tag.name,
    tagKind: tag.kind,
    tagDecimals: tag.decimals,
    label: tag.label || tag.name,
    visible: true,
    color: SeriesTable.nextColor(rows.map((r) => r.color)),
    axisGroup: "auto",
    scaleMode: "auto",
    yMin: null,
    yMax: null,
    unit: tag.unit || "",
    decimals: null,
    interp: "auto",
    lineWidth: 2,
    agg: "avg",
  });
  table.setRows(rows);
  applyRowsToChart();
  scheduleSave();
  reload();
}

function applyRowsToChart() {
  readoutStale = true;
  chart.setSeries(
    rows.map((r) => ({
      tagId: r.tagId,
      label: r.label,
      color: r.color,
      visible: r.visible,
      unit: r.unit,
      decimals: r.decimals ?? r.tagDecimals ?? 2,
      interp: r.interp,
      lineWidth: r.lineWidth,
      kind: r.tagKind,
      axisGroup: r.axisGroup,
      scaleKey: scaleKeyFor(r),
      scaleMode: r.scaleMode,
      yMin: r.yMin,
      yMax: r.yMax,
    })),
  );
}

// --- datos ------------------------------------------------------------

function visibleTagIds() {
  return rows.filter((r) => r.visible).map((r) => r.tagId);
}

async function reload() {
  const tagIds = visibleTagIds();
  if (!tagIds.length) {
    chart.setData({ series: new Map(), gaps: [], aggregated: false }, viewport.range);
    table.setStats(new Map());
    setResolution(null);
    return;
  }

  const token = ++loadToken;
  const { from, to } = viewport.range;

  // Pintado inmediato con lo que ya hay en cache; la red solo refina.
  const cached = cache.peek(from, to);
  if (cached) {
    chart.setData(cached, viewport.range);
    updateStats(cached);
    showPending(cached.missing);
  }

  setResolution(cached?.resolution ?? null, true);
  try {
    const result = await cache.load({
      tagIds,
      from,
      to,
      maxPoints: chart.maxPoints,
    });
    if (token !== loadToken) return; // llego una peticion mas nueva
    // `fromCache` significa que no llego ni una fila nueva: reconstruir todas
    // las columnas seria rehacer un trabajo identico. `canReuseData` es lo que
    // impide hacerlo cuando el trazo es de una configuracion de series anterior.
    if (result.fromCache && chart.canReuseData()) {
      chart.setRange(viewport.from, viewport.to);
    } else {
      chart.setData(result, viewport.range);
    }
    updateStats(result);
    setResolution(result.resolution);
    showPending([]);
    schedulePrefetch();
  } catch (err) {
    if (err?.name !== "AbortError") reportError(err);
  }
}

/**
 * Precarga de las ventanas contigua anterior y siguiente.
 *
 * Solo con la vista quieta, y solo fuera de LIVE: siguiendo el reloj la ventana
 * se mueve sola y el futuro todavia no existe, asi que no hay nada que traer.
 */
function schedulePrefetch() {
  clearTimeout(prefetchTimer);
  if (viewport.follow) return;
  prefetchTimer = setTimeout(runPrefetch, PREFETCH_IDLE_MS);
}

async function runPrefetch() {
  const tagIds = visibleTagIds();
  if (!tagIds.length) return;
  const { from, to, span } = viewport.range;
  try {
    await cache.prefetch({
      tagIds,
      from: from - span,
      to: to + span,
      maxPoints: chart.maxPoints,
    });
  } catch (err) {
    // Una precarga fallida no es un fallo del usuario: no se le avisa con toast.
    if (err?.name !== "AbortError") console.warn("prefetch fallido", err);
  }
}

/**
 * Tramos que faltan por llegar.
 *
 * Solo fuera de LIVE: siguiendo el reloj el borde derecho avanza un segundo por
 * tick sin que nadie lo recargue, y marcarlo dejaria una tira de "Cargando…"
 * creciendo para siempre sobre datos que en realidad estan llegando por el
 * WebSocket.
 */
function showPending(missing, redraw = true) {
  chart.setPending(viewport.follow ? [] : missing || [], redraw);
}

/**
 * Durante un arrastre se repinta al instante y solo se pide al soltar.
 *
 * El repintado se coalesce en un frame: un arrastre dispara del orden de 60
 * eventos por segundo, y cada uno redibujaba el grafico entero y recalculaba las
 * estadisticas de todo lo cacheado.
 */
function refresh({ redrawOnly = false, immediate = false } = {}) {
  paint();
  if (redrawOnly) return;

  clearTimeout(refineTimer);
  clearTimeout(prefetchTimer);
  if (immediate) {
    reload();
    return;
  }
  refineTimer = setTimeout(reload, REFINE_DEBOUNCE_MS);
}

function paint() {
  if (paintHandle) return;
  paintHandle = requestAnimationFrame(() => {
    paintHandle = null;
    // Se lee aqui y no al agendar: vale el rango del frame que se pinta.
    const cached = cache.peek(viewport.from, viewport.to);
    // Antes del setRange a proposito: el redibujado que provoca el cambio de
    // escala ya recoge la banda nueva, y asi un arrastre no pinta dos veces.
    if (cached) showPending(cached.missing, false);
    chart.setRange(viewport.from, viewport.to);
    if (cached) updateStats(cached);
  });
}

viewport.addEventListener("change", (event) => {
  const { reason } = event.detail;
  renderToolbar();

  if (reason === "tick") {
    chart.setRange(viewport.from, viewport.to);
    return;
  }
  if (reason === "live") {
    // Reanudar sin huecos: se pide desde el ultimo punto cargado hasta ahora.
    cache.abort();
    showPending([]);
    reload();
    return;
  }
  refresh({ immediate: IMMEDIATE_REASONS.has(reason) });
});

// El reloj avanza siempre; el viewport decide si eso mueve la ventana.
setInterval(() => viewport.tick(), 1000);

// --- estadisticas y cursor --------------------------------------------

function updateStats(result) {
  const { from, to } = viewport.range;
  const stats = new Map();
  for (const row of rows) {
    const data = result.series.get(row.tagId);
    if (!data) continue;
    let min = Infinity;
    let max = -Infinity;
    let sum = 0;
    let count = 0;
    let last = null;
    // La entrada cacheada guarda hasta 3 ventanas de contexto a cada lado, asi
    // que recorrerla entera en cada frame cuesta de mas. Los ts vienen
    // ordenados: se entra por busqueda binaria y se corta al pasarse.
    for (let i = lowerBound(data.ts, from); i < data.ts.length; i++) {
      if (data.ts[i] > to) break;
      const v = data.avg[i];
      if (v === null || v === undefined) continue;
      // La banda usa el min/max real del bucket, no el de la media: si no, un
      // pico corto desapareceria tambien de la columna "Máx".
      min = Math.min(min, data.min[i] ?? v);
      max = Math.max(max, data.max[i] ?? v);
      sum += v;
      count++;
      last = v;
    }
    if (count) stats.set(row.tagId, { min, max, avg: sum / count, last });
  }
  lastStats = stats;
  table.setStats(stats);
  renderCollapsedStrip(stats);
}

/**
 * Lectura del crosshair, pegada al cursor.
 *
 * El crosshair dispara un evento por cada movimiento del raton, asi que aqui
 * solo se guarda el estado y se pinta una vez por frame. Y las filas del readout
 * se construyen solo cuando cambian las series: rehacer su innerHTML por
 * movimiento es el mismo error que series-table.js ya resolvio con indexCells().
 */
function showCursor(idx, left, top) {
  if (idx === null || idx === undefined) {
    table.setCursor(null);
    cursorState = null;
    readoutEl.hidden = true;
    return;
  }
  cursorState = { idx, left, top };
  if (cursorHandle) return;
  cursorHandle = requestAnimationFrame(paintCursor);
}

function paintCursor() {
  cursorHandle = null;
  const state = cursorState;
  if (!state) return;

  const values = new Map();
  for (const row of rows) values.set(row.tagId, chart.valueAt(row.tagId, state.idx));
  table.setCursor(values);

  buildReadout();
  for (const cell of readoutCells.values()) {
    const value = values.get(cell.tagId);
    const empty = value === null || value === undefined;
    cell.value.textContent = fmtNumber(value, cell.decimals);
    cell.item.classList.toggle("empty", empty);
  }

  const ts = chart.timeAt(state.idx);
  readoutTimeEl.textContent = ts ? fmtDateTime(ts) : "";

  // Se muestra ANTES de colocarlo: oculto no tiene ni offsetParent ni altura,
  // asi que posicionarlo primero lo dejaria clavado en la esquina. El navegador
  // no pinta hasta que termina la tarea, asi que no hay parpadeo.
  readoutEl.hidden = false;
  positionReadout(state.left, state.top);
}

function buildReadout() {
  if (!readoutStale) return;
  readoutStale = false;

  const visible = rows.filter((r) => r.visible);
  const shown = visible.slice(0, READOUT_MAX_ITEMS);
  const hidden = visible.length - shown.length;

  readoutItemsEl.innerHTML = shown
    .map(
      (r) => `<li data-item="${r.tagId}">
        <span class="swatch" style="background:${r.color}"></span>
        <span class="name">${escapeHtml(r.label)}</span>
        <b data-value="${r.tagId}">—</b>
        <span class="unit">${escapeHtml(r.unit || "")}</span>
      </li>`,
    )
    .join("");

  readoutCells = new Map();
  for (const el of readoutItemsEl.querySelectorAll("[data-value]")) {
    const tagId = Number(el.dataset.value);
    const row = shown.find((r) => r.tagId === tagId);
    readoutCells.set(tagId, {
      tagId,
      value: el,
      item: el.closest("li"),
      decimals: row?.decimals ?? row?.tagDecimals ?? 2,
    });
  }

  readoutMoreEl.textContent = hidden > 0 ? `+${hidden} más en la tabla` : "";
  readoutMoreEl.hidden = hidden <= 0;
}

/** Sigue al cursor en ambos ejes, volteando y recortando contra los bordes. */
function positionReadout(cursorLeft, cursorTop) {
  const over = chart.overRect();
  const host = readoutEl.offsetParent?.getBoundingClientRect();
  if (!over || !host) return;

  const flip = cursorLeft > over.width / 2;
  readoutEl.classList.toggle("flip", flip);
  const x = over.left - host.left + cursorLeft + (flip ? -READOUT_OFFSET_PX : READOUT_OFFSET_PX);

  const top = over.top - host.top;
  const height = readoutEl.offsetHeight;
  const y = Math.min(
    Math.max(top + cursorTop + READOUT_OFFSET_PX, top + 4),
    top + over.height - height - 4,
  );

  readoutEl.style.left = `${Math.round(x)}px`;
  readoutEl.style.top = `${Math.round(y)}px`;
}

function renderCollapsedStrip(stats) {
  // Solo es visible con la tabla plegada (gallery.css). Rehacer su innerHTML
  // con la tabla desplegada es trabajo por frame que nadie llega a ver.
  const wrap = document.getElementById("table-wrap");
  if (!wrap.classList.contains("collapsed")) return;
  const strip = document.getElementById("collapsed-strip");
  strip.innerHTML = rows
    .filter((r) => r.visible)
    .map((r) => {
      const stat = stats.get(r.tagId);
      const value = fmtNumber(stat?.last, r.decimals ?? r.tagDecimals ?? 2);
      return `<span class="item">
        <span class="swatch" style="background:${r.color}"></span>
        ${escapeHtml(r.label)} <b>${value}</b> ${escapeHtml(r.unit || "")}
      </span>`;
    })
    .join("");
}

// --- WebSocket --------------------------------------------------------

function connectSocket() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  socket = new WebSocket(`${proto}://${location.host}/ws/live`);

  socket.addEventListener("open", () => subscribe());
  socket.addEventListener("message", (event) => onMessage(JSON.parse(event.data)));
  socket.addEventListener("close", () => setTimeout(connectSocket, 2000));
  socket.addEventListener("error", () => socket.close());
}

function subscribe() {
  if (socket?.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify({ type: "subscribe", tag_ids: visibleTagIds() }));
  }
}

function onMessage(message) {
  if (message.type === "subscribed") {
    subscribedIds = message.tag_ids;
    if (message.gap_open_since) {
      chart.markGapOpen(Date.parse(message.gap_open_since) / 1000);
    }
    return;
  }
  if (message.type === "gap_open") {
    chart.markGapOpen(message.ts);
    return;
  }
  if (message.type === "gap_close") {
    chart.closeGap(message.ts);
    return;
  }
  if (message.type !== "tick") return;

  // Solo interesa el vivo si estamos siguiendo el reloj. Si el usuario esta
  // analizando el pasado, los ticks se ignoran: al pulsar LIVE se rellena todo
  // desde la BD, que es la fuente de verdad.
  if (!viewport.follow) return;

  const byTag = new Map();
  subscribedIds.forEach((tagId, i) => byTag.set(tagId, message.values[i]));
  cache.appendLive(subscribedIds, message.ts, message.values);

  // Se acumulan los ticks y se pinta una vez por frame.
  pendingLive = { ts: message.ts, byTag };
  if (rafHandle) return;
  rafHandle = requestAnimationFrame(() => {
    rafHandle = null;
    const batch = pendingLive;
    pendingLive = null;
    if (batch) chart.appendPoint(batch.ts, batch.byTag, viewport.range);
  });
}

// --- guardado ---------------------------------------------------------

function scheduleSave() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(save, SAVE_DEBOUNCE_MS);
}

async function save() {
  try {
    await api.put(`/api/galleries/${galleryId}/series`, {
      series: rows.map((r) => ({
        tag_id: r.tagId,
        visible: r.visible,
        color: r.color,
        axis_group: r.axisGroup || "auto",
        scale_mode: r.scaleMode,
        y_min: r.scaleMode === "manual" ? r.yMin : null,
        y_max: r.scaleMode === "manual" ? r.yMax : null,
        unit_override: r.unit || null,
        decimals: r.decimals,
        interp: r.interp,
        line_width: r.lineWidth,
        agg: r.agg,
      })),
    });
    subscribe();
  } catch (err) {
    reportError(err);
  }
}

// --- toolbar ----------------------------------------------------------

function renderToolbar() {
  const live = document.getElementById("btn-live");
  live.dataset.live = viewport.follow ? "1" : "0";
  live.querySelector(".dot").className = viewport.follow ? "dot ok" : "dot";
  // La etiqueta la elige el CSS a partir de data-live: las dos estan en el DOM
  // para que el boton no cambie de ancho.
  live.title = viewport.follow
    ? "Siguiendo el reloj. Arrastra el gráfico para analizar el pasado."
    : `Volver al presente (Inicio). Viendo hasta ${fmtDateTime(viewport.to)}`;

  const input = document.getElementById("window-input");
  if (document.activeElement !== input) {
    input.value = viewport.windowText || fmtDuration(viewport.span);
  }

  document.querySelectorAll("#presets button").forEach((button) => {
    button.setAttribute(
      "aria-pressed",
      String(viewport.follow && Math.abs(viewport.span - Number(button.dataset.seconds)) < 1),
    );
  });
}

function setResolution(resolution, loading = false) {
  const el = document.getElementById("resolution");
  el.classList.toggle("loading", loading);
  if (!resolution) {
    el.innerHTML = "";
    return;
  }
  el.innerHTML = `res: <b>${resolution}</b>`;
  el.title =
    resolution === "1s"
      ? "Datos crudos, un punto por ciclo del PLC"
      : `Cada punto resume ${resolution}; la banda sombreada es el mín/máx del intervalo`;
}

function buildToolbar() {
  const presets = document.getElementById("presets");
  presets.innerHTML = PRESETS.map(
    (p) => `<button data-seconds="${p.seconds}">${p.label}</button>`,
  ).join("");
  presets.querySelectorAll("button").forEach((button) =>
    button.addEventListener("click", () => {
      viewport.goLive();
      viewport.setSpan(Number(button.dataset.seconds), button.textContent);
    }),
  );

  document.getElementById("btn-live").addEventListener("click", () => viewport.goLive());
  document.getElementById("btn-back").addEventListener("click", () => viewport.pan(-0.5, "step"));
  document.getElementById("btn-fwd").addEventListener("click", () => viewport.pan(0.5, "step"));
  document.getElementById("btn-reset").addEventListener("click", () => viewport.resetZoom());

  const input = document.getElementById("window-input");
  input.addEventListener("change", async () => {
    const text = input.value.trim();
    try {
      const { seconds } = await api.get(`/api/history/window?w=${encodeURIComponent(text)}`);
      input.setAttribute("aria-invalid", "false");
      viewport.setSpan(seconds, text);
    } catch (err) {
      input.setAttribute("aria-invalid", "true");
      toast(err.detail || "Ventana no válida", true);
    }
  });

  setupRangePopover();
  document.getElementById("btn-csv").addEventListener("click", downloadCsv);
}

function setupRangePopover() {
  const popover = document.getElementById("range-popover");
  const fromInput = document.getElementById("range-from");
  const toInput = document.getElementById("range-to");

  document.getElementById("btn-range").addEventListener("click", () => {
    fromInput.value = toDatetimeLocal(viewport.from);
    toInput.value = toDatetimeLocal(viewport.to);
    popover.hidden = !popover.hidden;
  });
  document.getElementById("range-cancel").addEventListener("click", () => {
    popover.hidden = true;
  });
  document.getElementById("range-apply").addEventListener("click", () => {
    const from = fromDatetimeLocal(fromInput.value);
    const to = fromDatetimeLocal(toInput.value);
    if (from === null || to === null || to <= from) {
      toast("Rango no válido: 'hasta' debe ser posterior a 'desde'", true);
      return;
    }
    popover.hidden = true;
    // Ir a fecha es simplemente fijar el rango: apaga el seguimiento como
    // cualquier otra interaccion, sin ser un modo aparte.
    viewport.setRange(from, to);
  });
}

function downloadCsv() {
  const params = new URLSearchParams({
    tags: visibleTagIds().join(","),
    from: new Date(viewport.from * 1000).toISOString(),
    to: new Date(viewport.to * 1000).toISOString(),
    max_points: String(chart.maxPoints),
  });
  location.href = `/api/export.csv?${params}`;
}

// --- redimension y plegado de la tabla --------------------------------

function setupTableResize() {
  const wrap = document.getElementById("table-wrap");
  const handle = document.getElementById("resize-handle");

  const saved = Number(localStorage.getItem(TABLE_HEIGHT_KEY));
  if (saved > 40) wrap.style.height = `${saved}px`;

  let drag = null;
  handle.addEventListener("mousedown", (e) => {
    drag = { y: e.clientY, height: wrap.offsetHeight, moved: false };
    e.preventDefault();
  });
  window.addEventListener("mousemove", (e) => {
    if (!drag) return;
    drag.moved = true;
    const height = Math.max(60, Math.min(window.innerHeight * 0.7, drag.height - (e.clientY - drag.y)));
    wrap.style.height = `${height}px`;
  });
  window.addEventListener("mouseup", () => {
    if (!drag) return;
    if (!drag.moved) toggleTable();
    else localStorage.setItem(TABLE_HEIGHT_KEY, String(wrap.offsetHeight));
    drag = null;
  });
}

function toggleTable() {
  const collapsed = document.getElementById("table-wrap").classList.toggle("collapsed");
  if (collapsed) renderCollapsedStrip(lastStats);
}

function setupKeyboard() {
  document.addEventListener("keydown", (event) => {
    if (event.target.matches("input, select, textarea")) return;
    // Mismo paso que los botones '‹' y '›': es la misma accion, y tener dos
    // magnitudes distintas para ella hacia impredecible cuanto te desplazabas.
    const step = event.shiftKey ? 1 : 0.5;
    switch (event.key) {
      case "ArrowLeft":
        viewport.pan(-step, "step");
        break;
      case "ArrowRight":
        viewport.pan(step, "step");
        break;
      case "Home":
        viewport.goLive();
        break;
      case "t":
      case "T":
        toggleTable();
        break;
      case "f":
      case "F":
        document.body.classList.toggle("chart-focus");
        break;
      default:
        return;
    }
    event.preventDefault();
  });
}

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text ?? "";
  return div.innerHTML;
}

buildToolbar();
setupTableResize();
setupKeyboard();
boot();
