import { fmtDateTime, fmtDuration } from "./format.js";
import { mountTopbar } from "./shell.js";

mountTopbar("home");

const STATE_LABEL = {
  ok: ["good", "Adquiriendo"],
  degraded: ["bad", "PLC sin responder"],
  down: ["bad", "Worker detenido"],
};

document.addEventListener("adanvi:health", (event) => render(event.detail));

function render(health) {
  const host = document.getElementById("health-cards");
  if (!host) return;

  if (!health) {
    host.innerHTML = `<div class="empty">Sin conexión con el servidor de ADANVI.</div>`;
    return;
  }

  const [chipClass, label] = STATE_LABEL[health.state] || ["idle", "Desconocido"];
  const gap = health.gap_open_since
    ? `<p><span class="chip bad">Hueco abierto</span> desde ${fmtDateTime(
        Date.parse(health.gap_open_since) / 1000,
      )}</p>`
    : "";

  host.innerHTML = `
    <div class="card">
      <h3>Adquisición <span class="chip ${chipClass}">${label}</span></h3>
      <p>${health.polled_tags} tags leídos por ciclo.</p>
      <p>Último ciclo: ${
        health.last_cycle_ts ? fmtDateTime(Date.parse(health.last_cycle_ts) / 1000) : "—"
      }</p>
      ${gap}
    </div>
    <div class="card">
      <h3>Cadencia</h3>
      <p>Jitter p95: <b class="num">${health.cycle_jitter_ms_p95} ms</b></p>
      <p>Retraso del último ciclo: ${
        health.seconds_since_last_cycle === null
          ? "—"
          : fmtDuration(health.seconds_since_last_cycle)
      }</p>
      <p>Suscriptores en vivo: ${health.ws_subscribers}</p>
    </div>
    <div class="card">
      <h3>Escritura</h3>
      <p>Cola pendiente: <b class="num">${health.write_queue_depth}</b></p>
      <p>Mensajes descartados: <b class="num">${health.dropped_writes}</b></p>
      ${
        health.last_error
          ? `<p><span class="chip bad">Error</span> ${escapeHtml(health.last_error)}</p>`
          : ""
      }
    </div>`;
}

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}
