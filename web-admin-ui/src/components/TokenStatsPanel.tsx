import { useState } from "react";
import type { UseQueryResult } from "@tanstack/react-query";
import type { ApiError } from "../api/client";
import type { StatsSeries, StatsSnapshot } from "../api/types";
import { ErrorBanner } from "./Feedback";
import { Section } from "./Section";
import { TokenChart, type TokenChartSeries } from "./TokenChart";

const TOP_PROXIES = 8;

function tokensFor(series: StatsSeries | undefined, length: number): number[] {
  return Array.from({ length }, (_, index) => series?.tokens?.[index] ?? 0);
}

function buildChartSeries(
  data: StatsSnapshot | undefined,
  selected: string,
): TokenChartSeries[] {
  if (!data) return [];
  const length = data.dates?.length ?? 0;
  if (!length) return [];

  if (selected) {
    return [
      {
        label: selected,
        values: tokensFor(data.series_by_proxy?.[selected], length),
        colorIndex: 0,
      },
    ];
  }

  // Sort defensively: the server already returns rows by tokens_total desc.
  const rows = [...(data.proxies ?? [])].sort(
    (a, b) => (b.tokens_total ?? 0) - (a.tokens_total ?? 0),
  );
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
    chartSeries.push({ label: "Другие", values, colorIndex: TOP_PROXIES });
  }

  return chartSeries;
}

export function TokenStatsPanel({
  query,
}: {
  query: UseQueryResult<StatsSnapshot, ApiError>;
}) {
  const [selected, setSelected] = useState("");
  const data = query.data;
  const proxyRows = data?.proxies ?? [];
  const activeProxy = proxyRows.some((row) => row.proxy === selected) ? selected : "";
  const chartSeries = buildChartSeries(data, activeProxy);

  return (
    <Section id="chart-title" icon="▦" title="Токены по дням" note="Дни — UTC">
      <div className="chart-wrap">
        <div className="stats-toolbar">
          <label className="stats-select">
            <span>Прокси</span>
            <select
              value={activeProxy}
              onChange={(event) => setSelected(event.target.value)}
            >
              <option value="">Все прокси</option>
              {proxyRows.map((row) => (
                <option key={row.proxy} value={row.proxy}>{row.proxy}</option>
              ))}
            </select>
          </label>
        </div>
        {query.isError ? (
          <ErrorBanner
            message={query.error?.message ?? "Не удалось загрузить статистику"}
            onRetry={() => void query.refetch()}
          />
        ) : query.isLoading ? (
          <div className="chart-empty">Загрузка…</div>
        ) : (
          <TokenChart dates={data?.dates ?? []} series={chartSeries} />
        )}
      </div>
    </Section>
  );
}
