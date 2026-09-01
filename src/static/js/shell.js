// Topbar comun: marca, navegacion y estado del PLC.
// El estado del worker se muestra siempre: uno de los fallos del prototipo fue
// que el worker podia morir en silencio sin que nadie lo notara.

import { api } from "./api.js";
import { fmtTime } from "./format.js";

const HEALTH_INTERVAL_MS = 3000;

export function mountTopbar(active) {
  const bar = document.querySelector(".topbar");
  if (!bar) return;
  bar.innerHTML = `
    <a class="brand" href="/"><strong>ADANVI</strong><span>by emolog</span></a>
    <nav class="nav">
      <a href="/" ${active === "home" ? 'aria-current="page"' : ""}>Inicio</a>
      <a href="/tags" ${active === "tags" ? 'aria-current="page"' : ""}>Tags</a>
      <a href="/galleries" ${active === "galleries" ? 'aria-current="page"' : ""}>Galerías</a>
    </nav>
    <div class="topbar-stats">
      <span class="stat" id="stat-tags"></span>
      <span class="stat" id="stat-cycle"></span>
      <span class="stat" id="stat-plc"><span class="dot"></span><span>PLC —</span></span>
    </div>`;

  startHealthPolling();
}

let timer = null;

function startHealthPolling() {
  const tick = async () => {
    let health = null;
    try {
      health = await api.get("/api/health");
    } catch {
      /* servidor caido: se pinta como tal */
    }
    renderHealth(health);
    document.dispatchEvent(new CustomEvent("adanvi:health", { detail: health }));
  };
  tick();
  clearInterval(timer);
  timer = setInterval(tick, HEALTH_INTERVAL_MS);
}

function renderHealth(health) {
  const plc = document.getElementById("stat-plc");
  const tags = document.getElementById("stat-tags");
  const cycle = document.getElementById("stat-cycle");
  if (!plc) return;

  if (!health) {
    plc.innerHTML = `<span class="dot bad"></span><span>Sin conexión al servidor</span>`;
    tags.textContent = "";
    cycle.textContent = "";
    return;
  }

  // Estado codificado por color Y por texto: el color solo no es accesible.
  const map = {
    ok: ["ok", "ADANVI conectado"],
    degraded: ["warn", "PLC desconectado"],
    down: ["bad", "Worker detenido"],
  };
  const [cls, text] = map[health.state] || ["", "PLC —"];
  plc.innerHTML = `<span class="dot ${cls}"></span><span>${text}</span>`;
  plc.title = health.last_error ? `Último error: ${health.last_error}` : "";

  tags.innerHTML = `<b>${health.polled_tags}</b> tags`;
  cycle.innerHTML = health.last_cycle_ts
    ? `<b>${fmtTime(Date.parse(health.last_cycle_ts) / 1000)}</b>`
    : "";
  cycle.title = `Jitter de ciclo p95: ${health.cycle_jitter_ms_p95} ms`;
}
