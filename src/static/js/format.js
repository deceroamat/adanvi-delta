// Formato de numeros y fechas. Hora local es-CO; el almacenamiento es UTC.

const TZ_LOCALE = "es-CO";

export function fmtNumber(value, decimals = 2) {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return value.toLocaleString(TZ_LOCALE, {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
}

const timeFmt = new Intl.DateTimeFormat(TZ_LOCALE, {
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
});

const dateTimeFmt = new Intl.DateTimeFormat(TZ_LOCALE, {
  day: "2-digit",
  month: "2-digit",
  year: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
});

export function fmtTime(epochSeconds) {
  return timeFmt.format(new Date(epochSeconds * 1000));
}

export function fmtDateTime(epochSeconds) {
  return dateTimeFmt.format(new Date(epochSeconds * 1000));
}

/** Etiqueta de eje adaptada al span: sin fecha en ventanas cortas. */
export function axisLabel(epochSeconds, spanSeconds) {
  const d = new Date(epochSeconds * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  if (spanSeconds < 3600) return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  if (spanSeconds < 86400 * 2) return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  if (spanSeconds < 86400 * 90) return `${pad(d.getDate())}/${pad(d.getMonth() + 1)}`;
  return `${pad(d.getMonth() + 1)}/${d.getFullYear()}`;
}

/** Duracion legible: 5400 -> "1h 30m". */
export function fmtDuration(seconds) {
  const s = Math.round(seconds);
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) {
    const h = Math.floor(s / 3600);
    const m = Math.round((s % 3600) / 60);
    return m ? `${h}h ${m}m` : `${h}h`;
  }
  const d = Math.floor(s / 86400);
  const h = Math.round((s % 86400) / 3600);
  return h ? `${d}d ${h}h` : `${d}d`;
}

/** Convierte un epoch a el valor que espera <input type="datetime-local">. */
export function toDatetimeLocal(epochSeconds) {
  const d = new Date(epochSeconds * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  return (
    `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}` +
    `T${pad(d.getHours())}:${pad(d.getMinutes())}`
  );
}

export function fromDatetimeLocal(value) {
  const ms = new Date(value).getTime();
  return Number.isNaN(ms) ? null : ms / 1000;
}
