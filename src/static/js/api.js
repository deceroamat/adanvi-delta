// Cliente HTTP minimo. Sin dependencias.

export class ApiError extends Error {
  constructor(status, detail) {
    super(detail || `HTTP ${status}`);
    this.status = status;
    this.detail = detail;
  }
}

async function request(method, path, body, options = {}) {
  const init = { method, headers: {}, signal: options.signal };
  if (body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }

  const res = await fetch(path, init);
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const payload = await res.json();
      if (typeof payload.detail === "string") {
        detail = payload.detail;
      } else if (Array.isArray(payload.detail)) {
        // Errores de validacion de FastAPI: se muestra el primero, legible.
        detail = payload.detail.map((e) => e.msg).join("; ");
      }
    } catch {
      /* respuesta sin cuerpo JSON */
    }
    throw new ApiError(res.status, detail);
  }
  return res.status === 204 ? null : res.json();
}

export const api = {
  get: (path, options) => request("GET", path, undefined, options),
  post: (path, body) => request("POST", path, body),
  patch: (path, body) => request("PATCH", path, body),
  put: (path, body) => request("PUT", path, body),
  del: (path) => request("DELETE", path),
};

// --- Avisos -----------------------------------------------------------

let toastTimer = null;

export function toast(message, isError = false) {
  document.querySelector(".toast")?.remove();
  const el = document.createElement("div");
  el.className = isError ? "toast error" : "toast";
  el.role = "status";
  el.textContent = message;
  document.body.appendChild(el);
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.remove(), isError ? 6000 : 3000);
}

export function reportError(err) {
  if (err?.name === "AbortError") return; // cancelacion esperada al hacer pan
  console.error(err);
  toast(err?.detail || err?.message || "Error inesperado", true);
}
