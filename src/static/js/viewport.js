/**
 * Estado temporal de la vista. Un solo modelo para todo.
 *
 * El prototipo tenia tres modos separados —vivo, pausado e ir a fecha— y esa
 * separacion era la causa de que consultar por fecha fuera incomodo, de que el
 * pan no funcionara y de que un F5 lo perdiera todo. Aqui hay un unico estado:
 *
 *     { to, span, follow }        (from se deriva: to - span)
 *
 * `follow` significa "el borde derecho esta anclado a ahora". Cualquier
 * interaccion con el grafico lo apaga; el boton LIVE es la unica forma de
 * volver a encenderlo. No hay boton "Pausar" porque pausar ES interactuar.
 *
 * El estado se serializa a la URL, de modo que F5 restaura la vista exacta, las
 * flechas atras/adelante del navegador funcionan y el enlace es compartible.
 */

const MIN_SPAN_S = 10;
const MAX_SPAN_S = 5 * 365 * 86400;
const URL_DEBOUNCE_MS = 300;

export const PRESETS = [
  { label: "5m", seconds: 300 },
  { label: "1h", seconds: 3600 },
  { label: "8h", seconds: 28800 },
  { label: "1d", seconds: 86400 },
];

export class Viewport extends EventTarget {
  constructor({ span = 3600, follow = true, to = null } = {}) {
    super();
    this.span = clampSpan(span);
    this.follow = follow;
    this.to = to ?? nowSeconds();
    this.windowText = "1h";
    // Ultimo ancho elegido explicitamente (preset o input). Es a lo que vuelve
    // "restablecer zoom": si escribiste 1d y luego hiciste zoom, vuelve a 1d.
    this.namedSpan = this.span;
    this.namedText = "1h";
    this._urlTimer = null;
  }

  get from() {
    return this.to - this.span;
  }

  get range() {
    return { from: this.from, to: this.to, span: this.span, follow: this.follow };
  }

  // --- transiciones -------------------------------------------------

  /** El reloj avanza: solo mueve la ventana si estamos siguiendo. */
  tick() {
    if (!this.follow) return false;
    this.to = nowSeconds();
    this._emit("tick");
    return true;
  }

  setSpan(seconds, text = null) {
    this.span = clampSpan(seconds);
    this.windowText = text;
    this.namedSpan = this.span;
    this.namedText = text;
    if (this.follow) this.to = nowSeconds();
    this._emit("window");
  }

  /** Vuelve al ancho nombrado, sin tocar si se esta siguiendo el reloj o no. */
  resetZoom() {
    this.span = this.namedSpan;
    this.windowText = this.namedText;
    if (this.follow) this.to = nowSeconds();
    this._emit("window");
  }

  /**
   * Pan como fraccion de la ventana: -0.5 = media ventana hacia atras.
   *
   * `origin` distingue el gesto continuo del discreto, y no es cosmetico: un
   * arrastre emite del orden de 60 pan por segundo y conviene esperar a que
   * pare antes de pedir datos, pero un clic en '‹' es un evento unico y esperar
   * ahi es tiempo muerto puro. Quien escucha decide con esto.
   */
  pan(fraction, origin = "drag") {
    this.to += this.span * fraction;
    this._leaveLive();
    this._emit(origin === "step" ? "pan-step" : "pan");
  }

  /**
   * Zoom manteniendo fijo el instante bajo el cursor.
   * `centerRatio` es 0 en el borde izquierdo y 1 en el derecho.
   */
  zoom(factor, centerRatio = 0.5) {
    const anchor = this.from + this.span * centerRatio;
    const span = clampSpan(this.span * factor);
    if (span === this.span) return;
    this.span = span;
    this.to = anchor + span * (1 - centerRatio);
    this.windowText = null;
    this._leaveLive();
    this._emit("zoom");
  }

  setRange(from, to) {
    if (!(to > from)) return;
    this.span = clampSpan(to - from);
    this.to = from + this.span;
    this.windowText = null;
    this._leaveLive();
    this._emit("range");
  }

  /** Vuelve a seguir el reloj. El backfill lo hace quien escucha el evento. */
  goLive() {
    if (this.follow) return;
    this.follow = true;
    this.to = nowSeconds();
    this._emit("live");
  }

  _leaveLive() {
    // Pausar no es un modo aparte: es simplemente dejar de seguir el reloj.
    this.follow = false;
  }

  _emit(reason) {
    this._scheduleUrlSync();
    this.dispatchEvent(new CustomEvent("change", { detail: { reason, ...this.range } }));
  }

  // --- persistencia en la URL ---------------------------------------

  _scheduleUrlSync() {
    clearTimeout(this._urlTimer);
    this._urlTimer = setTimeout(() => this.syncUrl(), URL_DEBOUNCE_MS);
  }

  syncUrl() {
    const params = new URLSearchParams();
    if (this.follow) {
      params.set("live", "1");
      params.set("w", this.windowText || `${Math.round(this.span)}s`);
    } else {
      params.set("from", String(Math.round(this.from)));
      params.set("to", String(Math.round(this.to)));
    }
    history.replaceState(null, "", `${location.pathname}?${params}`);
  }

  /**
   * Restaura desde la URL. Devuelve el token de ventana pendiente de resolver
   * en el servidor (p.ej. "15m"), o null si ya quedo todo resuelto.
   */
  static fromUrl() {
    const params = new URLSearchParams(location.search);
    const from = Number(params.get("from"));
    const to = Number(params.get("to"));

    if (Number.isFinite(from) && Number.isFinite(to) && to > from && from > 0) {
      const vp = new Viewport({ span: to - from, follow: false, to });
      vp.windowText = null;
      return { viewport: vp, pendingWindow: null };
    }

    const token = params.get("w");
    const preset = PRESETS.find((p) => p.label === token);
    const vp = new Viewport({ span: preset ? preset.seconds : 3600, follow: true });
    vp.windowText = token || "1h";
    // Un token no estandar ("45m") lo resuelve el servidor: el parser vive alli.
    return { viewport: vp, pendingWindow: preset || !token ? null : token };
  }
}

export function nowSeconds() {
  return Date.now() / 1000;
}

function clampSpan(seconds) {
  return Math.min(MAX_SPAN_S, Math.max(MIN_SPAN_S, seconds));
}
