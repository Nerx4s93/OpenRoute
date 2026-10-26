import type { StatsProxyRow } from "../api/types";

/** In-pool proxies first, then archived; each group by token spend desc. */
export function compareStatsProxies(a: StatsProxyRow, b: StatsProxyRow): number {
  if (a.in_pool !== b.in_pool) return a.in_pool ? -1 : 1;
  return b.tokens_total - a.tokens_total || a.proxy.localeCompare(b.proxy);
}
