import type { StatsProxyRow } from "../api/types";
import { EmptyRow, LoadingRows } from "./Feedback";
import { Section } from "./Section";
import { StatusBadge } from "./StatusBadge";
import { formatNumber } from "../lib/format";
import { compareStatsProxies } from "../lib/stats";

interface ProxyStatsPanelProps {
  rows: StatsProxyRow[];
  isLoading?: boolean;
  id?: string;
  note?: string;
}

export function ProxyStatsPanel({
  rows,
  isLoading = false,
  id = "proxy-stats-title",
  note = "За всё время",
}: ProxyStatsPanelProps) {
  const sortedProxies = [...rows].sort(compareStatsProxies);

  return (
    <Section id={id} icon="↗" title="Прокси" note={note}>
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th>Прокси</th>
              <th>В пуле</th>
              <th>Токенов за период</th>
              <th>Запросов</th>
              <th>Ошибок</th>
            </tr>
          </thead>
          <tbody>
            {isLoading ? (
              <LoadingRows colSpan={5} />
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
                  <td>{formatNumber(row.requests_total)}</td>
                  <td>{formatNumber(row.errors_total)}</td>
                </tr>
              ))
            ) : (
              <EmptyRow colSpan={5}>Прокси нет</EmptyRow>
            )}
          </tbody>
        </table>
      </div>
    </Section>
  );
}
