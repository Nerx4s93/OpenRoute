import { localeFor, translate, type Language } from "../i18n";

const numberFormatters = new Map<string, Intl.NumberFormat>();
const compactFormatters = new Map<string, Intl.NumberFormat>();

function numberFormatter(language: Language): Intl.NumberFormat {
  let formatter = numberFormatters.get(language);
  if (!formatter) {
    formatter = new Intl.NumberFormat(localeFor(language));
    numberFormatters.set(language, formatter);
  }
  return formatter;
}

function compactFormatter(language: Language): Intl.NumberFormat {
  let formatter = compactFormatters.get(language);
  if (!formatter) {
    formatter = new Intl.NumberFormat(localeFor(language), {
      notation: "compact",
      maximumFractionDigits: 1,
    });
    compactFormatters.set(language, formatter);
  }
  return formatter;
}

export function formatNumber(value: number, language: Language): string {
  return numberFormatter(language).format(value);
}

export function formatCompact(value: number, language: Language): string {
  return compactFormatter(language).format(value);
}

export function formatLimit(
  value: number | null | undefined,
  language: Language,
): string {
  return value === null || value === undefined ? "—" : formatNumber(value, language);
}

export function formatDuration(seconds: number, language: Language): string {
  const total = Math.max(0, Math.trunc(seconds));
  if (total < 60) return translate(language, "duration.lessThanMinute");
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  if (hours) {
    return translate(language, "duration.hours", {
      hours,
      minutes: String(minutes).padStart(2, "0"),
    });
  }
  return translate(language, "duration.minutes", { minutes });
}

export function formatTimestamp(ts: number, language: Language): string {
  return new Intl.DateTimeFormat(localeFor(language), {
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(new Date(ts * 1000));
}

export function formatServerTime(
  iso: string | null | undefined,
  language: Language,
): string {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat(localeFor(language), {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(date);
}

export function formatShortDate(value: string, language: Language): string {
  const [year, month, day] = value.split("-").map(Number);
  if (!year || !month || !day) return value;
  return new Intl.DateTimeFormat(localeFor(language), {
    month: "short",
    day: "numeric",
    timeZone: "UTC",
  }).format(new Date(Date.UTC(year, month - 1, day)));
}

export function formatLongDate(value: string, language: Language): string {
  const [year, month, day] = value.split("-").map(Number);
  if (!year || !month || !day) return value;
  return new Intl.DateTimeFormat(localeFor(language), {
    weekday: "short",
    day: "numeric",
    month: "long",
    year: "numeric",
    timeZone: "UTC",
  }).format(new Date(Date.UTC(year, month - 1, day)));
}

export function formatModels(models: string[] | null | undefined): string {
  return models && models.length ? models.join(", ") : "—";
}

export function requestTokens(prompt?: number | null, completion?: number | null): number {
  return (prompt ?? 0) + (completion ?? 0);
}

export function formatMoney(
  value: number,
  currency = "RUB",
  language: Language = "en",
): string {
  try {
    return new Intl.NumberFormat(localeFor(language), {
      style: "currency",
      currency: currency || "RUB",
      currencyDisplay: "narrowSymbol",
      maximumFractionDigits: 2,
      minimumFractionDigits: 2,
    }).format(value);
  } catch {
    return `${value.toFixed(2)} ${currency}`;
  }
}
