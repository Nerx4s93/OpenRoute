import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

export type Language = "en" | "ru";

export const LANGUAGES: readonly Language[] = ["en", "ru"];
export const DEFAULT_LANGUAGE: Language = "en";
export const LANGUAGE_STORAGE_KEY = "openroute.admin.language";

const en = {
  "app.adminConsole": "Admin console",
  "app.navManagement": "Management",
  "app.navProxies": "Proxies",
  "app.navStats": "Statistics",
  "app.navKeys": "Keys",
  "app.navSettings": "Settings",
  "app.logoutPending": "Signing out…",
  "app.logout": "Sign out",
  "app.breadcrumbRoot": "Control panel",
  "app.dataAsOf": "Data as of",
  "page.stats": "Statistics",
  "page.keys": "Keys",
  "page.proxies": "Proxies",
  "page.settings": "Settings",

  "common.loading": "Loading…",
  "common.retry": "Retry",
  "common.cancel": "Cancel",
  "common.delete": "Delete",
  "common.deleting": "Deleting…",
  "common.copy": "Copy",
  "common.copied": "Copied",
  "common.done": "Done",
  "common.buy": "Buy",
  "common.buying": "Buying…",
  "common.calculating": "Calculating…",
  "common.enable": "Enable",
  "common.disable": "Disable",
  "common.empty": "Empty",
  "common.loadFailed": "Could not load data",
  "common.refresh": "Refresh list",
  "common.refreshing": "Refreshing…",
  "common.selected": "Selected",
  "common.close": "Close",

  "login.eyebrow": "Administration panel",
  "login.headlineLine1": "Every route",
  "login.headlineLine2": "under control.",
  "login.lead": "A single place to manage proxies, keys, and service activity.",
  "login.kicker": "Secure access",
  "login.subtitle": "Enter the administrator password to continue.",
  "login.password": "Password",
  "login.passwordPlaceholder": "Enter password",
  "login.submitPending": "Signing in…",
  "login.submit": "Sign in to panel   ↗",
  "login.retryAfter": "Try again in {seconds} s.",
  "login.help": "Panel access is protected by a server-side session",

  "full.loading": "Loading panel…",
  "full.disabledEyebrow": "Panel disabled",
  "full.disabledTitle": "Admin panel unavailable",
  "full.disabledHintPrefix": "Set ",
  "full.disabledHintMiddle": " in ",
  "full.disabledHintSuffix": " and restart the service.",
  "full.errorEyebrow": "Error",
  "full.errorTitle": "Could not load the panel",

  "settings.eyebrow": "System / settings",
  "settings.title": "Settings",
  "settings.languageTitle": "Language",
  "settings.languageNote": "Interface",
  "settings.languageLabel": "Interface language",
  "settings.english": "English",
  "settings.russian": "Русский",
  "settings.englishHint": "English interface",
  "settings.russianHint": "Russian interface",
  "settings.selected": "Selected",

  "period.title": "Spend period",
  "period.aria": "Spend period",
  "period.1d.label": "1d",
  "period.1d.title": "Since the start of today (00:00 UTC)",
  "period.3d.label": "3d",
  "period.3d.title": "For 3 days, including today (00:00 UTC)",
  "period.7d.label": "7d",
  "period.7d.title": "For 7 days, including today (00:00 UTC)",
  "period.30d.label": "30d",
  "period.30d.title": "For 30 days, including today (00:00 UTC)",
  "period.90d.label": "90d",
  "period.90d.title": "For 90 days, including today (00:00 UTC)",
  "period.all.label": "All time",
  "period.all.title": "For all time",
  "period.1d.caption": "over 1 day",
  "period.3d.caption": "over 3 days",
  "period.7d.caption": "over 7 days",
  "period.30d.caption": "over 30 days",
  "period.90d.caption": "over 90 days",
  "period.1d.section": "Last day",
  "period.3d.section": "Last 3 days",
  "period.7d.section": "Last 7 days",
  "period.30d.section": "Last 30 days",
  "period.90d.section": "Last 90 days",
  "period.all.section": "All time",

  "stats.eyebrow": "System / statistics",
  "stats.title": "Statistics",
  "stats.requestsPeriod": "Requests {period}",
  "stats.requestsTotal": "Requests total",
  "stats.tokensPeriod": "Tokens {period}",
  "stats.tokensTotal": "Tokens spent",
  "stats.proxiesRotation": "Proxies in rotation",
  "stats.loadFailed": "Could not load data",
  "stats.recentTitle": "Recent requests",
  "stats.recentNote": "Activity log",
  "stats.time": "Time",
  "stats.key": "Key",
  "stats.model": "Model",
  "stats.status": "Status",
  "stats.latency": "Latency",
  "stats.tokens": "Tokens",
  "stats.proxy": "Proxy",
  "stats.error": "Error",
  "stats.latencyValue": "{value} ms",
  "stats.empty": "No requests yet",

  "tokenStats.title": "Tokens by day",
  "tokenStats.note": "Days — UTC",
  "tokenStats.other": "Other",
  "tokenStats.loadFailed": "Could not load statistics",
  "tokenStats.loading": "Loading…",

  "proxyStats.title": "Proxies",
  "proxyStats.allTime": "All time",
  "proxyStats.proxy": "Proxy",
  "proxyStats.inPoolHeader": "In pool",
  "proxyStats.tokensPeriod": "Tokens for the period",
  "proxyStats.requests": "Requests",
  "proxyStats.errors": "Errors",
  "proxyStats.inPool": "In pool",
  "proxyStats.outOfPool": "Out of pool",
  "proxyStats.empty": "No proxies",

  "keys.eyebrow": "Access / management",
  "keys.title": "Client keys",
  "keys.addTitle": "Add client key",
  "keys.addNote": "New access",
  "keys.keyId": "Key ID",
  "keys.keyIdPlaceholder": "Generated automatically",
  "keys.noLimit": "No limit",
  "keys.modelsLabel": "Allowed models",
  "keys.modelsPlaceholder": "Comma-separated; empty means all models",
  "keys.creating": "Creating…",
  "keys.create": "＋   Create key",
  "keys.invalidLimit": "Invalid {field}: {value}",
  "keys.loadFailed": "Could not load keys",
  "keys.listTitle": "Client keys",
  "keys.listNote": "Access management",
  "keys.rpm": "RPM",
  "keys.tpm": "TPM",
  "keys.modelsColumn": "Models",
  "keys.status": "Status",
  "keys.actions": "Actions",
  "keys.reissue": "Reissue",
  "keys.revoke": "Revoke",
  "keys.empty": "No keys yet — add one above",
  "keys.statsTitle": "Key statistics",
  "keys.requests": "Requests",
  "keys.errors": "Errors",
  "keys.tokens": "Tokens",
  "keys.noRequests": "No requests yet",
  "keys.createdTitle": "Key created",
  "keys.reissuedTitle": "Key reissued",
  "keys.reissueTitle": "Reissue key {keyId}?",
  "keys.reissuing": "Reissuing…",
  "keys.reissueWarning": "The old key will stop working immediately.",
  "keys.deleteTitle": "Delete key {keyId}?",
  "keys.deleteWarning": "The key will be deleted permanently. Its statistics will remain in the log.",

  "keyState.active": "active",
  "keyState.revoked": "revoked",
  "keyState.deleted": "deleted",

  "secret.lead": "Copy the access key now — it will not be shown again after this window closes.",
  "secret.keyId": "Key ID:",
  "secret.aria": "Access key",
  "secret.warning": "The key cannot be recovered. If it is lost, it can only be reissued.",
  "secret.copy": "Copy",
  "secret.copied": "Copied",
  "secret.done": "Done",

  "proxies.eyebrow": "px6.net / management",
  "proxies.title": "Proxies",
  "proxies.refreshTitle": "Request a fresh list from px6.net and clear the cache",
  "proxies.refreshing": "Refreshing…",
  "proxies.refresh": "Refresh list",
  "proxies.loadFailed": "Could not load proxies",
  "proxies.accountError": "px6.net balance: {message}",
  "proxies.accountLoadFailed": "could not load",
  "proxies.balance": "Balance",
  "proxies.enabled": "Proxies enabled",
  "proxies.ready": "Ready for rotation",
  "proxies.buyTitle": "Buy proxies",
  "proxies.version": "Version",
  "proxies.version3": "Shared IPv4",
  "proxies.version4": "IPv4",
  "proxies.version6": "IPv6",
  "proxies.country": "Country",
  "proxies.count": "Quantity",
  "proxies.period": "Period",
  "proxies.loading": "Loading…",
  "proxies.countriesError": "Could not load countries: {message}",
  "proxies.priceError": "Could not calculate the price: {message}",
  "proxies.invalidCount": "Enter a quantity from 1 to 1000",
  "proxies.summary": "{count} × {version}, {country}, {days}",
  "proxies.buy": "Buy",
  "proxies.buying": "Buying…",
  "proxies.listTitle": "px6.net proxies",
  "proxies.listNote": "{enabled} of {total} enabled",
  "proxies.proxy": "Proxy",
  "proxies.countryColumn": "Country",
  "proxies.versionColumn": "Version",
  "proxies.validUntil": "Valid until",
  "proxies.status": "Status",
  "proxies.tokens": "Tokens",
  "proxies.actions": "Actions",
  "proxies.empty": "No proxies found. Click “Refresh list” or buy proxies.",
  "proxies.confirmTitle": "Buy proxies?",
  "proxies.confirmProxies": "Proxies",
  "proxies.confirmPeriod": "Period",
  "proxies.confirmPrice": "Price",
  "proxies.confirmBalanceAfter": "Balance after",
  "proxies.toggleOffTitle": "Exclude this proxy from the gateway pool",
  "proxies.toggleOnTitle": "Allow the gateway to use this proxy",
  "proxies.disable": "Disable",
  "proxies.enable": "Enable",

  "proxyState.banned": "banned",
  "proxyState.dead": "dead",
  "proxyState.disabled": "disabled",
  "proxyState.notInPool": "not in pool",
  "proxyState.bannedFor": "banned {duration}",
  "proxyState.quota": "limit {duration}",
  "proxyState.cooldown": "cooldown {seconds}s",
  "proxyState.rpmCapped": "RPM capped",
  "proxyState.ok": "ok",

  "tokenStats.chartNoData": "No data for the selected period",
  "tokenStats.chartLegend": "Legend: {labels}",
  "tokenStats.chartAria": "Tokens by day (UTC)",
  "tokenStats.chartSpend": "Spend: {tokens} tokens",

  "duration.lessThanMinute": "<1m",
  "duration.hours": "{hours}h {minutes}m",
  "duration.minutes": "{minutes}m",
  "days.one": "{count} day",
  "days.few": "{count} days",
  "days.many": "{count} days",

  "api.requestError": "Request failed ({status})",
  "api.networkError": "Server is unavailable. Check your connection and try again.",
  "api.invalidResponse": "The server returned an invalid response",
} as const;

export type TranslationKey = keyof typeof en;

const ru: Record<TranslationKey, string> = {
  "app.adminConsole": "Админ-консоль",
  "app.navManagement": "Управление",
  "app.navProxies": "Прокси",
  "app.navStats": "Статистика",
  "app.navKeys": "Ключи",
  "app.navSettings": "Настройки",
  "app.logoutPending": "Выход…",
  "app.logout": "Выйти из панели",
  "app.breadcrumbRoot": "Панель управления",
  "app.dataAsOf": "Данные на",
  "page.stats": "Статистика",
  "page.keys": "Ключи",
  "page.proxies": "Прокси",
  "page.settings": "Настройки",

  "common.loading": "Загрузка…",
  "common.retry": "Повторить",
  "common.cancel": "Отмена",
  "common.delete": "Удалить",
  "common.deleting": "Удаление…",
  "common.copy": "Скопировать",
  "common.copied": "Скопировано",
  "common.done": "Готово",
  "common.buy": "Купить",
  "common.buying": "Покупаю…",
  "common.calculating": "Считаю…",
  "common.enable": "Включить",
  "common.disable": "Выключить",
  "common.empty": "Пусто",
  "common.loadFailed": "Не удалось загрузить данные",
  "common.refresh": "Обновить список",
  "common.refreshing": "Обновляю…",
  "common.selected": "Выбран",
  "common.close": "Закрыть",

  "login.eyebrow": "Административная панель",
  "login.headlineLine1": "Каждый маршрут",
  "login.headlineLine2": "под контролем.",
  "login.lead": "Единое пространство для управления прокси, ключами и активностью сервиса.",
  "login.kicker": "Защищённый доступ",
  "login.subtitle": "Введите пароль администратора, чтобы продолжить.",
  "login.password": "Пароль",
  "login.passwordPlaceholder": "Введите пароль",
  "login.submitPending": "Вход…",
  "login.submit": "Войти в панель   ↗",
  "login.retryAfter": "Повторите через {seconds} с.",
  "login.help": "Доступ к панели защищён серверной сессией",

  "full.loading": "Загрузка панели…",
  "full.disabledEyebrow": "Панель выключена",
  "full.disabledTitle": "Админка недоступна",
  "full.disabledHintPrefix": "Задайте ",
  "full.disabledHintMiddle": " в ",
  "full.disabledHintSuffix": " и перезапустите сервис.",
  "full.errorEyebrow": "Ошибка",
  "full.errorTitle": "Не удалось загрузить панель",

  "settings.eyebrow": "Система / настройки",
  "settings.title": "Настройки",
  "settings.languageTitle": "Язык",
  "settings.languageNote": "Интерфейс",
  "settings.languageLabel": "Язык интерфейса",
  "settings.english": "English",
  "settings.russian": "Русский",
  "settings.englishHint": "Английский интерфейс",
  "settings.russianHint": "Русский интерфейс",
  "settings.selected": "Выбран",

  "period.title": "Период расходов",
  "period.aria": "Период расходов",
  "period.1d.label": "1д",
  "period.1d.title": "С начала текущего дня (00:00 UTC)",
  "period.3d.label": "3д",
  "period.3d.title": "За 3 дня, включая сегодня (00:00 UTC)",
  "period.7d.label": "7д",
  "period.7d.title": "За 7 дней, включая сегодня (00:00 UTC)",
  "period.30d.label": "30д",
  "period.30d.title": "За 30 дней, включая сегодня (00:00 UTC)",
  "period.90d.label": "90д",
  "period.90d.title": "За 90 дней, включая сегодня (00:00 UTC)",
  "period.all.label": "Всё время",
  "period.all.title": "За всё время",
  "period.1d.caption": "за 1 день",
  "period.3d.caption": "за 3 дня",
  "period.7d.caption": "за 7 дней",
  "period.30d.caption": "за 30 дней",
  "period.90d.caption": "за 90 дней",
  "period.1d.section": "За 1 день",
  "period.3d.section": "За 3 дня",
  "period.7d.section": "За 7 дней",
  "period.30d.section": "За 30 дней",
  "period.90d.section": "За 90 дней",
  "period.all.section": "За всё время",

  "stats.eyebrow": "Система / статистика",
  "stats.title": "Статистика",
  "stats.requestsPeriod": "Запросов {period}",
  "stats.requestsTotal": "Запросов всего",
  "stats.tokensPeriod": "Токенов {period}",
  "stats.tokensTotal": "Токенов потрачено",
  "stats.proxiesRotation": "Прокси в ротации",
  "stats.loadFailed": "Не удалось загрузить данные",
  "stats.recentTitle": "Последние запросы",
  "stats.recentNote": "Журнал активности",
  "stats.time": "Время",
  "stats.key": "Key",
  "stats.model": "Модель",
  "stats.status": "Статус",
  "stats.latency": "Задержка",
  "stats.tokens": "Токены",
  "stats.proxy": "Прокси",
  "stats.error": "Ошибка",
  "stats.latencyValue": "{value} мс",
  "stats.empty": "Пусто",

  "tokenStats.title": "Токены по дням",
  "tokenStats.note": "Дни — UTC",
  "tokenStats.other": "Другие",
  "tokenStats.loadFailed": "Не удалось загрузить статистику",
  "tokenStats.loading": "Загрузка…",

  "proxyStats.title": "Прокси",
  "proxyStats.allTime": "За всё время",
  "proxyStats.proxy": "Прокси",
  "proxyStats.inPoolHeader": "В пуле",
  "proxyStats.tokensPeriod": "Токенов за период",
  "proxyStats.requests": "Запросов",
  "proxyStats.errors": "Ошибок",
  "proxyStats.inPool": "В пуле",
  "proxyStats.outOfPool": "Вне пула",
  "proxyStats.empty": "Прокси нет",

  "keys.eyebrow": "Доступ / управление",
  "keys.title": "Клиентские ключи",
  "keys.addTitle": "Добавить клиентский ключ",
  "keys.addNote": "Новый доступ",
  "keys.keyId": "Key ID",
  "keys.keyIdPlaceholder": "Генерируется автоматически",
  "keys.noLimit": "Без лимита",
  "keys.modelsLabel": "Доступные модели",
  "keys.modelsPlaceholder": "Через запятую; пусто — все модели",
  "keys.creating": "Создание…",
  "keys.create": "＋   Создать ключ",
  "keys.invalidLimit": "Некорректный {field}: {value}",
  "keys.loadFailed": "Не удалось загрузить данные",
  "keys.listTitle": "Клиентские ключи",
  "keys.listNote": "Управление доступом",
  "keys.rpm": "RPM",
  "keys.tpm": "TPM",
  "keys.modelsColumn": "Модели",
  "keys.status": "Статус",
  "keys.actions": "Действия",
  "keys.reissue": "Перевыпустить",
  "keys.revoke": "Отозвать",
  "keys.empty": "Ключей нет — добавьте выше",
  "keys.statsTitle": "Статистика по ключам",
  "keys.requests": "Запросы",
  "keys.errors": "Ошибки",
  "keys.tokens": "Токены",
  "keys.noRequests": "Запросов ещё не было",
  "keys.createdTitle": "Ключ создан",
  "keys.reissuedTitle": "Ключ перевыпущен",
  "keys.reissueTitle": "Перевыпустить ключ {keyId}?",
  "keys.reissuing": "Перевыпуск…",
  "keys.reissueWarning": "Старый ключ сразу перестанет работать.",
  "keys.deleteTitle": "Удалить ключ {keyId}?",
  "keys.deleteWarning": "Ключ будет удалён без возможности восстановления. Статистика по нему останется в журнале.",

  "keyState.active": "активен",
  "keyState.revoked": "отозван",
  "keyState.deleted": "удалён",

  "secret.lead": "Скопируйте ключ доступа сейчас — после закрытия окна он больше не будет показан.",
  "secret.keyId": "Key ID:",
  "secret.aria": "Ключ доступа",
  "secret.warning": "Восстановить ключ нельзя. Если он потеряется, останется только перевыпустить его.",
  "secret.copy": "Скопировать",
  "secret.copied": "Скопировано",
  "secret.done": "Готово",

  "proxies.eyebrow": "px6.net / управление",
  "proxies.title": "Прокси",
  "proxies.refreshTitle": "Запросить свежий список из px6.net и сбросить кэш",
  "proxies.refreshing": "Обновляю…",
  "proxies.refresh": "Обновить список",
  "proxies.loadFailed": "Не удалось загрузить прокси",
  "proxies.accountError": "Баланс px6.net: {message}",
  "proxies.accountLoadFailed": "не удалось загрузить",
  "proxies.balance": "Баланс",
  "proxies.enabled": "Прокси включено",
  "proxies.ready": "Готовы к ротации",
  "proxies.buyTitle": "Купить прокси",
  "proxies.version": "Версия",
  "proxies.version3": "общий IPv4",
  "proxies.version4": "IPv4",
  "proxies.version6": "IPv6",
  "proxies.country": "Страна",
  "proxies.count": "Количество",
  "proxies.period": "Период",
  "proxies.loading": "Загрузка…",
  "proxies.countriesError": "Не удалось получить список стран: {message}",
  "proxies.priceError": "Не удалось рассчитать стоимость: {message}",
  "proxies.invalidCount": "Укажите количество от 1 до 1000",
  "proxies.summary": "{count} × {version}, {country}, {days}",
  "proxies.buy": "Купить",
  "proxies.buying": "Покупаю…",
  "proxies.listTitle": "Прокси px6.net",
  "proxies.listNote": "{enabled} из {total} включено",
  "proxies.proxy": "Прокси",
  "proxies.countryColumn": "Страна",
  "proxies.versionColumn": "Версия",
  "proxies.validUntil": "Действует до",
  "proxies.status": "Статус",
  "proxies.tokens": "Токены",
  "proxies.actions": "Действия",
  "proxies.empty": "Прокси не найдены. Нажмите «Обновить список» или купите прокси.",
  "proxies.confirmTitle": "Купить прокси?",
  "proxies.confirmProxies": "Прокси",
  "proxies.confirmPeriod": "Период",
  "proxies.confirmPrice": "Стоимость",
  "proxies.confirmBalanceAfter": "Баланс после",
  "proxies.toggleOffTitle": "Исключить прокси из пула шлюза",
  "proxies.toggleOnTitle": "Разрешить использование этого прокси шлюзом",
  "proxies.disable": "Выключить",
  "proxies.enable": "Включить",

  "proxyState.banned": "бан",
  "proxyState.dead": "мёртв",
  "proxyState.disabled": "выключен",
  "proxyState.notInPool": "нет в пуле",
  "proxyState.bannedFor": "бан {duration}",
  "proxyState.quota": "лимит {duration}",
  "proxyState.cooldown": "cooldown {seconds}s",
  "proxyState.rpmCapped": "RPM-кап",
  "proxyState.ok": "ok",

  "tokenStats.chartNoData": "Нет данных за выбранный период",
  "tokenStats.chartLegend": "Легенда: {labels}",
  "tokenStats.chartAria": "Токены по дням (UTC)",
  "tokenStats.chartSpend": "Расход: {tokens} токенов",

  "duration.lessThanMinute": "<1м",
  "duration.hours": "{hours}ч {minutes}м",
  "duration.minutes": "{minutes}м",
  "days.one": "{count} день",
  "days.few": "{count} дня",
  "days.many": "{count} дней",

  "api.requestError": "Ошибка запроса ({status})",
  "api.networkError": "Сервер недоступен. Проверьте соединение и повторите попытку.",
  "api.invalidResponse": "Сервер вернул некорректный ответ",
};

const dictionaries: Record<Language, Record<TranslationKey, string>> = { en, ru };

type Params = Record<string, string | number>;

export function localeFor(language: Language): string {
  return language === "ru" ? "ru-RU" : "en-US";
}

export function isLanguage(value: string | null): value is Language {
  return value === "en" || value === "ru";
}

export function getLanguage(): Language {
  if (typeof window === "undefined") return DEFAULT_LANGUAGE;
  try {
    const stored = window.localStorage.getItem(LANGUAGE_STORAGE_KEY);
    return isLanguage(stored) ? stored : DEFAULT_LANGUAGE;
  } catch {
    return DEFAULT_LANGUAGE;
  }
}

export function translate(
  language: Language,
  key: TranslationKey,
  params?: Params,
): string {
  const template = dictionaries[language][key] ?? en[key];
  if (!params) return template;
  return template.replace(/\{(\w+)\}/g, (_, name: string) =>
    name in params ? String(params[name]) : `{${name}}`,
  );
}

export function formatDays(count: number, language: Language): string {
  const category = new Intl.PluralRules(localeFor(language)).select(count);
  const key: TranslationKey =
    category === "one" ? "days.one" : category === "few" ? "days.few" : "days.many";
  return translate(language, key, { count });
}

export function periodLabel(period: string, language: Language): string {
  return translate(language, `period.${period}.label` as TranslationKey);
}

export function periodTitle(period: string, language: Language): string {
  return translate(language, `period.${period}.title` as TranslationKey);
}

export function periodCaption(period: string, language: Language): string | null {
  if (period === "all") return null;
  return translate(language, `period.${period}.caption` as TranslationKey);
}

export function periodSection(period: string, language: Language): string {
  return translate(language, `period.${period}.section` as TranslationKey);
}

const API_MESSAGE_RULES: Array<{
  pattern: RegExp;
  replace: (...matches: string[]) => string;
}> = [
  { pattern: /^Список прокси обновлён$/, replace: () => "Proxy list refreshed" },
  { pattern: /^Прокси включён$/, replace: () => "Proxy enabled" },
  { pattern: /^Прокси отключён$/, replace: () => "Proxy disabled" },
  { pattern: /^Прокси не найден$/, replace: () => "Proxy not found" },
  { pattern: /^Не удалось обновить список прокси$/, replace: () => "Could not refresh the proxy list" },
  { pattern: /^Ключ не найден$/, replace: () => "Key not found" },
  { pattern: /^Такой ключ уже есть$/, replace: () => "Such a key already exists" },
  { pattern: /^Ключ не указан$/, replace: () => "No key given" },
  { pattern: /^Не удалось сгенерировать уникальный ключ$/, replace: () => "Could not generate a unique key" },
  { pattern: /^Ключ (.+) добавлен$/, replace: (id) => `Key ${id} added` },
  { pattern: /^Ключ добавлен$/, replace: () => "Key added" },
  { pattern: /^Ключ перевыпущен$/, replace: () => "Key reissued" },
  { pattern: /^Ключ включён$/, replace: () => "Key enabled" },
  { pattern: /^Ключ отозван$/, replace: () => "Key revoked" },
  { pattern: /^Ключ удалён$/, replace: () => "Key deleted" },
  { pattern: /^Не удалось добавить ключ$/, replace: () => "Could not add key" },
  { pattern: /^Не удалось перевыпустить ключ$/, replace: () => "Could not reissue key" },
  { pattern: /^Шлюз не вернул ключ доступа$/, replace: () => "Gateway did not return an access key" },
  { pattern: /^Прокси куплены, список обновлён$/, replace: () => "Proxies purchased and list refreshed" },
  { pattern: /^Прокси куплены, но список не обновился$/, replace: () => "Proxies purchased; list refresh failed" },
  {
    pattern: /^Куплено (\d+) прокси\. Включите их в списке\.$/,
    replace: (count) =>
      `${proxyWord(Number(count))} purchased. Enable ${
        Number(count) === 1 ? "it" : "them"
      } in the list.`,
  },
  {
    pattern: /^Куплено (\d+) прокси, но список не обновился\. Нажмите «Обновить список»\.$/,
    replace: (count) =>
      `${proxyWord(Number(count))} purchased, but the list was not refreshed. Click “Refresh list”.`,
  },
  { pattern: /^Количество должно быть от 1 до 1000$/, replace: () => "Quantity must be between 1 and 1000" },
  { pattern: /^Период должен быть от 1 до 365 дней$/, replace: () => "Period must be between 1 and 365 days" },
  { pattern: /^Страна должна быть кодом ISO2$/, replace: () => "Country must be an ISO2 code" },
  { pattern: /^Комментарий не длиннее 50 символов$/, replace: () => "Comment must be at most 50 characters" },
  { pattern: /^Некорректный числовой параметр покупки$/, replace: () => "Invalid numeric purchase parameter" },
  { pattern: /^Поддерживаются версии 3, 4 и 6$/, replace: () => "Supported versions: 3, 4, and 6" },
  { pattern: /^Сервис px6 вернул некорректный ответ$/, replace: () => "The px6 service returned an invalid response" },
  { pattern: /^Неизвестная ошибка px6\.net$/, replace: () => "Unknown px6.net error" },
  { pattern: /^Неверный API-ключ px6\.net$/, replace: () => "Invalid px6.net API key" },
  { pattern: /^Доступ к API px6\.net ограничен по IP$/, replace: () => "px6.net API access is restricted by IP" },
  { pattern: /^Неверный метод API px6\.net$/, replace: () => "Invalid px6.net API method" },
  { pattern: /^Некорректное количество прокси$/, replace: () => "Invalid proxy count" },
  { pattern: /^Некорректный период прокси$/, replace: () => "Invalid proxy period" },
  { pattern: /^Некорректный код страны$/, replace: () => "Invalid country code" },
  { pattern: /^Некорректные идентификаторы прокси$/, replace: () => "Invalid proxy IDs" },
  { pattern: /^Некорректная версия прокси$/, replace: () => "Invalid proxy version" },
  { pattern: /^Некорректный комментарий$/, replace: () => "Invalid proxy comment" },
  { pattern: /^Некорректный тип прокси$/, replace: () => "Invalid proxy type" },
  { pattern: /^Некорректный порт прокси$/, replace: () => "Invalid proxy port" },
  { pattern: /^Некорректная строка прокси$/, replace: () => "Invalid proxy string" },
  { pattern: /^На px6\.net недостаточно свободных прокси$/, replace: () => "Not enough proxies available on px6.net" },
  { pattern: /^Недостаточно средств на счёте px6\.net$/, replace: () => "Insufficient px6.net balance" },
  { pattern: /^Прокси не найден на px6\.net$/, replace: () => "Proxy not found on px6.net" },
  { pattern: /^Некорректная цена$/, replace: () => "Invalid price" },
  { pattern: /^Запрос к px6\.net не удался$/, replace: () => "Request to px6.net failed" },
  { pattern: /^Превышен лимит запросов к px6\.net$/, replace: () => "px6.net rate limit exceeded" },
  { pattern: /^Сервис px6 недоступен$/, replace: () => "px6 service is unavailable" },
  { pattern: /^Некорректный флаг автопродления$/, replace: () => "auto_prolong must be a boolean" },
  { pattern: /^Некорректный (RPM|TPM): (.+)$/, replace: (field, value) => `Invalid ${field}: ${value}` },
  { pattern: /^(RPM|TPM) не может быть отрицательным$/, replace: (field) => `${field} cannot be negative` },
  { pattern: /^Требуется вход в панель$/, replace: () => "Sign-in required" },
  { pattern: /^Неверный пароль$/, replace: () => "Incorrect password" },
  { pattern: /^Хранилище сессий недоступно$/, replace: () => "Session storage is unavailable" },
  {
    pattern: /^Слишком много неудачных попыток входа; попробуйте позже$/,
    replace: () => "Too many failed sign-in attempts; try again later",
  },
  { pattern: /^Неверный CSRF-токен$/, replace: () => "Invalid CSRF token" },
  {
    pattern: /^Админка выключена \(задайте ADMIN_PASSWORD\)$/,
    replace: () => "Admin panel is disabled (set ADMIN_PASSWORD)",
  },
  { pattern: /^Некорректные данные$/, replace: () => "Invalid data" },
  { pattern: /^Некорректный JSON$/, replace: () => "Invalid JSON" },
  { pattern: /^JSON должен быть объектом$/, replace: () => "JSON must be an object" },
  { pattern: /^Тело запроса слишком большое$/, replace: () => "Request body is too large" },
];

function proxyWord(count: number): string {
  return `${count} ${count === 1 ? "proxy" : "proxies"}`;
}

export function localizeApiMessage(
  message: string,
  language: Language = getLanguage(),
): string {
  if (!message || language !== "en") return message;
  for (const rule of API_MESSAGE_RULES) {
    const match = message.match(rule.pattern);
    if (match) return rule.replace(...match.slice(1));
  }
  return message;
}

interface I18nContextValue {
  language: Language;
  setLanguage: (language: Language) => void;
  t: (key: TranslationKey, params?: Params) => string;
}

const I18nContext = createContext<I18nContextValue | null>(null);

export function I18nProvider({ children }: { children: ReactNode }) {
  const [language, setLanguageState] = useState<Language>(getLanguage);

  useEffect(() => {
    document.documentElement.lang = language;
  }, [language]);

  useEffect(() => {
    const onStorage = (event: StorageEvent) => {
      if (event.key === LANGUAGE_STORAGE_KEY && isLanguage(event.newValue)) {
        setLanguageState(event.newValue);
      }
    };
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, []);

  const setLanguage = useCallback((next: Language) => {
    setLanguageState(next);
    try {
      window.localStorage.setItem(LANGUAGE_STORAGE_KEY, next);
    } catch {
      // Storage can be unavailable (private mode / disabled); keep it in memory.
    }
  }, []);

  const t = useCallback(
    (key: TranslationKey, params?: Params) => translate(language, key, params),
    [language],
  );

  const value = useMemo<I18nContextValue>(
    () => ({ language, setLanguage, t }),
    [language, setLanguage, t],
  );

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

export function useI18n(): I18nContextValue {
  const value = useContext(I18nContext);
  if (!value) throw new Error("useI18n must be used inside I18nProvider");
  return value;
}
