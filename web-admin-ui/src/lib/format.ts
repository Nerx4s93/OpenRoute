const numberFormatter = new Intl.NumberFormat("ru-RU");
const compactFormatter = new Intl.NumberFormat("ru-RU", {
  notation: "compact",
  maximumFractionDigits: 1,
});

export function formatNumber(value: number): string {
  return numberFormatter.format(value);
}

export function formatCompact(value: number): string {
  return compactFormatter.format(value);
}

export function formatLimit(value: number | null | undefined): string {
  return value === null || value === undefined ? "—" : formatNumber(value);
}

export function formatDuration(seconds: number): string {
  const total = Math.max(0, Math.trunc(seconds));
  if (total < 60) return "<1м";
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  if (hours) return `${hours}ч ${String(minutes).padStart(2, "0")}м`;
  return `${minutes}м`;
}

export function formatTimestamp(ts: number): string {
  return new Intl.DateTimeFormat("ru-RU", {
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(new Date(ts * 1000));
}

export function formatServerTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat("ru-RU", {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(date);
}

export function formatModels(models: string[] | null | undefined): string {
  return models && models.length ? models.join(", ") : "—";
}

export function requestTokens(prompt?: number | null, completion?: number | null): number {
  return (prompt ?? 0) + (completion ?? 0);
}

export function formatMoney(value: number, currency = "RUB"): string {
  try {
    return new Intl.NumberFormat("ru-RU", {
      style: "currency",
      currency: currency || "RUB",
      maximumFractionDigits: 2,
      minimumFractionDigits: 2,
    }).format(value);
  } catch {
    return `${value.toFixed(2)} ${currency}`;
  }
}
