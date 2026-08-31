import { api, reportError, toast } from "./api.js";
import { fmtNumber } from "./format.js";
import { mountTopbar } from "./shell.js";

mountTopbar("tags");

const host = document.getElementById("tags-host");
const form = document.getElementById("tag-form");

let rows = [];

async function load() {
  try {
    rows = await api.get("/api/tags");
    render();
  } catch (err) {
    reportError(err);
  }
}

function render() {
  if (!rows.length) {
    host.innerHTML = `<div class="empty">Aún no hay tags. Agrega el primero arriba.</div>`;
    return;
  }

  host.innerHTML = `
    <table>
      <thead>
        <tr>
          <th>Nombre CIP</th>
          <th>Etiqueta</th>
          <th>Unidad</th>
          <th>Tipo</th>
          <th class="num">Último valor</th>
          <th>Calidad</th>
          <th>Activo</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        ${rows.map(rowHtml).join("")}
      </tbody>
    </table>`;

  host.querySelectorAll("[data-toggle]").forEach((el) =>
    el.addEventListener("change", () => toggleActive(Number(el.dataset.toggle), el.checked)),
  );
  host.querySelectorAll("[data-delete]").forEach((el) =>
    el.addEventListener("click", () => removeTag(Number(el.dataset.delete))),
  );
}

function qualityHtml(statusName) {
  if (statusName === "Good") return '<span class="chip good">Good</span>';
  if (!statusName || statusName === "Pending") return '<span class="chip idle">Sin leer</span>';
  return `<span class="chip bad">${escapeHtml(statusName)}</span>`;
}

function rowHtml(tag) {
  return `
    <tr>
      <td class="mono">${escapeHtml(tag.name)}</td>
      <td>${escapeHtml(tag.label || "—")}</td>
      <td>${escapeHtml(tag.unit || "—")}</td>
      <td>${escapeHtml(tag.value_type || tag.kind)}</td>
      <td class="num" data-value="${tag.id}">${fmtNumber(tag.last_value, tag.decimals)}</td>
      <td data-quality="${tag.id}">${qualityHtml(tag.last_status)}</td>
      <td><input type="checkbox" data-toggle="${tag.id}" ${tag.active ? "checked" : ""} /></td>
      <td class="num">
        <button class="ghost" data-delete="${tag.id}" title="Eliminar tag">✕</button>
      </td>
    </tr>`;
}

/**
 * Refresco en vivo de solo las celdas de valor y calidad.
 * Repintar la tabla entera cada pocos segundos haria saltar el foco y pelearia
 * con el usuario mientras marca casillas.
 */
async function refreshLive() {
  if (!rows.length) return;
  try {
    const { readings } = await api.get("/api/live/snapshot");
    const byId = new Map(readings.map((r) => [r.tag_id, r]));
    for (const tag of rows) {
      const live = byId.get(tag.id);
      const valueCell = host.querySelector(`[data-value="${tag.id}"]`);
      const qualityCell = host.querySelector(`[data-quality="${tag.id}"]`);
      if (!valueCell || !qualityCell) continue;
      valueCell.textContent = fmtNumber(live ? live.value : null, tag.decimals);
      qualityCell.innerHTML = qualityHtml(live ? live.status_name : "Pending");
    }
  } catch {
    /* el topbar ya reporta la caida del servidor */
  }
}

async function toggleActive(id, active) {
  try {
    await api.patch(`/api/tags/${id}`, { active });
    toast(active ? "Tag activado" : "Tag desactivado: el worker deja de leerlo");
    await load();
  } catch (err) {
    reportError(err);
    await load();
  }
}

async function removeTag(id) {
  const tag = rows.find((r) => r.id === id);
  // Se separan las dos acciones a proposito: desactivar es reversible y conserva
  // el historico; purgar lo borra y no tiene vuelta atras.
  if (!confirm(`¿Eliminar "${tag.name}" y TODO su histórico?\n\nPara solo dejar de leerlo, usa la casilla "Activo".`)) {
    return;
  }
  try {
    await api.del(`/api/tags/${id}?purge=true`);
    toast("Tag e histórico eliminados");
    await load();
  } catch (err) {
    reportError(err);
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const data = Object.fromEntries(new FormData(form).entries());
  const payload = {
    name: data.name.trim(),
    label: data.label?.trim() || null,
    unit: data.unit?.trim() || null,
    decimals: Number(data.decimals) || 0,
    kind: data.kind,
  };
  if (!payload.name) return;

  try {
    await api.post("/api/tags", payload);
    form.reset();
    document.getElementById("f-decimals").value = "2";
    document.getElementById("f-name").focus();
    toast(`Tag "${payload.name}" agregado`);
    await load();
  } catch (err) {
    reportError(err);
  }
});

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text ?? "";
  return div.innerHTML;
}

load();
setInterval(refreshLive, 2000);
