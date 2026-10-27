import type { UseQueryResult } from "@tanstack/react-query";
import type { ApiError } from "../api/client";
import type { StatsProxyRow, StatsSeries, StatsSnapshot } from "../api/types";
import { useI18n } from "../i18n";
import { compareStatsProxies } from "../lib/stats";
import { ErrorBanner } from "./Feedback";
import { Section } from "./Section";
import { TokenChart, type TokenChartSeries } from "./TokenChart";

const TOP_PROXIES = 8;

function tokensFor(series: StatsSeries | undefined, length: number): number[] {
  return Array.from({ length }, (_, index) => series?.tokens?.[index] ?? 0);
}

function buildChartSeries(
  data: StatsSnapshot | undefined,
  rows: StatsProxyRow[],
  otherLabel: string,
): TokenChartSeries[] {
  if (!data) return [];
  const length = data.dates?.length ?? 0;
  if (!length) return [];

  const chartSeries: TokenChartSeries[] = rows.slice(0, TOP_PROXIES).map((row, index) => ({
    label: row.proxy,
    values: tokensFor(data.series_by_proxy?.[row.proxy], length),
    colorIndex: index,
  }));

  const rest = rows.slice(TOP_PROXIES);
  if (rest.length) {
    const values = Array.from({ length }, (_, index) =>
      rest.reduce(
        (sum, row) => sum + (data.series_by_proxy?.[row.proxy]?.tokens?.[index] ?? 0),
        0,
      ),
    );
    chartSeries.push({ label: otherLabel, values, colorIndex: TOP_PROXIES });
  }

  return chartSeries;
}

export function TokenStatsPanel({
  query,
}: {
  query: UseQueryResult<StatsSnapshot, ApiError>;
}) {
  const { t } = useI18n();
  const data = query.data;
  // Sort defensively; the server uses the same in-pool-first ordering.
  const proxyRows = [...(data?.proxies ?? [])].sort(compareStatsProxies);
  const chartSeries = buildChartSeries(data, proxyRows, t("tokenStats.other"));

  return (
    <Section
      id="chart-title"
      icon="▦"
      title={t("tokenStats.title")}
      note={t("tokenStats.note")}
    >
      <div className="chart-wrap">
        {query.isError ? (
          <ErrorBanner
            message={query.error?.message ?? t("tokenStats.loadFailed")}
            onRetry={() => void query.refetch()}
          />
        ) : query.isLoading ? (
          <div className="chart-empty">{t("tokenStats.loading")}</div>
        ) : (
          <TokenChart dates={data?.dates ?? []} series={chartSeries} />
        )}
      </div>
    </Section>
  );
}
