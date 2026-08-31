// Formulario de operacion: captura bobina a bobina.
//
// La regla que manda sobre todo lo demas: la fila de columnas esta siempre
// lista y visible, y nunca se abre una ventana. Quien lo usa esta de pie frente
// a una maquina y viene de llenar esto mismo en papel.

import { api, reportError, toast } from "./api.js";
import { mountTopbar } from "./shell.js";

// TEMPORAL: bloquea los enlaces del topbar mientras se captura (ver shell.js).
mountTopbar("forms", { lockNav: true });

const ZONES = 10;
const DRAFT_KEY = "adanvi.opDraft";

// Espejo de los rangos de src/api/forms.py, que es la fuente de verdad. Aqui
// solo sirven para avisar antes de mandar; el servidor valida igual.
const LIMITS = {
  speed: [0, 600],
  gsm: [10, 120],
  reel: [0, 5000],
  breaks: [0, 5],
};

const dateInput = document.getElementById("f-date");
const host = document.getElementById("rows-host");
const emptyBox = document.getElementById("op-empty");
const countLabel = document.getElementById("op-count");
const hintLabel = document.getElementById("op-hint");
const engMode = document.getElementById("eng-mode");
const captureRow = document.getElementById("capture-row");
const refSelect = document.getElementById("c-reference");
const refOther = document.getElementById("c-reference-other");

// Valor de la opcion "Otro...": no es una referencia, es un modo.
const OTHER = "__otro__";

// Orden de recorrido con Enter, en el mismo orden en que se lee la fila.
const FIELD_IDS = [
  "c-reference",
  "c-consecutive",
  "c-start",
  "c-end",
  "c-speed",
  ...Array.from({ length: ZONES }, (_, i) => `c-z${i + 1}`),
  "c-base",
  "c-reel",
  "c-breaks",
  "c-type",
];

// El borrador guarda ademas la referencia tecleada a mano, que no entra en el
// recorrido con Enter porque solo esta visible a ratos.
const DRAFT_IDS = [...FIELD_IDS, "c-reference-other"];

let rows = [];
let editWindowMin = 30;
let editingId = null;
let referenceList = [];
// Referencia del borrador: se aplica cuando llega la lista del API, no antes,
// porque hasta entonces el <select> no tiene opciones donde encajarla.
let pendingRef = null;

// --- Utilidades -------------------------------------------------------

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text ?? "";
  return div.innerHTML;
}

const el = (id) => document.getElementById(id);

/** Acepta coma o punto como separador decimal. Devuelve null si no es numero. */
function parseNum(raw) {
  const text = String(raw ?? "").trim().replace(",", ".");
  if (!text) return null;
  const value = Number(text);
  return Number.isFinite(value) ? value : null;
}

function fmtNum(value) {
  return value === null || value === undefined ? "—" : String(value);
}

function todayISO() {
  const d = new Date();
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

function nowHHMM() {
  const d = new Date();
  const pad = (n) => String(n).padStart(2, "0");
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function markInvalid(input, invalid) {
  if (invalid) input.setAttribute("aria-invalid", "true");
  else input.removeAttribute("aria-invalid");
}

// --- Referencia de produccion -----------------------------------------
// Un <select> con las referencias conocidas y un campo de texto para las que
// no lo son. Solo uno de los dos esta visible: la columna no puede crecer.

function setOtherMode(on) {
  refSelect.hidden = on;
  refOther.hidden = !on;
}

/** La lista no cambia durante la sesion, asi que se llena una sola vez. */
function fillReferences(list) {
  if (refSelect.options.length) return;
  referenceList = list;
  refSelect.innerHTML = [
    // Sin valor por defecto a proposito: si arrancara en K40, una bobina de
    // otra referencia se registraria mal sin que nadie lo notara.
    '<option value="">—</option>',
    ...list.map((ref) => `<option value="${escapeHtml(ref)}">${escapeHtml(ref)}</option>`),
    `<option value="${OTHER}">Otro…</option>`,
  ].join("");
  applyPendingRef();
}

function applyPendingRef() {
  if (pendingRef === null || !refSelect.options.length) return;
  refSelect.value = pendingRef;
  if (refSelect.value === "" && pendingRef !== "") {
    // La del borrador ya no esta en la lista: se trata como tecleada a mano.
    refSelect.value = OTHER;
    refOther.value = pendingRef;
  }
  setOtherMode(refSelect.value === OTHER);
  pendingRef = null;
}

function readReference() {
  return refSelect.value === OTHER ? refOther.value.trim().toUpperCase() : refSelect.value;
}

// --- Lectura y validacion de la fila de captura ------------------------

/**
 * Devuelve `{ payload }` o `{ error, field }`.
 * No bloquea nunca el tecleo ni deshabilita el boton: un boton muerto sin
 * explicacion es lo peor que le puede pasar a este usuario.
 */
function collect() {
  const fail = (message, id) => ({ error: message, field: el(id) });

  const reference = readReference();
  if (!reference) {
    return fail("Falta la referencia", refSelect.hidden ? "c-reference-other" : "c-reference");
  }

  const consecutive = el("c-consecutive").value.trim();
  if (!consecutive) return fail("Falta el consecutivo", "c-consecutive");
  if (rows.some((r) => r.consecutive === consecutive)) {
    return fail(`El consecutivo "${consecutive}" ya está registrado`, "c-consecutive");
  }

  const start = el("c-start").value;
  const end = el("c-end").value;
  if (!start) return fail("Falta la hora de inicio", "c-start");
  if (!end) return fail("Falta la hora de fin", "c-end");

  const speed = parseNum(el("c-speed").value);
  if (speed === null) return fail("Falta la velocidad de máquina", "c-speed");
  if (speed < LIMITS.speed[0] || speed > LIMITS.speed[1]) {
    return fail(`La velocidad debe estar entre ${LIMITS.speed[0]} y ${LIMITS.speed[1]} m/min`, "c-speed");
  }

  const profile = [];
  for (let i = 1; i <= ZONES; i += 1) {
    const id = `c-z${i}`;
    const value = parseNum(el(id).value);
    if (value === null) return fail(`Falta el peso de la zona ${i}`, id);
    if (value < LIMITS.gsm[0] || value > LIMITS.gsm[1]) {
      return fail(`La zona ${i} debe estar entre ${LIMITS.gsm[0]} y ${LIMITS.gsm[1]} g/m²`, id);
    }
    profile.push(value);
  }

  const base = parseNum(el("c-base").value);
  if (base === null) return fail("Falta el peso base promedio", "c-base");
  if (base < LIMITS.gsm[0] || base > LIMITS.gsm[1]) {
    return fail(`El peso base debe estar entre ${LIMITS.gsm[0]} y ${LIMITS.gsm[1]} g/m²`, "c-base");
  }

  const reel = parseNum(el("c-reel").value);
  if (reel === null) return fail("Falta el peso de la bobina", "c-reel");
  if (reel < LIMITS.reel[0] || reel > LIMITS.reel[1]) {
    return fail(`El peso de la bobina debe estar entre ${LIMITS.reel[0]} y ${LIMITS.reel[1]} kg`, "c-reel");
  }

  const breaks = parseNum(el("c-breaks").value);
  if (breaks === null) return fail("Falta el número de rupturas", "c-breaks");
  if (!Number.isInteger(breaks) || breaks < LIMITS.breaks[0] || breaks > LIMITS.breaks[1]) {
    return fail(`Las rupturas son un número entero de ${LIMITS.breaks[0]} a ${LIMITS.breaks[1]}`, "c-breaks");
  }

  return {
    payload: {
      reference,
      consecutive,
      shift_date: dateInput.value,
      start_time: start,
      end_time: end,
      machine_speed: speed,
      weight_profile: profile,
      base_weight: base,
      reel_weight: reel,
      breaks,
      reel_type: el("c-type").value,
    },
  };
}

/** Marca en rojo lo que esta fuera de rango, sin interrumpir. */
function validateLive() {
  const check = (id, [min, max]) => {
    const input = el(id);
    const value = parseNum(input.value);
    markInvalid(input, input.value.trim() !== "" && (value === null || value < min || value > max));
  };
  check("c-speed", LIMITS.speed);
  for (let i = 1; i <= ZONES; i += 1) check(`c-z${i}`, LIMITS.gsm);
  check("c-base", LIMITS.gsm);
  check("c-reel", LIMITS.reel);
  check("c-breaks", LIMITS.breaks);

  const consecutive = el("c-consecutive");
  markInvalid(
    consecutive,
    consecutive.value.trim() !== "" && rows.some((r) => r.consecutive === consecutive.value.trim()),
  );
}

// --- Borrador ---------------------------------------------------------
// Si el panel se reinicia a mitad de bobina, lo tecleado no se pierde.

function saveDraft() {
  const draft = {};
  for (const id of DRAFT_IDS) draft[id] = el(id).value;
  try {
    localStorage.setItem(DRAFT_KEY, JSON.stringify(draft));
  } catch {
    /* almacenamiento lleno o deshabilitado: no es motivo para romper la captura */
  }
}

function restoreDraft() {
  let draft = null;
  try {
    draft = JSON.parse(localStorage.getItem(DRAFT_KEY) || "null");
  } catch {
    draft = null;
  }
  if (!draft) return;
  for (const id of DRAFT_IDS) {
    if (id === "c-reference") continue;
    if (typeof draft[id] === "string") el(id).value = draft[id];
  }
  pendingRef = typeof draft["c-reference"] === "string" ? draft["c-reference"] : null;
  applyPendingRef();
}

// --- Tabla ------------------------------------------------------------

const LOCKED_LABEL = "Bloqueada: solo se corrige en modo ingeniería";

/**
 * Candado en vez de una cuenta atras escrita: el texto "Editable 24:12" gastaba
 * 132 px de columna para algo que solo importa media hora. El tiempo restante
 * sigue estando, en el `title`, que no ocupa ancho.
 */
function lockHtml(open, label, until = null) {
  const icon = open ? "lock-open" : "lock-closed";
  return `<span class="lock ${open ? "open" : "closed"}" role="img" aria-label="${label}"
    title="${label}"${until ? ` data-until="${until}"` : ""}
    ><svg viewBox="0 0 24 24"><use href="#${icon}" /></svg></span>`;
}

function stateHtml(row) {
  return row.editable
    ? lockHtml(true, "Editable", row.editable_until)
    : lockHtml(false, LOCKED_LABEL);
}

function rowHtml(row) {
  const zones = row.weight_profile
    .map((value, i) => {
      const edge = i === 0 ? " zone-first" : i === ZONES - 1 ? " zone-last" : "";
      return `<td class="num${edge}">${fmtNum(value)}</td>`;
    })
    .join("");

  const unlocked = row.editable || engMode.checked;
  return `
    <tr data-row="${row.id}">
      <td class="mono">${escapeHtml(row.reference ?? "—")}</td>
      <td class="mono">${escapeHtml(row.consecutive)}</td>
      <td class="num">${row.start_time}</td>
      <td class="num">${row.end_time}</td>
      <td class="num">${fmtNum(row.machine_speed)}</td>
      ${zones}
      <td class="num">${fmtNum(row.base_weight)}</td>
      <td class="num">${fmtNum(row.reel_weight)}</td>
      <td class="num">${fmtNum(row.breaks)}</td>
      <td>${escapeHtml(row.reel_type)}</td>
      <td>${stateHtml(row)}</td>
      <td>
        <div class="row-actions">
          ${unlocked ? `<button class="ghost" data-edit="${row.id}">Editar</button>` : ""}
          ${unlocked ? `<button class="ghost" data-del="${row.id}" title="Eliminar registro">✕</button>` : ""}
        </div>
      </td>
    </tr>`;
}

function render() {
  editingId = null;
  host.innerHTML = rows.map(rowHtml).join("");
  emptyBox.hidden = rows.length > 0;
  countLabel.innerHTML = rows.length ? `<b>${rows.length}</b> registros` : "";
  hintLabel.textContent = `Se puede corregir durante ${editWindowMin} min después de guardar.`;

  host.querySelectorAll("[data-edit]").forEach((btn) =>
    btn.addEventListener("click", () => startEdit(Number(btn.dataset.edit))),
  );
  host.querySelectorAll("[data-del]").forEach((btn) =>
    btn.addEventListener("click", () => removeRow(Number(btn.dataset.del))),
  );
  tickCountdowns();
}

/**
 * Cuenta atras de la ventana de edicion.
 * Reescribe solo el texto del chip: repintar la tabla cada segundo le robaria
 * el foco al operador mientras corrige una fila.
 */
function tickCountdowns() {
  const now = Date.now();

  for (const chip of host.querySelectorAll("[data-until]")) {
    const left = Math.floor((Date.parse(chip.dataset.until) - now) / 1000);
    if (left > 0) {
      const m = Math.floor(left / 60);
      const s = left % 60;
      const label = `Editable ${m}:${String(s).padStart(2, "0")}`;
      chip.title = label;
      chip.setAttribute("aria-label", label);
      continue;
    }

    // Vencio. Se apaga solo esa fila, sin recargar ni repintar la tabla: si el
    // operador esta corrigiendo otra fila, un repintado le borraria lo tecleado.
    const tr = chip.closest("tr");
    const id = Number(tr?.dataset.row);
    const row = rows.find((r) => r.id === id);
    if (row) row.editable = false;

    chip.removeAttribute("data-until");
    chip.className = "lock closed";
    chip.title = LOCKED_LABEL;
    chip.setAttribute("aria-label", LOCKED_LABEL);
    chip.querySelector("use")?.setAttribute("href", "#lock-closed");

    if (id === editingId) {
      // Se avisa pero no se cierra el editor: cerrarlo tiraria la correccion a
      // medio escribir. Si al guardar ya no hay permiso, el servidor responde
      // 409 y el mensaje lo explica.
      toast("Se acabó el tiempo para corregir esta fila", true);
    } else if (!engMode.checked) {
      tr?.querySelectorAll("[data-edit], [data-del]").forEach((btn) => btn.remove());
    }
  }
}

// --- Acciones ---------------------------------------------------------

async function load() {
  try {
    const data = await api.get(`/api/forms/operation?date=${dateInput.value}`);
    rows = data.records;
    editWindowMin = data.edit_window_min;
    fillReferences(data.references);
    render();
  } catch (err) {
    reportError(err);
  }
}

async function add() {
  const { payload, error, field } = collect();
  if (error) {
    toast(error, true);
    if (field) {
      markInvalid(field, true);
      field.focus();
      field.select?.();
    }
    return;
  }

  try {
    await api.post("/api/forms/operation", payload);
  } catch (err) {
    reportError(err);
    return;
  }

  // Menos tecleo en la siguiente bobina: las bobinas son consecutivas, asi que
  // la hora de inicio es la de fin de esta; referencia, velocidad y tipo rara
  // vez cambian (una corrida son muchas bobinas del mismo papel).
  el("c-start").value = payload.end_time;
  el("c-end").value = "";
  el("c-consecutive").value = "";
  for (let i = 1; i <= ZONES; i += 1) el(`c-z${i}`).value = "";
  el("c-base").value = "";
  el("c-reel").value = "";
  el("c-breaks").value = "";
  captureRow.querySelectorAll("[aria-invalid]").forEach((input) => markInvalid(input, false));

  // Se reescribe el borrador con lo que quedo arrastrado, no se borra: si el
  // panel se reinicia ahora, la hora de inicio de la siguiente sigue puesta.
  saveDraft();
  toast(`Bobina ${payload.consecutive} registrada`);
  el("c-consecutive").focus(); // la referencia ya quedo puesta de la anterior
  await load();
}

async function removeRow(id) {
  const row = rows.find((r) => r.id === id);
  if (!row) return;
  if (!confirm(`¿Eliminar el registro de la bobina "${row.consecutive}"?`)) return;

  const force = !row.editable && engMode.checked ? "?force=true" : "";
  try {
    await api.del(`/api/forms/operation/${id}${force}`);
    toast("Registro eliminado");
    await load();
  } catch (err) {
    reportError(err);
  }
}

// --- Edicion en sitio -------------------------------------------------
// Los mismos campos, en la misma fila. Sin modal.

/**
 * Celda de referencia del editor. Si la fila trae una que no esta en la lista
 * —o no trae ninguna, como las bobinas registradas antes de que existiera la
 * columna— abre directamente en modo "Otro" para no perder el valor.
 */
function refEditorCell(value) {
  const known = referenceList.includes(value);
  const options = [
    '<option value="">—</option>',
    ...referenceList.map(
      (ref) =>
        `<option value="${escapeHtml(ref)}" ${ref === value ? "selected" : ""}>${escapeHtml(ref)}</option>`,
    ),
    `<option value="${OTHER}" ${!known && value ? "selected" : ""}>Otro…</option>`,
  ].join("");
  const other = known || !value ? "hidden" : "";
  return `<td>
    <select data-f="reference" ${known || !value ? "" : "hidden"}>${options}</select>
    <input data-f="reference-other" ${other} maxlength="20" autocomplete="off"
      value="${escapeHtml(known ? "" : (value ?? ""))}" />
  </td>`;
}

function editorCell(id, value, extra = "") {
  return `<td><input data-f="${id}" class="num" type="text" inputmode="decimal"
    autocomplete="off" value="${escapeHtml(value)}" ${extra} /></td>`;
}

function startEdit(id) {
  const row = rows.find((r) => r.id === id);
  const tr = host.querySelector(`[data-row="${id}"]`);
  if (!row || !tr) return;

  editingId = id;
  tr.classList.add("editing");

  const zones = row.weight_profile
    .map((value, i) => {
      const edge = i === 0 ? "zone-first" : i === ZONES - 1 ? "zone-last" : "";
      return `<td class="${edge}"><input data-f="z${i + 1}" class="num" type="text"
        inputmode="decimal" autocomplete="off" value="${value}" /></td>`;
    })
    .join("");

  tr.innerHTML = `
    ${refEditorCell(row.reference)}
    <td><input data-f="consecutive" autocomplete="off" value="${escapeHtml(row.consecutive)}" /></td>
    <td><input data-f="start" type="time" value="${row.start_time}" /></td>
    <td><input data-f="end" type="time" value="${row.end_time}" /></td>
    ${editorCell("speed", row.machine_speed)}
    ${zones}
    ${editorCell("base", row.base_weight)}
    ${editorCell("reel", row.reel_weight)}
    <td><input data-f="breaks" class="num" type="text" inputmode="numeric"
      autocomplete="off" value="${row.breaks}" /></td>
    <td>
      <select data-f="type">
        <option value="x1" ${row.reel_type === "x1" ? "selected" : ""}>x1</option>
        <option value="x2" ${row.reel_type === "x2" ? "selected" : ""}>x2</option>
      </select>
    </td>
    <td>${stateHtml(row)}</td>
    <td>
      <div class="row-actions">
        <button class="primary" data-save="${id}">Guardar</button>
        <button class="ghost" data-cancel="1">✕</button>
      </div>
    </td>`;

  tr.querySelector('[data-f="reference"]').addEventListener("change", (event) => {
    const other = tr.querySelector('[data-f="reference-other"]');
    const on = event.target.value === OTHER;
    event.target.hidden = on;
    other.hidden = !on;
    if (on) other.focus();
  });

  tr.querySelector('[data-f="consecutive"]').focus();
  tr.querySelector("[data-save]").addEventListener("click", () => saveEdit(id, tr, row));
  tr.querySelector("[data-cancel]").addEventListener("click", () => {
    editingId = null;
    render();
  });
}

async function saveEdit(id, tr, original) {
  const get = (name) => tr.querySelector(`[data-f="${name}"]`);
  const profile = [];
  for (let i = 1; i <= ZONES; i += 1) {
    const input = get(`z${i}`);
    const value = parseNum(input.value);
    if (value === null || value < LIMITS.gsm[0] || value > LIMITS.gsm[1]) {
      toast(`La zona ${i} debe estar entre ${LIMITS.gsm[0]} y ${LIMITS.gsm[1]} g/m²`, true);
      markInvalid(input, true);
      input.focus();
      return;
    }
    profile.push(value);
  }

  const refSel = get("reference");
  const payload = {
    // Vacia si la fila es de antes de que existiera la columna: el bucle de
    // abajo lo atrapa y obliga a ponerla al corregir, que es lo que se quiere.
    reference:
      refSel.value === OTHER ? get("reference-other").value.trim().toUpperCase() : refSel.value,
    consecutive: get("consecutive").value.trim(),
    shift_date: original.shift_date,
    start_time: get("start").value,
    end_time: get("end").value,
    machine_speed: parseNum(get("speed").value),
    weight_profile: profile,
    base_weight: parseNum(get("base").value),
    reel_weight: parseNum(get("reel").value),
    breaks: parseNum(get("breaks").value),
    reel_type: get("type").value,
  };

  // El resto de rangos los valida el servidor y devuelve el motivo legible;
  // aqui solo se atrapa lo que ni siquiera es un numero.
  for (const [name, value] of Object.entries(payload)) {
    if (value === null || value === "") {
      toast(`Falta un valor en "${name}"`, true);
      return;
    }
  }

  const force = !original.editable && engMode.checked ? "?force=true" : "";
  try {
    await api.patch(`/api/forms/operation/${id}${force}`, payload);
    toast(force ? "Corregido como ingeniería" : "Registro corregido");
    editingId = null;
    await load();
  } catch (err) {
    reportError(err);
  }
}

// --- Cableado ---------------------------------------------------------

document.getElementById("btn-add").addEventListener("click", add);

document.getElementById("btn-today").addEventListener("click", () => {
  dateInput.value = todayISO();
  load();
});

dateInput.addEventListener("change", load);

document.getElementById("btn-csv").addEventListener("click", () => {
  location.href = `/api/forms/operation.csv?date=${dateInput.value}`;
});

engMode.addEventListener("change", () => {
  if (engMode.checked) {
    toast("Modo ingeniería activo: se pueden corregir filas bloqueadas");
  }
  render();
});

refSelect.addEventListener("change", () => {
  if (refSelect.value !== OTHER) return;
  setOtherMode(true);
  refOther.focus();
});

// Escape o dejarlo vacio devuelve a la lista: quien entro a "Otro" por error
// tiene que poder salir sin saber que existe el teclado.
refOther.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  refOther.value = "";
  refSelect.value = "";
  setOtherMode(false);
  refSelect.focus();
  saveDraft();
});

refOther.addEventListener("blur", () => {
  if (refOther.value.trim()) return;
  refSelect.value = "";
  setOtherMode(false);
});

captureRow.querySelectorAll("[data-stamp]").forEach((btn) =>
  btn.addEventListener("click", () => {
    el(btn.dataset.stamp).value = nowHHMM();
    saveDraft();
  }),
);

captureRow.addEventListener("input", () => {
  validateLive();
  saveDraft();
});

// Enter avanza al siguiente campo y, en el ultimo, agrega. Es como se recorre
// una fila en papel; Tab sigue funcionando igual.
captureRow.addEventListener("keydown", (event) => {
  if (event.key !== "Enter") return;
  event.preventDefault();
  // En modo "Otro" el campo visible no esta en el recorrido; cuenta como la
  // celda de referencia, o Enter agregaria la fila a medio llenar.
  const id = event.target.id === "c-reference-other" ? "c-reference" : event.target.id;
  const index = FIELD_IDS.indexOf(id);
  if (index === -1 || index === FIELD_IDS.length - 1) {
    add();
    return;
  }
  el(FIELD_IDS[index + 1]).focus();
});

dateInput.value = todayISO();
restoreDraft();
validateLive();
el("c-reference").focus();
load();
setInterval(tickCountdowns, 1000);
