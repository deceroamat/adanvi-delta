import { api, reportError, toast } from "./api.js";
import { mountTopbar } from "./shell.js";

mountTopbar("galleries");

const host = document.getElementById("galleries-host");
const form = document.getElementById("gallery-form");

async function load() {
  try {
    const { galleries, max_galleries: max } = await api.get("/api/galleries");
    render(galleries, max);
  } catch (err) {
    reportError(err);
  }
}

function render(galleries, max) {
  const button = form.querySelector("button");
  const atLimit = max > 0 && galleries.length >= max;
  button.disabled = atLimit;
  button.title = atLimit ? `Límite de ${max} galerías alcanzado` : "";

  if (!galleries.length) {
    host.innerHTML = `<div class="empty">Aún no hay galerías. Crea la primera arriba.</div>`;
    return;
  }

  host.innerHTML = `<div class="cards">${galleries.map(cardHtml).join("")}</div>`;
  host.querySelectorAll("[data-rename]").forEach((el) =>
    el.addEventListener("click", () => rename(Number(el.dataset.rename))),
  );
  host.querySelectorAll("[data-delete]").forEach((el) =>
    el.addEventListener("click", () => remove(Number(el.dataset.delete))),
  );
}

function cardHtml(gallery) {
  const count = Number(gallery.series_count);
  return `
    <div class="card">
      <h3>${escapeHtml(gallery.name)}</h3>
      <p>${count} ${count === 1 ? "serie" : "series"}</p>
      <div class="card-actions">
        <a class="btn" href="/galleries/${gallery.id}">Abrir</a>
        <button class="ghost" data-rename="${gallery.id}">Renombrar</button>
        <button class="ghost danger" data-delete="${gallery.id}">Eliminar</button>
      </div>
    </div>`;
}

async function rename(id) {
  const name = prompt("Nuevo nombre de la galería:");
  if (!name?.trim()) return;
  try {
    await api.patch(`/api/galleries/${id}`, { name: name.trim() });
    await load();
  } catch (err) {
    reportError(err);
  }
}

async function remove(id) {
  if (!confirm("¿Eliminar la galería?\n\nSe pierde su configuración de series, pero NO el histórico de los tags.")) {
    return;
  }
  try {
    await api.del(`/api/galleries/${id}`);
    toast("Galería eliminada");
    await load();
  } catch (err) {
    reportError(err);
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const name = new FormData(form).get("name").trim();
  if (!name) return;
  try {
    const gallery = await api.post("/api/galleries", { name });
    location.href = `/galleries/${gallery.id}`;
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
