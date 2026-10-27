export type Period = "1d" | "3d" | "7d" | "30d" | "90d" | "all";

export const PERIODS: readonly Period[] = ["1d", "3d", "7d", "30d", "90d", "all"];

export function isPeriod(value: string | null): value is Period {
  return value === "1d" || value === "3d" || value === "7d"
    || value === "30d" || value === "90d" || value === "all";
}

export interface Usage {
  requests: number;
  errors: number;
  tokens: number;
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
  banned: boolean;
  quota_seconds_left: number;
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
  proxies: ProxyRow[];
  recent: RecentRequest[];
}

export interface KeyRow {
  key_hash: string;
  key_id: string;
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

/** A generated secret is returned only by the add/rotate mutation. */
export interface KeyIssued extends ApiMessage {
  key: string;
  key_id: string;
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
  key_id?: string;
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
