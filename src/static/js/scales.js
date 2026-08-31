// Identidad y limites de las escalas Y del grafico.
//
// Una escala es un marco de referencia: todo lo que se dibuja dentro comparte
// limites. Dos series solo pueden compartirlo si estan de acuerdo en cuales
// son, y por eso el rango manual forma parte de la clave. Si no lo fuese, dos
// series de la misma unidad con limites distintos caerian en la misma escala,
// una ganaria el desempate y la otra quedaria dibujada fuera del area de trazo:
// desaparece del grafico sin ningun aviso, que es el peor fallo posible en un
// historiador.
//
// Modulo sin DOM a proposito, como cache.js y viewport.js: asi la regla se
// puede probar sin navegador.

/** Numero utilizable, o null. Cubre null, undefined, "" y NaN. */
function num(value) {
  if (value === null || value === undefined || value === "") return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

/** Limites manuales de una serie, o null si le toca autoescalar.
 *
 * Un rango incompleto o invertido cuenta como automatico: mas vale una escala
 * util que una degenerada en la que no cabe ningun punto. El servidor ya valida
 * y_max > y_min (api/galleries.py), pero el grafico tambien dibuja filas que se
 * estan editando en la tabla y que aun no han pasado por ahi.
 */
export function manualRange(row) {
  if (row.scaleMode !== "manual") return null;
  const min = num(row.yMin);
  const max = num(row.yMax);
  if (min === null || max === null || min >= max) return null;
  return [min, max];
}

/** Clave de la escala a la que pertenece una serie.
 *
 * - Un axis_group explicito es una orden de compartir marco y se respeta tal
 *   cual, aunque los limites no coincidan; ese desempate lo resuelve
 *   buildScaleConfigs.
 * - 'auto' agrupa por unidad, que es lo que evita que °C y bar compartan
 *   escala, y separa ademas por rango manual.
 * - Sin unidad se cae al id del tag, con prefijo propio para que un tag sin
 *   unidad no pueda colisionar con una unidad que se llame como su id.
 */
export function scaleKeyFor(row) {
  if (row.axisGroup && row.axisGroup !== "auto") return `g:${row.axisGroup}`;
  const base = row.unit ? `u:${row.unit}` : `t:${row.tagId}`;
  const range = manualRange(row);
  return range ? `${base}:m:${range[0]}:${range[1]}` : base;
}

/** Config de escalas de uPlot, una por clave, a partir de las series visibles.
 *
 * Quien llama debe haber apartado ya las digitales, que viven en su propio
 * carril. El invariante que sostiene esta funcion: ninguna combinacion de
 * configuraciones puede dejar una serie fuera del area de trazo.
 */
export function buildScaleConfigs(series) {
  const groups = new Map();
  for (const s of series) {
    let group = groups.get(s.scaleKey);
    if (!group) {
      group = { anyAuto: false, min: null, max: null };
      groups.set(s.scaleKey, group);
    }
    const range = manualRange(s);
    if (range === null) {
      group.anyAuto = true;
      continue;
    }
    // Varios rangos manuales en la misma clave solo ocurre con un axis_group
    // explicito: se unen en vez de que gane el primero y el resto se pierda.
    group.min = group.min === null ? range[0] : Math.min(group.min, range[0]);
    group.max = group.max === null ? range[1] : Math.max(group.max, range[1]);
  }

  const scales = {};
  for (const [key, group] of groups) {
    if (group.min === null) {
      scales[key] = { auto: true };
    } else if (!group.anyAuto) {
      scales[key] = { auto: false, range: [group.min, group.max] };
    } else {
      // Manual y automatica compartiendo eje. Los limites manuales valen como
      // minimo garantizado y el marco se ensancha si los datos de las
      // automaticas se salen. uPlot pasa null en los extremos cuando la ventana
      // no tiene ningun dato.
      scales[key] = {
        auto: true,
        range: (_u, dataMin, dataMax) => [
          dataMin === null || dataMin === undefined ? group.min : Math.min(group.min, dataMin),
          dataMax === null || dataMax === undefined ? group.max : Math.max(group.max, dataMax),
        ],
      };
    }
  }
  return scales;
}
