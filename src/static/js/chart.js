/**
 * Grafico de tendencias sobre uPlot.
 *
 * Decisiones que vienen del dominio industrial:
 *  - El readout del crosshair es minimo y se autolimita (hora + un maximo de
 *    series). Tener la lectura pegada al cursor evita apartar la vista del punto
 *    que se esta inspeccionando; la tabla de abajo sigue siendo la lectura
 *    completa y persistente, y es la unica cuando la mano no esta en el grafico.
 *  - Los huecos de adquisicion se pintan como banda, no como valle a cero: un
 *    cero destruiria la escala y seria indistinguible de un cero real.
 *  - Lo que aun no ha llegado de la red se pinta en gris como "Cargando…", NUNCA
 *    con el rojo de los huecos: confundir "todavia no lo tengo" con "aqui no
 *    hubo dato" es el peor fallo posible en un historiador.
 *  - Al alejar el zoom se dibuja la envolvente min-max detras de la media, para
 *    que un pico de 2 segundos siga siendo visible en una ventana de un mes.
 *  - Los tags digitales van en carriles al pie del area, nunca en el eje
 *    analogico: un booleano 0/1 aplastaria la escala de una temperatura.
 */

// `lowerBound` vive en cache.js porque alli nacio y alli esta cubierto por
// tests; se reutiliza aqui en vez de duplicar la busqueda binaria.
import { lowerBound } from "./cache.js";
import { axisLabel, fmtNumber } from "./format.js";
import { buildScaleConfigs } from "./scales.js";

const DIGITAL_AREA_RATIO = 0.25; // fraccion del alto reservada a los carriles
const LANE_FILL = 0.7;

// Por debajo de esto una banda de "cargando" es una raya sin informacion.
const MIN_PENDING_PX = 20;

// Marcadores de punto solo si caben pocos EN LA VENTANA (no en la cache).
const MAX_POINTS_FOR_MARKERS = 120;

const COLORS = {
  axis: "#898781",
  grid: "#2c2c2a",
  baseline: "#383835",
  gapFill: "rgba(208, 59, 59, 0.14)",
  gapEdge: "rgba(208, 59, 59, 0.55)",
  gapText: "#f09a9a",
  pendingFill: "rgba(255, 255, 255, 0.035)",
  pendingEdge: "rgba(255, 255, 255, 0.12)",
  pendingText: "#898781",
};

export class TrendChart {
  /**
   * @param {HTMLElement} host
   * @param {{onCursor:Function, onPan:Function, onZoom:Function, onSelect:Function}} handlers
   */
  constructor(host, handlers = {}) {
    this.host = host;
    this.handlers = handlers;
    this.plot = null;
    this.config = [];
    this.bands = new Map(); // tagId -> {min:[], max:[]}
    this.gaps = [];
    this.pending = []; // tramos pedidos pero aun no recibidos
    this.openGapSince = null;
    this.aggregated = false;
    this.digitalOrder = [];
    this.rawValues = new Map(); // tagId -> array alineado con el eje x
    // El trazo actual no corresponde a la configuracion de series vigente.
    // Se levanta en cada reconstruccion y solo `setData` lo baja.
    this._dataStale = true;

    this._observer = new ResizeObserver(() => this._resize());
    this._observer.observe(host);
  }

  destroy() {
    this._observer.disconnect();
    this._listeners?.abort();
    this.plot?.destroy();
    this.plot = null;
  }

  /** Puntos objetivo = pixeles de ancho: no tiene sentido pedir mas. */
  get maxPoints() {
    return Math.max(300, Math.min(3000, Math.round(this.host.clientWidth || 1200)));
  }

  // --- configuracion de series --------------------------------------

  setSeries(config) {
    const signature = JSON.stringify(
      config.map((s) => [
        s.tagId, s.visible, s.color, s.axisGroup, s.scaleMode,
        s.yMin, s.yMax, s.interp, s.lineWidth, s.kind, s.label, s.unit,
      ]),
    );
    if (signature === this._signature) return;
    this._signature = signature;
    this.config = config;
    this._rebuild();
  }

  // --- datos ---------------------------------------------------------

  setData(result, range) {
    this.aggregated = result.aggregated;
    this.gaps = result.gaps || [];
    const visible = this.config.filter((s) => s.visible);
    const { x, columns, bands, raw } = buildData(result.series, visible);
    this.bands = bands;
    this.rawValues = raw;

    if (!this.plot) this._rebuild();
    if (!this.plot) return;

    this.plot.setData([x, ...columns], false);
    this._dataStale = false;
    this.setRange(range.from, range.to);
  }

  /**
   * True si el trazo dibujado sigue correspondiendo a las series vigentes.
   *
   * Sirve para saltarse un `setData` que reconstruiria columnas identicas. No
   * basta con "no hay datos nuevos": al ocultar una serie, uPlot conserva las
   * columnas por INDICE, no por tag, asi que reutilizar el trazo tras una
   * reconstruccion asignaria los valores a la serie equivocada.
   */
  canReuseData() {
    return !!this.plot && !this._dataStale;
  }

  /**
   * Tramos de la ventana pedidos pero aun no recibidos.
   *
   * `redraw = false` para quien va a provocar un redibujado justo despues (un
   * `setRange`, por ejemplo): durante un arrastre estos tramos cambian en cada
   * frame y repintar dos veces por frame es justo lo que se quiere evitar.
   */
  setPending(ranges, redraw = true) {
    const next = ranges || [];
    if (samePending(this.pending, next)) return;
    this.pending = next;
    if (redraw) this.plot?.redraw(false, false);
  }

  setRange(from, to) {
    this.plot?.setScale("x", { min: from, max: to });
  }

  /** Geometria del area de trazo en pixeles CSS, para posicionar el readout. */
  overRect() {
    return this.plot?.over?.getBoundingClientRect() ?? null;
  }

  /** Un tick de WebSocket: se agrega al final sin reconstruir nada. */
  appendPoint(ts, valuesByTag, range) {
    if (!this.plot) return;
    const data = this.plot.data;
    const visible = this.config.filter((s) => s.visible);
    const xs = data[0].slice();
    if (xs.length && ts <= xs[xs.length - 1]) return;
    xs.push(ts);

    // Se recorta por la izquierda o una sesion abierta toda la jornada acabaria
    // acumulando decenas de miles de puntos que ya nadie va a ver.
    const keepFrom = range.from - (range.to - range.from);
    let cut = 0;
    while (cut < xs.length && xs[cut] < keepFrom) cut++;

    const columns = visible.map((s, i) => {
      const value = valuesByTag.get(s.tagId);
      const column = data[i + 1].slice();
      column.push(value === undefined ? null : this._project(s, value));
      const raw = this.rawValues.get(s.tagId);
      if (raw) {
        raw.push(value === undefined ? null : value);
        if (cut) raw.splice(0, cut);
      }
      return cut ? column.slice(cut) : column;
    });

    this.plot.setData([cut ? xs.slice(cut) : xs, ...columns], false);
    this.setRange(range.from, range.to);
  }

  /** El PLC acaba de caerse: el hueco sigue creciendo hasta que se recupere. */
  markGapOpen(ts) {
    this.openGapSince = ts;
    this.plot?.redraw(false, false);
  }

  closeGap(ts) {
    if (this.openGapSince !== null && this.openGapSince !== undefined) {
      this.gaps = [...this.gaps, [this.openGapSince, ts]];
      this.openGapSince = null;
    }
    this.plot?.redraw(false, false);
  }

  _project(seriesCfg, value) {
    if (seriesCfg.kind !== "digital" || value === null) return value;
    const lane = this.digitalOrder.indexOf(seriesCfg.tagId);
    return lane < 0 ? value : lane + 0.15 + value * LANE_FILL;
  }

  /** Valor real (sin proyectar) de una serie en un indice del eje x. */
  valueAt(tagId, idx) {
    const arr = this.rawValues.get(tagId);
    return arr && idx != null ? arr[idx] : null;
  }

  timeAt(idx) {
    return this.plot && idx != null ? this.plot.data[0][idx] : null;
  }

  // --- construccion de uPlot -----------------------------------------

  _rebuild() {
    const previous = this.plot?.data;
    this.plot?.destroy();
    this.host.innerHTML = "";
    // Los datos que se reinyectan abajo estan alineados por indice con la
    // configuracion ANTERIOR: hasta que llegue un setData no son de fiar.
    this._dataStale = true;

    const visible = this.config.filter((s) => s.visible);
    if (!visible.length) {
      this.host.innerHTML =
        '<div class="chart-empty">Agrega tags en la tabla inferior para ver la tendencia.</div>';
      this.plot = null;
      return;
    }

    this.digitalOrder = visible.filter((s) => s.kind === "digital").map((s) => s.tagId);
    const scales = this._buildScales(visible);
    const axes = this._buildAxes(visible, scales);

    const opts = {
      width: this.host.clientWidth || 800,
      height: this.host.clientHeight || 400,
      padding: [12, 8, 0, 0],
      legend: { show: false }, // la leyenda es la tabla de abajo
      cursor: {
        x: true,
        y: false,
        // El zoom por arrastre lo maneja el viewport, no uPlot: la fuente de
        // verdad del rango temporal debe ser una sola.
        drag: { x: true, y: false, setScale: false },
        points: { size: 6 },
      },
      scales: { x: { time: true }, ...scales },
      series: [
        { value: () => "" },
        ...visible.map((s) => ({
          label: s.label,
          scale: s.kind === "digital" ? "__dig" : s.scaleKey,
          stroke: s.color,
          width: s.lineWidth || 2,
          spanGaps: false, // un null corta la linea: asi se ve la perdida
          paths:
            s.interp === "step" || s.kind === "digital"
              ? uPlot.paths.stepped({ align: 1 })
              : undefined,
          // Marcadores solo cuando hay pocos puntos: con miles serian ruido.
          // Se cuentan los que caen DENTRO de la ventana, no la longitud de
          // `u.data[0]`: ese array es toda la entrada de cache (hasta siete
          // ventanas), asi que medirlo hacia que no aparecieran nunca.
          points: { show: (u) => visibleCount(u) < MAX_POINTS_FOR_MARKERS },
        })),
      ],
      axes,
      hooks: {
        drawClear: [(u) => this._drawUnder(u)],
        draw: [(u) => this._drawOver(u)],
        // left/top van en pixeles CSS relativos al area de trazo, que es lo que
        // necesita el readout. Ojo con mezclarlos con `u.bbox`, que esta en
        // pixeles de canvas (el mismo lio de devicePixelRatio del pan).
        setCursor: [(u) => this.handlers.onCursor?.(u.cursor.idx, u.cursor.left, u.cursor.top)],
        setSelect: [(u) => this._onSelect(u)],
      },
    };

    // uPlot indexa data[i] por serie. Si `previous` viene de una configuracion
    // con menos series —justo lo que pasa al anadir una— el constructor lee un
    // undefined y lanza antes de que se asigne this.plot, dejando el grafico
    // apuntando a una instancia ya destruida: a partir de ahi solo lo arregla un
    // F5. Se ajusta el ancho de los datos previos antes de pasarlos.
    try {
      this.plot = new uPlot(opts, fitColumns(previous, visible.length), this.host);
    } catch (err) {
      console.error("uPlot no pudo reconstruirse con los datos previos", err);
      this.host.innerHTML = "";
      this.plot = new uPlot(opts, fitColumns(null, visible.length), this.host);
    }
    this._attachInteractions();
  }

  _buildScales(visible) {
    const scales = buildScaleConfigs(visible.filter((s) => s.kind !== "digital"));
    if (this.digitalOrder.length) {
      // Rango estirado para que los carriles ocupen solo la franja inferior.
      const lanes = this.digitalOrder.length;
      scales.__dig = { auto: false, range: [0, lanes / DIGITAL_AREA_RATIO] };
    }
    return scales;
  }

  _buildAxes(visible, scales) {
    const xAxis = {
      stroke: COLORS.axis,
      grid: { stroke: COLORS.grid, width: 1 },
      ticks: { stroke: COLORS.baseline },
      font: '11px system-ui, sans-serif',
      values: (u, splits) => {
        const span = u.scales.x.max - u.scales.x.min;
        return splits.map((v) => axisLabel(v, span));
      },
    };

    // Solo dos ejes visibles (izquierda y derecha). Mas ejes se comen el ancho
    // del trazo, que es lo que se quiere maximizar; los demas grupos conservan
    // su escala propia aunque no muestren regla.
    const groupKeys = [...new Set(visible.filter((s) => s.kind !== "digital").map((s) => s.scaleKey))];
    const yAxes = groupKeys.slice(0, 2).map((key, i) => {
      const owner = visible.find((s) => s.scaleKey === key);
      return {
        scale: key,
        side: i === 0 ? 3 : 1,
        stroke: COLORS.axis,
        grid: { stroke: i === 0 ? COLORS.grid : "transparent", width: 1 },
        ticks: { stroke: COLORS.baseline },
        font: '11px system-ui, sans-serif',
        size: 54,
        label: owner?.unit || undefined,
        labelSize: owner?.unit ? 16 : 0,
        labelFont: '11px system-ui, sans-serif',
        values: (u, splits) => splits.map((v) => fmtNumber(v, decimalsFor(splits))),
      };
    });

    if (scales.__dig) {
      yAxes.push({ scale: "__dig", show: false });
    }
    return [xAxis, ...yAxes];
  }

  // --- interacciones --------------------------------------------------

  _attachInteractions() {
    // Se reengancha en cada reconstruccion: el AbortController evita acumular
    // listeners de window en cada cambio de configuracion de series.
    this._listeners?.abort();
    this._listeners = new AbortController();
    const { signal } = this._listeners;
    const over = this.plot.over;

    // Arrastrar = pan. Es la accion mas frecuente, asi que es la primaria.
    let dragging = null;
    over.addEventListener(
      "mousedown",
      (e) => {
        if (e.button !== 0 || e.shiftKey) return; // shift+arrastre = zoom a rango
        dragging = { x: e.clientX };
        over.style.cursor = "grabbing";
      },
      { signal },
    );
    window.addEventListener(
      "mousemove",
      (e) => {
        if (!dragging) return;
        const dx = e.clientX - dragging.x;
        if (Math.abs(dx) < 2) return;
        dragging.x = e.clientX;
        // bbox esta en pixeles de canvas; dx en pixeles CSS.
        const cssWidth = this.plot.bbox.width / devicePixelRatio;
        this.handlers.onPan?.(-dx / cssWidth);
      },
      { signal },
    );
    window.addEventListener(
      "mouseup",
      () => {
        if (!dragging) return;
        dragging = null;
        over.style.cursor = "";
      },
      { signal },
    );

    // Rueda = zoom en X centrado en el cursor.
    over.addEventListener(
      "wheel",
      (e) => {
        e.preventDefault();
        const rect = over.getBoundingClientRect();
        const ratio = Math.min(1, Math.max(0, (e.clientX - rect.left) / rect.width));
        this.handlers.onZoom?.(e.deltaY > 0 ? 1.25 : 0.8, ratio);
      },
      { passive: false, signal },
    );

    over.addEventListener("dblclick", () => this.handlers.onReset?.(), { signal });
  }

  _onSelect(u) {
    const { left, width } = u.select;
    if (width < 8) return;
    const from = u.posToVal(left, "x");
    const to = u.posToVal(left + width, "x");
    u.setSelect({ left: 0, width: 0, top: 0, height: 0 }, false);
    this.handlers.onSelect?.(from, to);
  }

  // --- dibujo ---------------------------------------------------------

  /** Debajo de las series: envolvente min-max y relleno de los huecos. */
  _drawUnder(u) {
    const ctx = u.ctx;
    ctx.save();
    ctx.beginPath();
    ctx.rect(u.bbox.left, u.bbox.top, u.bbox.width, u.bbox.height);
    ctx.clip();

    this._drawPendingFills(u, ctx);
    if (this.aggregated) this._drawBands(u, ctx);
    this._drawGapFills(u, ctx);

    ctx.restore();
  }

  /**
   * Envolvente min-max, acotada al tramo visible.
   *
   * Esto corre en el hook `drawClear`, o sea en CADA redibujado: cada frame de
   * un arrastre y cada segundo en vivo. `u.data[0]` es la entrada de cache
   * entera (hasta siete ventanas), asi que recorrerla completa por serie y en
   * dos pasadas era trabajo proporcional a lo cacheado y no a lo que se ve.
   */
  _drawBands(u, ctx) {
    const xs = u.data[0];
    if (!xs.length) return;
    const [lo, hi] = visibleSlice(u);
    if (hi <= lo) return;

    const visible = this.config.filter((s) => s.visible);
    visible.forEach((s) => {
      if (s.kind === "digital") return;
      const band = this.bands.get(s.tagId);
      if (!band) return;

      ctx.beginPath();
      let started = false;
      for (let i = lo; i < hi; i++) {
        if (band.max[i] === null || band.max[i] === undefined) continue;
        const x = u.valToPos(xs[i], "x", true);
        const y = u.valToPos(band.max[i], s.scaleKey, true);
        started ? ctx.lineTo(x, y) : (ctx.moveTo(x, y), (started = true));
      }
      for (let i = hi - 1; i >= lo; i--) {
        if (band.min[i] === null || band.min[i] === undefined) continue;
        ctx.lineTo(u.valToPos(xs[i], "x", true), u.valToPos(band.min[i], s.scaleKey, true));
      }
      if (!started) return;
      ctx.closePath();
      ctx.fillStyle = withAlpha(s.color, 0.18);
      ctx.fill();
    });
  }

  /** Lo pedido y aun no recibido. Gris: no es un hueco, es que no ha llegado. */
  _drawPendingFills(u, ctx) {
    if (!this.pending.length) return;
    ctx.save();
    ctx.fillStyle = COLORS.pendingFill;
    ctx.strokeStyle = COLORS.pendingEdge;
    ctx.lineWidth = 1;
    ctx.setLineDash([4 * devicePixelRatio, 4 * devicePixelRatio]);
    for (const { from, to } of this.pending) {
      const [left, width] = clipToPlot(u, from, to);
      if (width < MIN_PENDING_PX * devicePixelRatio) continue;
      ctx.fillRect(left, u.bbox.top, width, u.bbox.height);
      ctx.beginPath();
      ctx.moveTo(left, u.bbox.top);
      ctx.lineTo(left, u.bbox.top + u.bbox.height);
      ctx.moveTo(left + width, u.bbox.top);
      ctx.lineTo(left + width, u.bbox.top + u.bbox.height);
      ctx.stroke();
    }
    ctx.restore();
  }

  /** Huecos cerrados mas, si lo hay, el que sigue abierto hasta el borde. */
  _allGaps(u) {
    if (this.openGapSince === null || this.openGapSince === undefined) return this.gaps;
    return [...this.gaps, [this.openGapSince, u.scales.x.max]];
  }

  _drawGapFills(u, ctx) {
    for (const [from, to] of this._allGaps(u)) {
      const x1 = u.valToPos(from, "x", true);
      const x2 = u.valToPos(to, "x", true);
      if (x2 < u.bbox.left || x1 > u.bbox.left + u.bbox.width) continue;
      const left = Math.max(x1, u.bbox.left);
      const width = Math.max(2, Math.min(x2, u.bbox.left + u.bbox.width) - left);
      ctx.fillStyle = COLORS.gapFill;
      ctx.fillRect(left, u.bbox.top, width, u.bbox.height);
      ctx.strokeStyle = COLORS.gapEdge;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(left, u.bbox.top);
      ctx.lineTo(left, u.bbox.top + u.bbox.height);
      ctx.moveTo(left + width, u.bbox.top);
      ctx.lineTo(left + width, u.bbox.top + u.bbox.height);
      ctx.stroke();
    }
  }

  /** Encima: etiquetas de hueco y separadores de los carriles digitales. */
  _drawOver(u) {
    const ctx = u.ctx;
    ctx.save();
    ctx.beginPath();
    ctx.rect(u.bbox.left, u.bbox.top, u.bbox.width, u.bbox.height);
    ctx.clip();

    ctx.font = `${11 * devicePixelRatio}px system-ui, sans-serif`;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";

    ctx.fillStyle = COLORS.gapText;
    for (const [from, to] of this._allGaps(u)) {
      const x1 = Math.max(u.valToPos(from, "x", true), u.bbox.left);
      const x2 = Math.min(u.valToPos(to, "x", true), u.bbox.left + u.bbox.width);
      if (x2 - x1 > 70 * devicePixelRatio) {
        ctx.fillText("SIN DATO", (x1 + x2) / 2, u.bbox.top + 14 * devicePixelRatio);
      }
    }

    // Etiqueta distinta y color distinto que "SIN DATO", a proposito.
    ctx.fillStyle = COLORS.pendingText;
    for (const { from, to } of this.pending) {
      const [left, width] = clipToPlot(u, from, to);
      if (width > 70 * devicePixelRatio) {
        ctx.fillText("Cargando…", left + width / 2, u.bbox.top + 14 * devicePixelRatio);
      }
    }

    this._drawDigitalLanes(u, ctx);
    ctx.restore();
  }

  _drawDigitalLanes(u, ctx) {
    if (!this.digitalOrder.length) return;
    const visible = this.config.filter((s) => s.visible);
    ctx.textAlign = "left";
    ctx.font = `${10 * devicePixelRatio}px system-ui, sans-serif`;

    this.digitalOrder.forEach((tagId, lane) => {
      const cfg = visible.find((s) => s.tagId === tagId);
      if (!cfg) return;
      const yBase = u.valToPos(lane, "__dig", true);
      const yTop = u.valToPos(lane + 1, "__dig", true);

      ctx.strokeStyle = COLORS.grid;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(u.bbox.left, yBase);
      ctx.lineTo(u.bbox.left + u.bbox.width, yBase);
      ctx.stroke();

      ctx.fillStyle = cfg.color;
      ctx.fillText(cfg.label, u.bbox.left + 6 * devicePixelRatio, (yBase + yTop) / 2);
    });
  }

  _resize() {
    if (!this.plot) return;
    const { clientWidth, clientHeight } = this.host;
    if (clientWidth > 0 && clientHeight > 0) {
      this.plot.setSize({ width: clientWidth, height: clientHeight });
    }
  }
}

// --- helpers ----------------------------------------------------------

/**
 * Rango de indices [lo, hi) de `u.data[0]` que cae dentro de la ventana.
 *
 * Se ensancha un punto a cada lado para que el trazo entre por el borde en vez
 * de nacer dentro del area.
 */
function visibleSlice(u) {
  const xs = u.data[0];
  const { min, max } = u.scales.x;
  // En el primer dibujado la escala aun no tiene rango; acotar contra un
  // undefined daria una rebanada de un punto.
  if (!Number.isFinite(min) || !Number.isFinite(max)) return [0, xs.length];
  const lo = Math.max(0, lowerBound(xs, min) - 1);
  const hi = Math.min(xs.length, lowerBound(xs, max) + 1);
  return [lo, hi];
}

function visibleCount(u) {
  if (!u.data[0]?.length) return 0;
  const [lo, hi] = visibleSlice(u);
  return hi - lo;
}

function samePending(a, b) {
  if (a.length !== b.length) return false;
  return a.every((iv, i) => iv.from === b[i].from && iv.to === b[i].to);
}

/** [left, width] en pixeles de canvas de un intervalo temporal, recortado. */
function clipToPlot(u, from, to) {
  const right = u.bbox.left + u.bbox.width;
  const left = Math.max(u.valToPos(from, "x", true), u.bbox.left);
  return [left, Math.min(u.valToPos(to, "x", true), right) - left];
}

/** Construye la malla x comun y una columna por serie visible. */
function buildData(seriesMap, visible) {
  const arrays = visible.map((s) => seriesMap.get(s.tagId)).filter(Boolean);
  if (!arrays.length) {
    return { x: [], columns: visible.map(() => []), bands: new Map(), raw: new Map() };
  }

  // Camino rapido: con datos crudos todas las series comparten los timestamps
  // del ciclo, asi que no hace falta calcular la union.
  const first = arrays[0].ts;
  const identical = arrays.every(
    (a) => a.ts.length === first.length && a.ts[0] === first[0] && a.ts[a.ts.length - 1] === first[first.length - 1],
  );
  const x = identical ? first.slice() : [...new Set(arrays.flatMap((a) => a.ts))].sort((a, b) => a - b);
  const index = identical ? null : new Map(x.map((t, i) => [t, i]));

  const bands = new Map();
  const raw = new Map();
  const digitalOrder = visible.filter((s) => s.kind === "digital").map((s) => s.tagId);

  const columns = visible.map((s) => {
    const data = seriesMap.get(s.tagId);
    const column = new Array(x.length).fill(null);
    const rawColumn = new Array(x.length).fill(null);
    if (!data) {
      raw.set(s.tagId, rawColumn);
      return column;
    }

    const min = new Array(x.length).fill(null);
    const max = new Array(x.length).fill(null);
    const lane = digitalOrder.indexOf(s.tagId);

    for (let i = 0; i < data.ts.length; i++) {
      const at = identical ? i : index.get(data.ts[i]);
      if (at === undefined) continue;
      const value = data.avg[i];
      rawColumn[at] = value;
      column[at] =
        lane >= 0 && value !== null ? lane + 0.15 + value * LANE_FILL : value;
      min[at] = data.min[i];
      max[at] = data.max[i];
    }
    if (lane < 0) bands.set(s.tagId, { min, max });
    raw.set(s.tagId, rawColumn);
    return column;
  });

  return { x, columns, bands, raw };
}

/** Datos previos ajustados a `count` series: rellena las que falten con nulls. */
function fitColumns(data, count) {
  const x = data?.[0] ?? [];
  const columns = [];
  for (let i = 0; i < count; i++) {
    const column = data?.[i + 1];
    columns.push(column?.length === x.length ? column : new Array(x.length).fill(null));
  }
  return [x, ...columns];
}

function withAlpha(hex, alpha) {
  const n = parseInt(hex.slice(1), 16);
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
}

function decimalsFor(splits) {
  const step = Math.abs((splits[1] ?? 1) - (splits[0] ?? 0));
  if (step >= 10) return 0;
  if (step >= 1) return 1;
  return 2;
}
