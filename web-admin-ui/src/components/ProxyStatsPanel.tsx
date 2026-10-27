import type { StatsProxyRow } from "../api/types";
import { useI18n } from "../i18n";
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
  note,
}: ProxyStatsPanelProps) {
  const { language, t } = useI18n();
  const sortedProxies = [...rows].sort(compareStatsProxies);
  const resolvedNote = note ?? t("proxyStats.allTime");

  return (
    <Section id={id} icon="↗" title={t("proxyStats.title")} note={resolvedNote}>
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th>{t("proxyStats.proxy")}</th>
              <th>{t("proxyStats.inPoolHeader")}</th>
              <th>{t("proxyStats.tokensPeriod")}</th>
              <th>{t("proxyStats.requests")}</th>
              <th>{t("proxyStats.errors")}</th>
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
                      <StatusBadge label={t("proxyStats.inPool")} tone="ok" />
                    ) : (
                      <StatusBadge label={t("proxyStats.outOfPool")} tone="off" />
                    )}
                  </td>
                  <td>{formatNumber(row.tokens_total, language)}</td>
                  <td>{formatNumber(row.requests_total, language)}</td>
                  <td>{formatNumber(row.errors_total, language)}</td>
                </tr>
              ))
            ) : (
              <EmptyRow colSpan={5}>{t("proxyStats.empty")}</EmptyRow>
            )}
          </tbody>
        </table>
      </div>
    </Section>
  );
}
