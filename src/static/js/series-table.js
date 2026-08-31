/**
 * Tabla de series bajo el grafico.
 *
 * Hace tres trabajos a la vez, y por eso sustituye al tooltip flotante:
 *  1. Configurar cada serie (lo que se persiste en BD).
 *  2. Ser la leyenda del grafico.
 *  3. Ser el lector del crosshair y de las estadisticas de la ventana visible.
 *
 * Las estadisticas se calculan en cliente sobre lo que ya esta cargado: coste
 * cero y se actualizan solas al hacer pan o zoom.
 */

import { fmtNumber } from "./format.js";

const PALETTE = [
  "#3987e5", "#008300", "#d55181", "#c98500",
  "#199e70", "#d95926", "#9085e9", "#e66767",
];

export class SeriesTable {
  /**
   * @param {HTMLElement} host
   * @param {{onChange:Function, onRemove:Function, onAdd:Function}} handlers
   */
  constructor(host, handlers) {
    this.host = host;
    this.handlers = handlers;
    this.rows = [];
    this.availableTags = [];
    this.stats = new Map();
    this.cursor = new Map();
    this.cells = new Map();
  }

  static nextColor(used) {
    return PALETTE.find((c) => !used.includes(c)) || PALETTE[used.length % PALETTE.length];
  }

  setTags(tags) {
    this.availableTags = tags;
  }

  setRows(rows) {
    this.rows = rows;
    this.render();
  }

  // --- lecturas en vivo ----------------------------------------------

  /** Estadisticas de la ventana visible, por serie. */
  setStats(stats) {
    this.stats = stats;
    this.updateNumbers();
  }

  /** Valores bajo el crosshair. `null` limpia la columna. */
  setCursor(values) {
    this.cursor = values || new Map();
    this.updateNumbers();
  }

  updateNumbers() {
    for (const row of this.rows) {
      const cells = this.cells.get(row.tagId);
      if (!cells) continue;
      const stat = this.stats.get(row.tagId) || {};
      const decimals = row.decimals ?? row.tagDecimals ?? 2;
      const cell = (name, value) => {
        if (cells[name]) cells[name].textContent = fmtNumber(value, decimals);
      };
      cell("cursor", this.cursor.get(row.tagId));
      cell("last", stat.last);
      cell("min", stat.min);
      cell("max", stat.max);
      cell("avg", stat.avg);
    }
  }

  /**
   * Indice de las celdas numericas por tag, rehecho en cada render.
   *
   * `updateNumbers` corre con cada movimiento del crosshair y en cada frame de
   * un arrastre; buscarlas con querySelector cada vez son cinco recorridos del
   * DOM por fila y por actualizacion.
   */
  indexCells() {
    this.cells = new Map();
    for (const el of this.host.querySelectorAll("[data-cell]")) {
      const [name, id] = el.dataset.cell.split("-");
      const tagId = Number(id);
      const group = this.cells.get(tagId) || {};
      group[name] = el;
      this.cells.set(tagId, group);
    }
  }

  // --- render ----------------------------------------------------------

  render() {
    const used = this.rows.map((r) => r.tagId);
    const options = this.availableTags
      .filter((t) => !used.includes(t.id))
      .map((t) => `<option value="${t.id}">${escapeHtml(t.label || t.name)}</option>`)
      .join("");

    this.host.innerHTML = `
      <table class="series-table">
        <thead>
          <tr>
            <th title="Mostrar u ocultar"></th>
            <th>Tag</th>
            <th title="Color de la serie"></th>
            <th title="Series con el mismo grupo comparten escala Y">Eje</th>
            <th>Escala</th>
            <th class="num">Y mín</th>
            <th class="num">Y máx</th>
            <th>Unid</th>
            <th class="num" title="Decimales">Dec</th>
            <th class="num cursor-col">Cursor</th>
            <th class="num">Último</th>
            <th class="num">Mín</th>
            <th class="num">Máx</th>
            <th class="num">Prom</th>
            <th></th>
            <th></th>
          </tr>
        </thead>
        <tbody>${this.rows.map((row) => this.rowHtml(row)).join("")}</tbody>
      </table>
      <div class="series-add">
        <select id="add-tag" ${options ? "" : "disabled"}>
          <option value="">${options ? "Elegir tag…" : "No hay más tags disponibles"}</option>
          ${options}
        </select>
        <button id="add-btn" ${options ? "" : "disabled"}>+ Agregar</button>
        <span class="hint">
          Arrastra el gráfico para desplazarlo · rueda para zoom · Shift+arrastre para seleccionar
        </span>
      </div>`;

    this.bind();
    this.indexCells();
    this.updateNumbers();
  }

  rowHtml(row) {
    const manual = row.scaleMode === "manual";
    const isDigital = row.tagKind === "digital";
    return `
      <tr data-row="${row.tagId}" class="${row.visible ? "" : "row-hidden"}">
        <td>
          <input type="checkbox" data-field="visible" ${row.visible ? "checked" : ""}
                 title="${row.visible ? "Ocultar" : "Mostrar"} serie" />
        </td>
        <td class="series-name">
          <span class="swatch" style="background:${row.color}"></span>
          <span title="${escapeHtml(row.tagName)}">${escapeHtml(row.label)}</span>
          ${isDigital ? '<span class="chip idle">digital</span>' : ""}
        </td>
        <td><input type="color" data-field="color" value="${row.color}" /></td>
        <td>
          <input type="text" data-field="axisGroup" value="${escapeHtml(row.axisGroup)}"
                 class="tiny" title="Series con el mismo texto comparten eje Y" />
        </td>
        <td>
          <select data-field="scaleMode" ${isDigital ? "disabled" : ""}>
            <option value="auto" ${manual ? "" : "selected"}>auto</option>
            <option value="manual" ${manual ? "selected" : ""}>manual</option>
          </select>
        </td>
        <td class="num">
          <input type="number" data-field="yMin" class="tiny num" step="any"
                 value="${row.yMin ?? ""}" ${manual ? "" : "disabled"} />
        </td>
        <td class="num">
          <input type="number" data-field="yMax" class="tiny num" step="any"
                 value="${row.yMax ?? ""}" ${manual ? "" : "disabled"} />
        </td>
        <td><input type="text" data-field="unit" class="tiny" value="${escapeHtml(row.unit || "")}" /></td>
        <td class="num">
          <input type="number" data-field="decimals" class="tiny num" min="0" max="6"
                 value="${row.decimals ?? row.tagDecimals ?? 2}" />
        </td>
        <td class="num cursor-col" data-cell="cursor-${row.tagId}">—</td>
        <td class="num" data-cell="last-${row.tagId}">—</td>
        <td class="num" data-cell="min-${row.tagId}">—</td>
        <td class="num" data-cell="max-${row.tagId}">—</td>
        <td class="num" data-cell="avg-${row.tagId}">—</td>
        <td><button class="ghost" data-more title="Más opciones">⚙</button></td>
        <td><button class="ghost" data-remove title="Quitar de la galería">✕</button></td>
      </tr>
      <tr class="advanced-row" data-advanced="${row.tagId}" hidden>
        <td colspan="16">
          <div class="advanced">
            <label>Interpolación
              <select data-field="interp">
                <option value="auto" ${row.interp === "auto" ? "selected" : ""}>auto</option>
                <option value="linear" ${row.interp === "linear" ? "selected" : ""}>línea</option>
                <option value="step" ${row.interp === "step" ? "selected" : ""}>escalón</option>
              </select>
            </label>
            <label>Grosor
              <select data-field="lineWidth">
                ${[1, 2, 3, 4]
                  .map((w) => `<option value="${w}" ${row.lineWidth === w ? "selected" : ""}>${w} px</option>`)
                  .join("")}
              </select>
            </label>
            <label title="Qué valor representa cada bucket cuando la ventana es larga">
              Agregación
              <select data-field="agg">
                ${[
                  ["avg", "promedio"],
                  ["min", "mínimo"],
                  ["max", "máximo"],
                  ["last", "último"],
                ]
                  .map(([v, t]) => `<option value="${v}" ${row.agg === v ? "selected" : ""}>${t}</option>`)
                  .join("")}
              </select>
            </label>
            <span class="hint mono">${escapeHtml(row.tagName)}</span>
          </div>
        </td>
      </tr>`;
  }

  bind() {
    this.host.querySelectorAll("tr[data-row]").forEach((tr) => {
      const tagId = Number(tr.dataset.row);
      tr.querySelectorAll("[data-field]").forEach((input) => {
        const event = input.type === "color" || input.tagName === "SELECT" || input.type === "checkbox"
          ? "change"
          : "input";
        input.addEventListener(event, () => this.onField(tagId, input));
      });
      tr.querySelector("[data-remove]").addEventListener("click", () =>
        this.handlers.onRemove(tagId),
      );
      tr.querySelector("[data-more]").addEventListener("click", () => {
        const panel = this.host.querySelector(`[data-advanced="${tagId}"]`);
        panel.hidden = !panel.hidden;
      });
    });

    // Los controles avanzados viven en su propia fila, fuera del bucle anterior.
    this.host.querySelectorAll("tr[data-advanced]").forEach((tr) => {
      const tagId = Number(tr.dataset.advanced);
      tr.querySelectorAll("[data-field]").forEach((input) =>
        input.addEventListener("change", () => this.onField(tagId, input)),
      );
    });

    const select = this.host.querySelector("#add-tag");
    this.host.querySelector("#add-btn")?.addEventListener("click", () => {
      const id = Number(select.value);
      if (id) this.handlers.onAdd(id);
    });
  }

  onField(tagId, input) {
    const row = this.rows.find((r) => r.tagId === tagId);
    if (!row) return;
    const field = input.dataset.field;

    if (input.type === "checkbox") {
      row[field] = input.checked;
    } else if (input.type === "number") {
      row[field] = input.value === "" ? null : Number(input.value);
    } else {
      row[field] = input.value;
    }

    // La escala manual sin limites no significa nada: se rellenan con lo visible.
    if (field === "scaleMode") {
      const stat = this.stats.get(tagId);
      if (input.value === "manual" && row.yMin === null && stat) {
        row.yMin = round(stat.min);
        row.yMax = round(stat.max);
      }
      this.render();
    }
    if (field === "visible" || field === "color") this.render();

    this.handlers.onChange(row, field);
  }
}

function round(value) {
  if (value === null || value === undefined) return null;
  const magnitude = Math.abs(value);
  const decimals = magnitude >= 100 ? 0 : magnitude >= 10 ? 1 : 2;
  return Number(value.toFixed(decimals));
}

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text ?? "";
  return div.innerHTML;
}
