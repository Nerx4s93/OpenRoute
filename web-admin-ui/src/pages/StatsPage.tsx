import { useState } from "react";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { api, type ApiError } from "../api/client";
import type { StatsRange, StatsSeries, StatsSnapshot } from "../api/types";
import { EmptyRow, ErrorBanner, LoadingRows } from "../components/Feedback";
import { usePublishLiveStatus } from "../components/LiveStatus";
import { RangeTabs } from "../components/RangeTabs";
import { Section } from "../components/Section";
import { StatCard } from "../components/StatCard";
import { StatusBadge } from "../components/StatusBadge";
import { TokenChart, type TokenChartSeries } from "../components/TokenChart";
import { formatLimit, formatNumber, formatTimestamp } from "../lib/format";

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

export function StatsPage() {
  const [range, setRange] = useState<StatsRange>(30);
  const [selected, setSelected] = useState("");
  const query = useQuery<StatsSnapshot, ApiError>({
    queryKey: ["stats", range],
    queryFn: () => api.stats(range),
    refetchInterval: 30_000,
    placeholderData: keepPreviousData,
  });

  usePublishLiveStatus(query.data?.server_time ?? null, !query.isError);

  const data = query.data;
  const totals = data?.totals;
  const proxyRows = data?.proxies ?? [];
  const limitedProxies = proxyRows.filter(
    (row) => typeof row.last_limit_at === "number",
  ).length;
  const sortedProxies = [...proxyRows].sort(
    (a, b) => (b.tokens_total ?? 0) - (a.tokens_total ?? 0),
  );
  const chartSeries = buildChartSeries(data, selected);

  return (
    <>
      <div className="hero page-enter">
        <div className="hero-content">
          <div className="eyebrow">Система / статистика</div>
          <h1>Статистика</h1>
          <p>Расход токенов по прокси и дням. Данные обновляются автоматически.</p>
        </div>
      </div>

      <RangeTabs active={range} onChange={setRange} />

      {query.isError ? (
        <ErrorBanner
          message={query.error?.message ?? "Не удалось загрузить статистику"}
          onRetry={() => void query.refetch()}
        />
      ) : null}

      <div className="cards page-enter">
        <StatCard
          glyph="Σ"
          value={formatNumber(totals?.tokens_total ?? 0)}
          label="Токенов за всё время"
          title="Суммарный расход токенов за всё время"
        />
        <StatCard
          glyph="↺"
          value={formatNumber(totals?.tokens_since_limit ?? 0)}
          label="Токенов после последнего лимита"
          title="С момента последнего 429 FreeUsageLimitError; у прокси без лимита — «—»"
        />
        <StatCard
          glyph="↗"
          value={formatNumber(totals?.requests_total ?? 0)}
          label="Запросов за всё время"
        />
        <StatCard
          glyph="⚑"
          value={`${limitedProxies}/${proxyRows.length}`}
          label="Прокси с лимитом"
          title="Сколько прокси хотя бы раз получали 429 FreeUsageLimitError / всего прокси"
        />
      </div>

      <Section id="chart-title" icon="▦" title="Токены по дням" note="Дни — UTC">
        <div className="chart-wrap">
          <div className="stats-toolbar">
            <label className="stats-select">
              <span>Прокси</span>
              <select
                value={selected}
                onChange={(event) => setSelected(event.target.value)}
              >
                <option value="">Все прокси</option>
                {proxyRows.map((row) => (
                  <option key={row.proxy} value={row.proxy}>{row.proxy}</option>
                ))}
              </select>
            </label>
          </div>
          {query.isLoading ? (
            <div className="chart-empty">Загрузка…</div>
          ) : (
            <TokenChart dates={data?.dates ?? []} series={chartSeries} />
          )}
          <p className="chart-hint">
            После лимита — с момента последнего 429 FreeUsageLimitError; у прокси без лимита — «—»
          </p>
        </div>
      </Section>

      <Section id="stats-title" icon="↗" title="Прокси и лимиты" note="За всё время">
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Прокси</th>
                <th>В пуле</th>
                <th>Всего токенов</th>
                <th>После лимита</th>
                <th>Запросов</th>
                <th>Лимитов</th>
                <th>Последний лимит</th>
              </tr>
            </thead>
            <tbody>
              {query.isLoading ? (
                <LoadingRows colSpan={7} />
              ) : sortedProxies.length ? (
                sortedProxies.map((row) => (
                  <tr key={row.proxy} className={row.in_pool ? undefined : "dead"}>
                    <td className="mono">{row.proxy}</td>
                    <td>
                      {row.in_pool ? (
                        <StatusBadge label="В пуле" tone="ok" />
                      ) : (
                        <StatusBadge label="Вне пула" tone="off" />
                      )}
                    </td>
                    <td>{formatNumber(row.tokens_total)}</td>
                    <td>{formatLimit(row.tokens_since_limit)}</td>
                    <td>{formatNumber(row.requests_total)}</td>
                    <td>{formatNumber(row.limits_total)}</td>
                    <td className="mono muted">
                      {typeof row.last_limit_at === "number"
                        ? formatTimestamp(row.last_limit_at)
                        : "—"}
                    </td>
                  </tr>
                ))
              ) : (
                <EmptyRow colSpan={7}>Прокси нет</EmptyRow>
              )}
            </tbody>
          </table>
        </div>
      </Section>
    </>
  );
}
