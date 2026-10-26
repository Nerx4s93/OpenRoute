export type Period = "day" | "week" | "month" | "all";

export const PERIODS: ReadonlyArray<{ value: Period; label: string; title: string }> = [
  { value: "day", label: "День", title: "С начала дня (00:00 UTC)" },
  { value: "week", label: "Неделя", title: "С начала недели, понедельник (00:00 UTC)" },
  { value: "month", label: "Месяц", title: "С начала месяца (00:00 UTC)" },
  { value: "all", label: "Всё время", title: "За всё время" },
];

export const PERIOD_CAPTIONS: Record<Period, string | null> = {
  day: "за день",
  week: "за неделю",
  month: "за месяц",
  all: null,
};

export const PERIOD_SECTIONS: Record<Period, string> = {
  day: "За день",
  week: "За неделю",
  month: "За месяц",
  all: "За всё время",
};

export function isPeriod(value: string | null): value is Period {
  return value === "day" || value === "week" || value === "month" || value === "all";
}

export interface Usage {
  requests: number;
  errors: number;
  tokens: number;
}

export interface ProxyLifetime {
  tokens_total: number;
  tokens_since_limit: number | null;
  last_limit_at: number | null;
  limits_total: number;
}

export interface ProxiesTotals {
  tokens_total: number;
  tokens_since_limit: number;
  limits_total: number;
}

export interface ProxyRow {
  id: string | null;
  display: string;
  enabled: boolean;
  manual: boolean;
  disabled: boolean;
  banned: boolean;
  quota_seconds_left: number;
  banned_seconds_left: number;
  cooldown_seconds_left: number;
  rpm_capped: boolean;
  last_error: string | null;
  usage: Usage;
  // Optional because old SSE frames and cached snapshots may not carry it yet.
  lifetime?: ProxyLifetime;
}

export interface RecentRequest {
  ts: number;
  key_id: string;
  method?: string;
  path: string;
  model?: string | null;
  status: number;
  latency_ms: number;
  stream?: boolean;
  account?: string | null;
  prompt_tokens?: number | null;
  completion_tokens?: number | null;
  error?: string | null;
}

export interface ProxiesSnapshot {
  server_time: string;
  summary: {
    requests: number;
    errors: number;
    tokens: number;
    proxies_ready: number;
    proxies_total: number;
  };
  // Optional for old SSE frames and cached snapshots.
  totals?: ProxiesTotals;
  proxies: ProxyRow[];
  recent: RecentRequest[];
}

export interface KeyRow {
  key_hash: string;
  key_id: string;
  name: string;
  admin: boolean;
  rpm: number | null;
  tpm: number | null;
  models: string[] | null;
  enabled: boolean;
}

export type KeyState = "active" | "revoked" | "deleted";

export interface KeyStat {
  key_id: string;
  state: KeyState;
  requests: number;
  errors: number;
  tokens: number;
}

export interface KeysSnapshot {
  server_time: string;
  keys: KeyRow[];
  stats: KeyStat[];
}

export interface SessionInfo {
  authenticated: boolean;
  csrf?: string;
  expires_at?: number;
}

export interface ApiMessage {
  ok: boolean;
  message: string;
}

export interface KeyAddPayload {
  key: string;
  key_id?: string;
  name?: string;
  admin?: boolean;
  rpm?: number | null;
  tpm?: number | null;
  models?: string[] | null;
}

export type StatsRange = 7 | 30 | 90;

export interface StatsTotals {
  tokens_total: number;
  tokens_since_limit: number;
  requests_total: number;
  errors_total: number;
  limits_total: number;
}

export interface StatsProxyRow {
  proxy: string;
  in_pool: boolean;
  enabled: boolean;
  banned: boolean;
  quota_seconds_left: number;
  tokens_total: number;
  tokens_since_limit: number | null;
  requests_total: number;
  errors_total: number;
  last_limit_at: number | null;
  limits_total: number;
}

export interface StatsSeries {
  tokens: number[];
  requests: number[];
  errors: number[];
}

export interface StatsSnapshot {
  server_time: string;
  days: number;
  dates: string[];
  totals: StatsTotals;
  proxies: StatsProxyRow[];
  series_total: StatsSeries;
  series_by_proxy: Record<string, StatsSeries>;
}
