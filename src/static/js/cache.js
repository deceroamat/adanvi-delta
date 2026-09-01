/**
 * Cache de tramos de historico.
 *
 * Al hacer pan, el 90% de lo que se ve ya estaba cargado. Sin cache cada
 * arrastre repide la ventana entera y el grafico parpadea; con ella solo se pide
 * el trozo nuevo. Junto con AbortController (cancelar lo que ya no interesa) es
 * lo que separa un pan "laggy" de uno que se siente nativo.
 *
 * Dos invariantes que costaron caro aprender:
 *
 * 1. La cobertura es POR TAG, no por entrada. Indexar solo por rango de tiempo
 *    hacia que activar una serie sobre una ventana ya cubierta no pidiera nada:
 *    la serie se quedaba vacia hasta que el usuario recargaba la pagina.
 *
 * 2. Un tramo solo se puede mezclar con otro de SU MISMA resolucion. El servidor
 *    deriva el bucket del span de la peticion, y el trozo que falta tras un pan
 *    es mas corto que la ventana: pedirlo tal cual devuelve una resolucion mas
 *    fina que no encaja con lo cacheado. Por eso al ampliar una entrada se le
 *    pide al servidor el bucket que ya se tiene (ver `pointsForBucket`).
 *
 * La clave de cache sigue siendo el `bucket_s` que devolvio el servidor: durante
 * un pan el span no cambia, luego el bucket tampoco, y los tramos se reutilizan.
 * Un zoom si cambia el bucket: ahi el fallo de cache es esperado y correcto.
 */

import { api } from "./api.js";

// Se guardan hasta 3 ventanas de contexto a cada lado antes de podar.
const KEEP_SPANS = 3;

// Espejo de timeparse.MIN_MAX_POINTS / HARD_MAX_POINTS del servidor. Solo se
// usan para reproducir un bucket concreto; la fase que anada `bucket_s` al API
// deja de necesitarlos.
const MIN_SERVER_POINTS = 200;
const MAX_SERVER_POINTS = 5000;

export class HistoryCache {
  constructor() {
    this.entries = new Map(); // bucketS -> entry
    this.lastSpan = null;
    this.lastBucket = null;
    this.controller = null;
    // Controller propio para el prefetch, separado del interactivo a proposito:
    // `load()` aborta el suyo en cada llamada, asi que compartirlo significaria
    // que cada pan mata la precarga en vuelo y que la precarga puede cancelar
    // justo la peticion que el usuario esta esperando.
    this.prefetchController = null;
  }

  abort() {
    this.controller?.abort();
    this.controller = null;
  }

  abortPrefetch() {
    this.prefetchController?.abort();
    this.prefetchController = null;
  }

  /**
   * Lo que se puede pintar YA, sin esperar a la red.
   *
   * `missing` son los tramos de la ventana que aun no se han traido. La vista
   * los pinta como "cargando" en vez de dejarlos en blanco: en un historiador,
   * un blanco sin explicar se lee como "aqui no hubo dato".
   */
  peek(from, to) {
    const entry = this.entries.get(this.lastBucket);
    if (!entry) return null;
    const missing = subtract(from, to, entry.covered);
    return { ...describe(entry), stale: missing.length > 0, missing };
  }

  /**
   * Carga [from, to] para `tagIds` pidiendo solo lo que falta.
   * Lanza AbortError si una peticion posterior la reemplaza.
   */
  async load({ tagIds, from, to, maxPoints }) {
    this.abort();
    const controller = new AbortController();
    this.controller = controller;

    // Durante un pan el span es identico al anterior, asi que la entrada de esa
    // resolucion sigue siendo la buena y solo hay que completarla.
    const span = to - from;
    const sameZoom = this.lastSpan !== null && Math.abs(span - this.lastSpan) < 1;
    let merged = sameZoom ? this.entries.get(this.lastBucket) : null;

    const groups = merged
      ? missingGroups(merged, tagIds, from, to)
      : [{ tags: tagIds, ranges: [{ from, to }] }];

    if (merged && !groups.length) {
      this.controller = null;
      return { ...describe(merged), fromCache: true };
    }

    // Si el servidor devuelve otra resolucion pese a habersela pedido, mezclar
    // seria pintar dos escalas de tiempo en el mismo trazo: se empieza limpio.
    let restart = false;

    for (const group of groups) {
      for (const range of group.ranges) {
        const payload = await this._fetch({
          tagIds: group.tags,
          range,
          maxPoints,
          bucketS: merged?.bucketS ?? null,
          signal: controller.signal,
        });
        if (!merged) {
          merged = this.entries.get(payload.bucket_s) || newEntry(payload);
          this.entries.set(payload.bucket_s, merged);
        } else if (merged.bucketS !== payload.bucket_s) {
          restart = true;
          break;
        }
        absorb(merged, payload, group.tags);
      }
      if (restart) break;
    }

    if (restart) {
      const payload = await this._fetch({
        tagIds,
        range: { from, to },
        maxPoints,
        bucketS: null,
        signal: controller.signal,
      });
      merged = newEntry(payload);
      this.entries.set(payload.bucket_s, merged);
      absorb(merged, payload, tagIds);
    }

    this.lastSpan = span;
    this.lastBucket = merged.bucketS;
    prune(merged, from - span * KEEP_SPANS, to + span * KEEP_SPANS);
    this.controller = null;
    return { ...describe(merged), fromCache: false };
  }

  /**
   * Trae en segundo plano lo que falte de [from, to] sin estorbar a `load()`.
   *
   * Solo amplia la entrada activa. Si no hay ninguna, o si el usuario hizo zoom
   * mientras la peticion viajaba, se abandona: el tramo vendria con otro bucket
   * y mezclarlo es exactamente lo que prohibe la invariante 2 de este modulo.
   *
   * Devuelve true si absorbio algo.
   */
  async prefetch({ tagIds, from, to, maxPoints }) {
    const entry = this.entries.get(this.lastBucket);
    if (!entry) return false;

    const groups = missingGroups(entry, tagIds, from, to);
    if (!groups.length) return false;

    this.abortPrefetch();
    const controller = new AbortController();
    this.prefetchController = controller;

    let absorbed = false;
    try {
      for (const group of groups) {
        for (const range of group.ranges) {
          const payload = await this._fetch({
            tagIds: group.tags,
            range,
            maxPoints,
            bucketS: entry.bucketS,
            signal: controller.signal,
          });
          // El await es el hueco por donde puede colarse un zoom del usuario.
          if (this.entries.get(this.lastBucket) !== entry) return absorbed;
          if (payload.bucket_s !== entry.bucketS) return absorbed;
          absorb(entry, payload, group.tags);
          absorbed = true;
        }
      }
    } catch (err) {
      if (err?.name !== "AbortError") throw err;
    } finally {
      if (this.prefetchController === controller) this.prefetchController = null;
    }
    return absorbed;
  }

  async _fetch({ tagIds, range, maxPoints, bucketS, signal }) {
    let { from, to } = range;
    let points = maxPoints;

    if (bucketS) {
      const pinned = pointsForBucket(to - from, bucketS);
      if (pinned === null) {
        // El trozo es tan corto que ningun max_points valido expresa el bucket.
        // Se ensancha hacia el pasado, que es dato inmutable y siempre seguro
        // de cachear, hasta que quepan los puntos minimos.
        from = to - bucketS * MIN_SERVER_POINTS;
        points = MIN_SERVER_POINTS;
      } else {
        points = pinned;
      }
    }

    const params = new URLSearchParams({
      tags: tagIds.join(","),
      from: new Date(from * 1000).toISOString(),
      to: new Date(to * 1000).toISOString(),
      max_points: String(points),
    });
    return api.get(`/api/history?${params}`, { signal });
  }

  /** Un punto que llega por WebSocket entra en la entrada de resolucion cruda. */
  appendLive(tagIds, ts, values) {
    const entry = this.entries.get(this.lastBucket);
    if (!entry || entry.bucketS > 1) return false;
    tagIds.forEach((tagId, i) => {
      const series = entry.tags.get(tagId);
      if (!series) return;
      const last = series.ts.length - 1;
      if (last >= 0 && ts <= series.ts[last]) return; // fuera de orden: se ignora
      series.ts.push(ts);
      series.avg.push(values[i]);
      series.min.push(values[i]);
      series.max.push(values[i]);
      extendLast(series.covered, ts);
    });
    extendLast(entry.covered, ts);
    return true;
  }
}

// --- peticiones -------------------------------------------------------

/**
 * `max_points` que hace que el servidor elija exactamente `bucketS` para un
 * rango de `spanS` segundos, o null si no hay ninguno valido.
 *
 * El servidor calcula `snap_bucket(span / max_points)`, y `snap_bucket` redondea
 * hacia ARRIBA al siguiente peldano de la escalera. Con `ceil` el cociente queda
 * en (peldano anterior, bucketS], que snapea justo a `bucketS`; con `round`
 * podria pasarse por unas decimas y saltar al peldano siguiente.
 */
export function pointsForBucket(spanS, bucketS) {
  const points = Math.ceil(spanS / bucketS);
  if (points < MIN_SERVER_POINTS || points > MAX_SERVER_POINTS) return null;
  return points;
}

/**
 * Que le falta a cada tag, agrupado por peticion.
 *
 * Los tags que ya estaban piden solo el trozo nuevo y el recien activado pide la
 * ventana entera, asi que en la practica salen dos grupos como mucho.
 */
export function missingGroups(entry, tagIds, from, to) {
  const groups = new Map();
  for (const tagId of tagIds) {
    const series = entry.tags.get(tagId);
    const missing = series ? subtract(from, to, series.covered) : [{ from, to }];
    if (!missing.length) continue;
    const key = missing.map((iv) => `${iv.from}:${iv.to}`).join("|");
    const group = groups.get(key);
    if (group) group.tags.push(tagId);
    else groups.set(key, { tags: [tagId], ranges: missing });
  }
  return [...groups.values()];
}

// --- entradas ---------------------------------------------------------

function newEntry(payload) {
  return {
    bucketS: payload.bucket_s,
    resolution: payload.resolution,
    layer: payload.layer,
    aggregated: payload.aggregated,
    // Cobertura de la entrada = union de todo lo pedido. Los gaps se piden con
    // cada consulta, asi que este rango es el que dice si los huecos estan al
    // dia; la cobertura de datos vive en cada serie.
    covered: [],
    tags: new Map(),
    gaps: [],
  };
}

function newSeries() {
  return { ts: [], avg: [], min: [], max: [], covered: [] };
}

function absorb(entry, payload, requestedTags) {
  entry.resolution = payload.resolution;
  entry.layer = payload.layer;
  entry.aggregated = payload.aggregated;

  const from = Date.parse(payload.from) / 1000;
  const to = Date.parse(payload.to) / 1000;

  for (const s of payload.series) {
    const target = entry.tags.get(s.tag_id) || newSeries();
    entry.tags.set(s.tag_id, mergeSeries(target, s));
  }

  // Un tag pedido que no trae ni una fila esta igual de cubierto: "aqui no hubo
  // dato" es una respuesta, y sin esto se repediria en cada pan.
  for (const tagId of requestedTags) {
    const series = entry.tags.get(tagId) || newSeries();
    series.covered = mergeIntervals([...series.covered, { from, to }]);
    entry.tags.set(tagId, series);
  }

  entry.covered = mergeIntervals([...entry.covered, { from, to }]);
  entry.gaps = mergeGaps(entry.gaps, payload.gaps);
}

/** Fusiona respetando el orden temporal y sin duplicar timestamps. */
function mergeSeries(target, incoming) {
  const covered = target.covered;
  if (!target.ts.length) {
    return {
      ts: incoming.ts.slice(),
      avg: incoming.avg.slice(),
      min: incoming.min.slice(),
      max: incoming.max.slice(),
      covered,
    };
  }
  if (!incoming.ts.length) return target;

  // Caso dominante del pan: el bloque nuevo va entero antes o despues.
  if (incoming.ts[incoming.ts.length - 1] < target.ts[0]) {
    return {
      ts: incoming.ts.concat(target.ts),
      avg: incoming.avg.concat(target.avg),
      min: incoming.min.concat(target.min),
      max: incoming.max.concat(target.max),
      covered,
    };
  }
  if (incoming.ts[0] > target.ts[target.ts.length - 1]) {
    return {
      ts: target.ts.concat(incoming.ts),
      avg: target.avg.concat(incoming.avg),
      min: target.min.concat(incoming.min),
      max: target.max.concat(incoming.max),
      covered,
    };
  }

  // Solape: se rehace la mezcla ordenada descartando timestamps repetidos.
  const byTs = new Map();
  for (let i = 0; i < target.ts.length; i++) {
    byTs.set(target.ts[i], [target.avg[i], target.min[i], target.max[i]]);
  }
  for (let i = 0; i < incoming.ts.length; i++) {
    byTs.set(incoming.ts[i], [incoming.avg[i], incoming.min[i], incoming.max[i]]);
  }
  const ordered = [...byTs.keys()].sort((a, b) => a - b);
  return {
    ts: ordered,
    avg: ordered.map((t) => byTs.get(t)[0]),
    min: ordered.map((t) => byTs.get(t)[1]),
    max: ordered.map((t) => byTs.get(t)[2]),
    covered,
  };
}

function prune(entry, from, to) {
  entry.covered = clampIntervals(entry.covered, from, to);

  for (const [tagId, s] of entry.tags) {
    const covered = clampIntervals(s.covered, from, to);
    let start = 0;
    while (start < s.ts.length && s.ts[start] < from) start++;
    let end = s.ts.length;
    while (end > start && s.ts[end - 1] > to) end--;
    if (start === 0 && end === s.ts.length) {
      s.covered = covered;
      continue;
    }
    entry.tags.set(tagId, {
      ts: s.ts.slice(start, end),
      avg: s.avg.slice(start, end),
      min: s.min.slice(start, end),
      max: s.max.slice(start, end),
      covered,
    });
  }
  entry.gaps = entry.gaps.filter((g) => g[1] > from && g[0] < to);
}

function describe(entry) {
  return {
    bucketS: entry.bucketS,
    resolution: entry.resolution,
    layer: entry.layer,
    aggregated: entry.aggregated,
    series: entry.tags,
    gaps: entry.gaps,
  };
}

// --- intervalos -------------------------------------------------------

export function mergeIntervals(intervals) {
  const sorted = intervals.slice().sort((a, b) => a.from - b.from);
  const out = [];
  for (const iv of sorted) {
    const last = out[out.length - 1];
    if (last && iv.from <= last.to) {
      last.to = Math.max(last.to, iv.to);
    } else {
      out.push({ from: iv.from, to: iv.to });
    }
  }
  return out;
}

/** [from,to] menos lo ya cubierto: los tramos que hay que pedir. */
export function subtract(from, to, covered) {
  const missing = [];
  let cursor = from;
  for (const iv of covered) {
    if (iv.to <= cursor) continue;
    if (iv.from >= to) break;
    if (iv.from > cursor) missing.push({ from: cursor, to: Math.min(iv.from, to) });
    cursor = Math.max(cursor, iv.to);
    if (cursor >= to) break;
  }
  if (cursor < to) missing.push({ from: cursor, to });
  return missing;
}

/**
 * Primer indice con `values[i] >= target`, sobre un array ordenado.
 *
 * Lo usa la vista para acotar las estadisticas a la ventana visible sin
 * recorrer las hasta 3 ventanas de contexto que guarda cada entrada.
 */
export function lowerBound(values, target) {
  let lo = 0;
  let hi = values.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (values[mid] < target) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}

export function covers(covered, from, to) {
  return subtract(from, to, covered).length === 0;
}

function clampIntervals(intervals, from, to) {
  return intervals
    .map((iv) => ({ from: Math.max(iv.from, from), to: Math.min(iv.to, to) }))
    .filter((iv) => iv.to > iv.from);
}

function extendLast(intervals, to) {
  if (intervals.length) intervals[intervals.length - 1].to = to;
}

function mergeGaps(existing, incoming) {
  const all = [...existing, ...incoming].map((g) => ({ from: g[0], to: g[1] }));
  return mergeIntervals(all).map((iv) => [iv.from, iv.to]);
}
