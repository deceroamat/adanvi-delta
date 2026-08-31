// Tests de la logica de cliente que no toca el DOM.
//   ~/.deno/bin/deno test tests/frontend.test.js

import { assertEquals, assert } from "jsr:@std/assert@1";
import {
  covers,
  HistoryCache,
  lowerBound,
  mergeIntervals,
  pointsForBucket,
  subtract,
} from "../src/static/js/cache.js";
import { buildScaleConfigs, manualRange, scaleKeyFor } from "../src/static/js/scales.js";
import { Viewport } from "../src/static/js/viewport.js";

// --- intervalos -------------------------------------------------------

Deno.test("subtract: sin nada cacheado pide la ventana entera", () => {
  assertEquals(subtract(0, 100, []), [{ from: 0, to: 100 }]);
});

Deno.test("subtract: ventana ya cubierta no pide nada", () => {
  assertEquals(subtract(10, 90, [{ from: 0, to: 100 }]), []);
});

Deno.test("subtract: pan a la izquierda pide solo el trozo nuevo", () => {
  // Se tenia [100,200] y ahora se mira [50,150].
  assertEquals(subtract(50, 150, [{ from: 100, to: 200 }]), [{ from: 50, to: 100 }]);
});

Deno.test("subtract: pan a la derecha pide solo el trozo nuevo", () => {
  assertEquals(subtract(150, 250, [{ from: 100, to: 200 }]), [{ from: 200, to: 250 }]);
});

Deno.test("subtract: hueco en medio produce dos peticiones", () => {
  const missing = subtract(0, 100, [{ from: 20, to: 40 }, { from: 60, to: 80 }]);
  assertEquals(missing, [
    { from: 0, to: 20 },
    { from: 40, to: 60 },
    { from: 80, to: 100 },
  ]);
});

Deno.test("mergeIntervals fusiona tramos contiguos y solapados", () => {
  assertEquals(
    mergeIntervals([
      { from: 0, to: 10 },
      { from: 10, to: 20 },
      { from: 15, to: 30 },
      { from: 50, to: 60 },
    ]),
    [{ from: 0, to: 30 }, { from: 50, to: 60 }],
  );
});

Deno.test("covers", () => {
  assert(covers([{ from: 0, to: 100 }], 10, 90));
  assert(!covers([{ from: 0, to: 100 }], 10, 110));
});

// --- viewport ---------------------------------------------------------

Deno.test("cualquier interaccion apaga el seguimiento del reloj", () => {
  for (const action of [
    (vp) => vp.pan(-0.5),
    (vp) => vp.zoom(1.25, 0.5),
    (vp) => vp.setRange(1000, 2000),
  ]) {
    const vp = new Viewport({ span: 3600, follow: true });
    assert(vp.follow, "arranca siguiendo");
    action(vp);
    assert(!vp.follow, "deja de seguir tras interactuar");
  }
});

Deno.test("no hay modo pausa: LIVE es la unica vuelta atras", () => {
  const vp = new Viewport({ span: 3600, follow: true });
  vp.pan(-1);
  assert(!vp.follow);
  vp.goLive();
  assert(vp.follow);
  assert(Math.abs(vp.to - Date.now() / 1000) < 2, "se reancla al presente");
});

Deno.test("el pan conserva el ancho de ventana", () => {
  const vp = new Viewport({ span: 3600, follow: false, to: 10_000 });
  vp.pan(-0.5);
  assertEquals(vp.span, 3600);
  assertEquals(vp.to, 10_000 - 1800);
  assertEquals(vp.from, 10_000 - 1800 - 3600);
});

Deno.test("el zoom mantiene fijo el instante bajo el cursor", () => {
  const vp = new Viewport({ span: 1000, follow: false, to: 10_000 });
  const anchor = vp.from + vp.span * 0.25;
  vp.zoom(0.5, 0.25);
  assertEquals(vp.span, 500);
  assert(Math.abs(vp.from + vp.span * 0.25 - anchor) < 0.001);
});

Deno.test("el tick solo mueve la ventana si se esta siguiendo", () => {
  const following = new Viewport({ span: 3600, follow: true, to: 1000 });
  assert(following.tick());
  assert(following.to > 1000);

  const paused = new Viewport({ span: 3600, follow: false, to: 1000 });
  assert(!paused.tick());
  assertEquals(paused.to, 1000);
});

Deno.test("restablecer zoom vuelve al ancho nombrado", () => {
  const vp = new Viewport({ span: 3600, follow: false, to: 10_000 });
  vp.setSpan(86_400, "1d");
  vp.zoom(0.1, 0.5);
  assert(vp.span < 86_400);
  vp.resetZoom();
  assertEquals(vp.span, 86_400);
  assertEquals(vp.windowText, "1d");
});

Deno.test("el span se acota a limites razonables", () => {
  const vp = new Viewport({ span: 3600, follow: false, to: 10_000 });
  for (let i = 0; i < 60; i++) vp.zoom(0.5, 0.5);
  assert(vp.span >= 10, "no baja de 10 s");
  for (let i = 0; i < 100; i++) vp.zoom(2, 0.5);
  assert(vp.span <= 5 * 365 * 86_400, "no pasa de 5 anos");
});

Deno.test("emite un evento change por cada transicion", () => {
  const vp = new Viewport({ span: 3600, follow: true });
  const reasons = [];
  vp.addEventListener("change", (e) => reasons.push(e.detail.reason));
  vp.pan(-0.5);
  vp.zoom(2, 0.5);
  vp.goLive();
  vp.setSpan(600, "10m");
  assertEquals(reasons, ["pan", "zoom", "live", "window"]);
});

// --- escalas ----------------------------------------------------------

/** Serie con lo minimo que mira la logica de escalas. */
function serie(tagId, unit, extra = {}) {
  return { tagId, unit, axisGroup: "auto", scaleMode: "auto", yMin: null, yMax: null, ...extra };
}

const manual = (min, max) => ({ scaleMode: "manual", yMin: min, yMax: max });

Deno.test("misma unidad y rangos manuales distintos no comparten escala", () => {
  // El caso que borraba una serie del grafico: con una sola escala '%', la que
  // perdia el desempate se dibujaba fuera del area de trazo.
  const consistencia = serie(22, "%", manual(0, 10));
  const nivel = serie(23, "%", manual(0, 100));
  assert(scaleKeyFor(consistencia) !== scaleKeyFor(nivel));

  const scales = buildScaleConfigs([consistencia, nivel].map(withKey));
  assertEquals(Object.keys(scales).length, 2);
  for (const cfg of Object.values(scales)) assertEquals(cfg.auto, false);
  const ranges = Object.values(scales).map((c) => c.range);
  assert(ranges.some((r) => r[1] === 10) && ranges.some((r) => r[1] === 100));
});

Deno.test("misma unidad y el mismo rango manual siguen compartiendo escala", () => {
  // Separarlas gastaria un eje de mas sin ganar nada: son comparables.
  const a = serie(1, "bar", manual(0, 16));
  const b = serie(2, "bar", manual(0, 16));
  assertEquals(scaleKeyFor(a), scaleKeyFor(b));
  assertEquals(Object.keys(buildScaleConfigs([a, b].map(withKey))).length, 1);
});

Deno.test("misma unidad en automatico comparte escala", () => {
  const a = serie(1, "degC");
  const b = serie(2, "degC");
  assertEquals(scaleKeyFor(a), scaleKeyFor(b));
  assertEquals(buildScaleConfigs([a, b].map(withKey))[scaleKeyFor(a)], { auto: true });
});

Deno.test("una manual y una automatica de la misma unidad se separan", () => {
  // Compartir dejaria a la automatica encajada en un rango ajeno, o al reves.
  const fija = serie(1, "%", manual(0, 100));
  const libre = serie(2, "%");
  assert(scaleKeyFor(fija) !== scaleKeyFor(libre));
  const scales = buildScaleConfigs([fija, libre].map(withKey));
  assertEquals(scales[scaleKeyFor(fija)], { auto: false, range: [0, 100] });
  assertEquals(scales[scaleKeyFor(libre)], { auto: true });
});

Deno.test("unidades distintas nunca comparten escala", () => {
  const keys = [serie(1, "degC"), serie(2, "bar"), serie(3, "rpm")].map(scaleKeyFor);
  assertEquals(new Set(keys).size, 3);
});

Deno.test("sin unidad cada serie cae en su propia escala", () => {
  assert(scaleKeyFor(serie(1, "")) !== scaleKeyFor(serie(2, "")));
  // Un tag sin unidad no puede colisionar con una unidad que se llame como su id.
  assert(scaleKeyFor(serie(7, "")) !== scaleKeyFor(serie(9, "7")));
});

Deno.test("un axis_group explicito manda sobre la unidad", () => {
  const a = serie(1, "degC", { axisGroup: "proceso" });
  const b = serie(2, "bar", { axisGroup: "proceso", ...manual(0, 16) });
  assertEquals(scaleKeyFor(a), scaleKeyFor(b));
});

Deno.test("en un eje compartido los rangos manuales se unen, no se descartan", () => {
  const a = serie(1, "bar", { axisGroup: "proceso", ...manual(0, 16) });
  const b = serie(2, "bar", { axisGroup: "proceso", ...manual(-5, 4) });
  const scales = buildScaleConfigs([a, b].map(withKey));
  assertEquals(scales["g:proceso"], { auto: false, range: [-5, 16] });
});

Deno.test("en un eje compartido mixto los limites manuales son un minimo", () => {
  // El marco se ensancha si los datos de la automatica se salen: asi ninguna
  // serie puede quedar fuera del area de trazo.
  const fija = serie(1, "bar", { axisGroup: "proceso", ...manual(0, 10) });
  const libre = serie(2, "bar", { axisGroup: "proceso" });
  const cfg = buildScaleConfigs([fija, libre].map(withKey))["g:proceso"];
  assertEquals(cfg.auto, true);
  assertEquals(cfg.range(null, 3, 8), [0, 10], "los datos que caben no encogen el marco");
  assertEquals(cfg.range(null, -2, 45), [-2, 45], "los que no caben lo ensanchan");
  assertEquals(cfg.range(null, null, null), [0, 10], "ventana sin datos");
});

Deno.test("un rango manual invalido se trata como automatico", () => {
  // Mejor autoescalar que dejar una escala en la que no cabe ningun punto.
  assertEquals(manualRange(serie(1, "%", manual(50, 50))), null, "vacio");
  assertEquals(manualRange(serie(1, "%", manual(100, 0))), null, "invertido");
  assertEquals(manualRange(serie(1, "%", manual(0, null))), null, "sin techo");
  assertEquals(manualRange(serie(1, "%", manual("", 10))), null, "campo en blanco");
  assertEquals(manualRange(serie(1, "%", { scaleMode: "auto", yMin: 0, yMax: 10 })), null);
  // Los limites llegan como texto desde los inputs de la tabla.
  assertEquals(manualRange(serie(1, "%", manual("0", "10"))), [0, 10]);
});

Deno.test("cada galeria construye sus escalas por separado", () => {
  // No hay estado global: las mismas unidades en otra galeria no se contaminan.
  const galeria1 = [serie(1, "%", manual(0, 10))].map(withKey);
  const galeria2 = [serie(9, "%", manual(0, 250))].map(withKey);
  const s1 = buildScaleConfigs(galeria1);
  const s2 = buildScaleConfigs(galeria2);
  assertEquals(Object.values(s1)[0].range, [0, 10]);
  assertEquals(Object.values(s2)[0].range, [0, 250]);
});

function withKey(row) {
  return { ...row, scaleKey: scaleKeyFor(row) };
}

// --- cache: cobertura por tag y resolucion estable --------------------
//
// Los dos fallos que obligaban a recargar la pagina: activar una serie sobre una
// ventana ya cubierta no pedia nada, y cada pan pedia un trozo mas corto que la
// ventana, con lo que el servidor devolvia otra resolucion y el tramo no
// encajaba con lo cacheado.

// Escalera y formula del servidor (timeparse.py), para simularlo sin red.
const LADDER = [
  1, 2, 5, 10, 15, 30,
  60, 120, 300, 600, 900, 1800,
  3600, 7200, 10800, 21600, 43200,
  86400, 172800, 604800,
];

function snapBucket(seconds) {
  return LADDER.find((step) => step >= seconds) ?? LADDER[LADDER.length - 1];
}

function fakeHistory(url) {
  const params = new URL(url, "http://servidor").searchParams;
  const tags = params.get("tags").split(",").map(Number);
  const from = Date.parse(params.get("from")) / 1000;
  const to = Date.parse(params.get("to")) / 1000;
  const maxPoints = Math.max(200, Math.min(5000, Number(params.get("max_points"))));
  const bucket = snapBucket(Math.max((to - from) / maxPoints, 1));

  const start = Math.floor(from / bucket) * bucket;
  const end = Math.ceil(to / bucket) * bucket;
  const ts = [];
  for (let t = start; t < end; t += bucket) ts.push(t);

  return {
    from: new Date(start * 1000).toISOString(),
    to: new Date(end * 1000).toISOString(),
    layer: "raw",
    bucket_s: bucket,
    resolution: `${bucket}s`,
    aggregated: bucket > 1,
    series: tags.map((id) => ({
      tag_id: id,
      ts: ts.slice(),
      avg: ts.map(() => id),
      min: ts.map(() => id),
      max: ts.map(() => id),
    })),
    gaps: [],
  };
}

/** Comprueba que la ventana visible esta entera, sin huecos ni bordes vacios. */
function assertVentanaLlena(result, from, to, bucket) {
  const visible = result.series.get(1).ts.filter((t) => t >= from && t <= to);
  assert(visible.length >= (to - from) / bucket, `faltan puntos: ${visible.length}`);
  assert(visible[0] <= from + bucket, "el borde izquierdo se quedo vacio");
  assert(visible[visible.length - 1] >= to - bucket, "el borde derecho se quedo vacio");
  for (let i = 1; i < visible.length; i++) {
    assertEquals(visible[i] - visible[i - 1], bucket, "hueco en mitad de la ventana");
  }
}

/** Instala un servidor de mentira y devuelve el registro de peticiones. */
function stubServer() {
  const requests = [];
  globalThis.fetch = (path) => {
    requests.push(path);
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(fakeHistory(path)) });
  };
  return requests;
}

Deno.test("max_points fijado reproduce el bucket que se pidio", () => {
  for (const bucket of [1, 2, 5, 30, 60, 600, 3600]) {
    for (const factor of [200, 237, 500, 1201, 2999]) {
      const span = bucket * factor + 7; // +7 para que no cuadre redondo
      const points = pointsForBucket(span, bucket);
      if (points === null) continue;
      assertEquals(snapBucket(Math.max(span / points, 1)), bucket);
    }
  }
});

Deno.test("activar un tag sobre una ventana ya cubierta lo pide y lo trae", async () => {
  const requests = stubServer();
  const cache = new HistoryCache();
  const to = 1_700_000_000;
  const from = to - 3600;

  await cache.load({ tagIds: [1, 2], from, to, maxPoints: 1200 });
  assertEquals(requests.length, 1);

  const result = await cache.load({ tagIds: [1, 2, 3], from, to, maxPoints: 1200 });
  assertEquals(requests.length, 2); // una sola peticion, y solo para el tag nuevo
  assert(requests[1].includes("tags=3"));
  assert(result.series.get(3).ts.length > 0);
  assertEquals(result.series.get(1).ts.length, result.series.get(3).ts.length);
});

Deno.test("quitar un tag no vuelve a pedir nada", async () => {
  const requests = stubServer();
  const cache = new HistoryCache();
  const to = 1_700_000_000;
  const from = to - 3600;

  await cache.load({ tagIds: [1, 2], from, to, maxPoints: 1200 });
  const result = await cache.load({ tagIds: [1], from, to, maxPoints: 1200 });
  assertEquals(requests.length, 1);
  assertEquals(result.fromCache, true);
});

Deno.test("el pan conserva la resolucion y deja la ventana entera cubierta", async () => {
  stubServer();
  const cache = new HistoryCache();
  const span = 3600;
  let to = 1_700_000_000;
  let from = to - span;

  const first = await cache.load({ tagIds: [1], from, to, maxPoints: 1200 });
  assertEquals(first.bucketS, 5);

  // Tres pulsaciones de '‹' y una de '›', que es la secuencia que dejaba el
  // grafico con dos puntos.
  for (const step of [-0.5, -0.5, -0.5, 0.5]) {
    from += span * step;
    to += span * step;
    const result = await cache.load({ tagIds: [1], from, to, maxPoints: 1200 });
    assertEquals(result.bucketS, 5);
    assertVentanaLlena(result, from, to, 5);
  }
});

Deno.test("un arrastre corto ensancha la peticion en vez de cambiar de bucket", async () => {
  stubServer();
  const cache = new HistoryCache();
  const span = 3600;
  let to = 1_700_000_000;
  let from = to - span;

  await cache.load({ tagIds: [1], from, to, maxPoints: 1200 });
  // 2% de la ventana: 72 s, que a bucket 5 s son 15 puntos, por debajo del
  // minimo que acepta el servidor.
  from += span * 0.02;
  to += span * 0.02;
  const result = await cache.load({ tagIds: [1], from, to, maxPoints: 1200 });
  assertEquals(result.bucketS, 5);
  assertVentanaLlena(result, from, to, 5);
});

Deno.test("lowerBound encuentra el primer indice dentro de la ventana", () => {
  const ts = [10, 20, 30, 40, 50];
  assertEquals(lowerBound(ts, 5), 0); // antes del principio
  assertEquals(lowerBound(ts, 10), 0); // justo en el primero
  assertEquals(lowerBound(ts, 31), 3); // entre dos puntos
  assertEquals(lowerBound(ts, 50), 4); // justo en el ultimo
  assertEquals(lowerBound(ts, 99), 5); // pasado el final
  assertEquals(lowerBound([], 1), 0); // serie vacia
});

// --- pan discreto vs arrastre -----------------------------------------
//
// Un arrastre emite ~60 pan/s y conviene esperar a que pare antes de pedir
// datos; un clic en '‹' es un evento unico y esperar ahi es tiempo muerto.

Deno.test("pan distingue el arrastre de la pulsacion suelta", () => {
  const vp = new Viewport({ span: 3600, follow: false, to: 10_000 });
  const reasons = [];
  vp.addEventListener("change", (e) => reasons.push(e.detail.reason));

  vp.pan(-0.5); // arrastre: es el valor por defecto
  vp.pan(-0.5, "step"); // boton o flecha del teclado

  assertEquals(reasons, ["pan", "pan-step"]);
});

// --- precarga de las ventanas contiguas -------------------------------
//
// Sin esto, cada '‹' es siempre una ida y vuelta a la red, y hasta que vuelve
// media ventana se queda en blanco.

/** Servidor de mentira que no responde hasta que se le suelta. */
function stubPausedServer() {
  const pending = [];
  globalThis.fetch = (path, init) => {
    let release;
    const promise = new Promise((resolve, reject) => {
      release = () =>
        resolve({ ok: true, status: 200, json: () => Promise.resolve(fakeHistory(path)) });
      init?.signal?.addEventListener("abort", () => {
        const error = new Error("abortada");
        error.name = "AbortError";
        reject(error);
      });
    });
    pending.push({ path, release });
    return promise;
  };
  return pending;
}

Deno.test("peek señala el tramo que aun no ha llegado", async () => {
  stubServer();
  const cache = new HistoryCache();
  const span = 3600;
  const to = 1_700_000_000;
  const from = to - span;

  await cache.load({ tagIds: [1], from, to, maxPoints: 1200 });
  assertEquals(cache.peek(from, to).missing, []);

  // Media ventana hacia atras, todavia sin pedir.
  const view = cache.peek(from - span / 2, to - span / 2);
  assertEquals(view.stale, true);
  assertEquals(view.missing.length, 1);
  assertEquals(view.missing[0].from, from - span / 2);
  assert(view.missing[0].to <= from, "el tramo que falta invade lo ya cubierto");
});

Deno.test("la precarga deja el siguiente '‹' servido desde memoria", async () => {
  const requests = stubServer();
  const cache = new HistoryCache();
  const span = 86_400;
  const to = 1_700_000_000;
  const from = to - span;

  await cache.load({ tagIds: [1], from, to, maxPoints: 1200 });
  const trasCarga = requests.length;

  await cache.prefetch({ tagIds: [1], from: from - span, to: to + span, maxPoints: 1200 });
  assert(requests.length > trasCarga, "la precarga no pidio nada");

  const antesDelPan = requests.length;
  const panFrom = from - span / 2;
  const panTo = to - span / 2;
  const result = await cache.load({ tagIds: [1], from: panFrom, to: panTo, maxPoints: 1200 });

  assertEquals(requests.length, antesDelPan, "el pan repidio datos ya precargados");
  assertEquals(result.fromCache, true);
  assertVentanaLlena(result, panFrom, panTo, result.bucketS);
});

Deno.test("la precarga respeta el bucket de la entrada activa", async () => {
  stubServer();
  const cache = new HistoryCache();
  const span = 86_400;
  const to = 1_700_000_000;
  const from = to - span;

  const first = await cache.load({ tagIds: [1], from, to, maxPoints: 1200 });
  await cache.prefetch({ tagIds: [1], from: from - span, to, maxPoints: 1200 });

  // Mezclar dos resoluciones en el mismo trazo es la invariante 2 de cache.js.
  assertEquals(cache.entries.size, 1);
  assertEquals([...cache.entries.keys()], [first.bucketS]);
});

Deno.test("un pan no cancela la precarga en vuelo", async () => {
  stubServer();
  const cache = new HistoryCache();
  const span = 3600;
  const to = 1_700_000_000;
  const from = to - span;
  await cache.load({ tagIds: [1], from, to, maxPoints: 1200 });

  const pending = stubPausedServer();
  // Solo el lado izquierdo: una peticion y una sola.
  const prefetching = cache.prefetch({ tagIds: [1], from: from - span, to, maxPoints: 1200 });
  assertEquals(pending.length, 1);
  assert(cache.prefetchController !== null);

  // Es lo primero que hace cada interaccion antes de pedir datos.
  cache.abort();
  assert(cache.prefetchController !== null, "el pan aborto la precarga");

  pending[0].release();
  await prefetching;
  assertEquals(cache.prefetchController, null);
});

Deno.test("abortar la precarga no toca la peticion interactiva", async () => {
  stubServer();
  const cache = new HistoryCache();
  const to = 1_700_000_000;
  const from = to - 3600;
  await cache.load({ tagIds: [1], from, to, maxPoints: 1200 });

  const pending = stubPausedServer();
  const loading = cache.load({ tagIds: [1], from: from - 3600, to, maxPoints: 1200 });
  assertEquals(pending.length, 1);

  cache.abortPrefetch();
  assert(cache.controller !== null, "la precarga aborto la peticion del usuario");

  pending[0].release();
  await loading;
});
