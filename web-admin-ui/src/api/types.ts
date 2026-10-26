export type Period = "1d" | "3d" | "7d" | "30d" | "90d" | "all";

export const PERIODS: ReadonlyArray<{ value: Period; label: string; title: string }> = [
  { value: "1d", label: "1д", title: "С начала текущего дня (00:00 UTC)" },
  { value: "3d", label: "3д", title: "За 3 дня, включая сегодня (00:00 UTC)" },
  { value: "7d", label: "7д", title: "За 7 дней, включая сегодня (00:00 UTC)" },
  { value: "30d", label: "30д", title: "За 30 дней, включая сегодня (00:00 UTC)" },
  { value: "90d", label: "90д", title: "За 90 дней, включая сегодня (00:00 UTC)" },
  { value: "all", label: "Всё время", title: "За всё время" },
];

export const PERIOD_CAPTIONS: Record<Period, string | null> = {
  "1d": "за 1 день",
  "3d": "за 3 дня",
  "7d": "за 7 дней",
  "30d": "за 30 дней",
  "90d": "за 90 дней",
  all: null,
};

export const PERIOD_SECTIONS: Record<Period, string> = {
  "1d": "За 1 день",
  "3d": "За 3 дня",
  "7d": "За 7 дней",
  "30d": "За 30 дней",
  "90d": "За 90 дней",
  all: "За всё время",
};

export function isPeriod(value: string | null): value is Period {
  return value === "1d" || value === "3d" || value === "7d"
    || value === "30d" || value === "90d" || value === "all";
}

export interface Usage {
  requests: number;
  errors: number;
  tokens: number;
}

export interface ProxiesTotals {
  tokens_total: number;
}

export interface Px6ProxyMeta {
  id: string;
  version: string;
  country: string;
  date_end: string;
  descr: string;
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
  px6?: Px6ProxyMeta;
  usage: Usage;
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
    proxies_enabled?: number;
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

export interface Px6Account {
  user_id: string;
  balance: number;
  currency: string;
}

export interface Px6Countries {
  version: number;
  countries: string[];
}

export interface Px6Price {
  user_id: string;
  balance: number;
  currency: string;
  price: number;
  price_single: number;
  period: number;
  count: number;
  version: number;
}

export interface ProxyBuyPayload {
  count: number;
  period: number;
  country: string;
  version: number;
  descr?: string;
}

export interface ProxyBuyResult extends ApiMessage {
  order_id: string;
  count: number;
  price: number;
  price_single: number;
  period: number;
  country: string;
  version: number;
  currency: string;
  balance: number;
  refresh_ok: boolean;
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

export interface StatsTotals {
  tokens_total: number;
  requests_total: number;
  errors_total: number;
}

export interface StatsProxyRow {
  proxy: string;
  in_pool: boolean;
  tokens_total: number;
  requests_total: number;
  errors_total: number;
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
