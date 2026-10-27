import { translate, type Language, type TranslationKey } from "../i18n";

export const VERSIONS: ReadonlyArray<{ value: number; labelKey: TranslationKey }> = [
  { value: 3, labelKey: "proxies.version3" },
  { value: 4, labelKey: "proxies.version4" },
  { value: 6, labelKey: "proxies.version6" },
];

export const PERIODS = [3, 7, 14, 30, 60, 90] as const;

export function periodsForVersion(version: number): readonly number[] {
  return version === 6 ? PERIODS : PERIODS.filter((days) => days >= 7);
}

export function versionLabel(value: string | number, language: Language): string {
  if (value === "") return "-";
  const version = String(value);
  const item = VERSIONS.find((entry) => String(entry.value) === version);
  return item ? translate(language, item.labelKey) : version;
}

export function countryLabel(code: string, language: Language): string {
  if (!code) return "—";
  try {
    const name = new Intl.DisplayNames([language === "ru" ? "ru" : "en"], {
      type: "region",
    }).of(code.toUpperCase());
    return name ? `${name} (${code.toUpperCase()})` : code.toUpperCase();
  } catch {
    return code.toUpperCase();
  }
}
