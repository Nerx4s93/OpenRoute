import { EmptyRow, ErrorBanner, LoadingRows } from "../components/Feedback";
import { PeriodTabs } from "../components/PeriodTabs";
import { ProxyStatsPanel } from "../components/ProxyStatsPanel";
import { Section } from "../components/Section";
import { StatCard } from "../components/StatCard";
import { TokenStatsPanel } from "../components/TokenStatsPanel";
import { useProxiesLive } from "../hooks/useLiveSnapshot";
import { usePeriod } from "../hooks/usePeriod";
import { useStatsPeriod } from "../hooks/useStatsPeriod";
import { periodCaption, periodSection, useI18n } from "../i18n";
import {
  formatNumber,
  formatTimestamp,
  requestTokens,
} from "../lib/format";

export function StatsPage() {
  const [period, setPeriod] = usePeriod();
  const query = useProxiesLive(period);
  const statsQuery = useStatsPeriod(period);
  const { language, t } = useI18n();
  const snapshot = query.data;
  const caption = period === "all" ? null : periodCaption(period, language);
  const requestsLabel = caption
    ? t("stats.requestsPeriod", { period: caption })
    : t("stats.requestsTotal");
  const tokensLabel = caption
    ? t("stats.tokensPeriod", { period: caption })
    : t("stats.tokensTotal");
  const summary = snapshot?.summary;

  return (
    <>
      <div className="hero page-enter">
        <div className="hero-content">
          <div className="eyebrow">{t("stats.eyebrow")}</div>
          <h1>{t("stats.title")}</h1>
        </div>
      </div>

      <PeriodTabs active={period} onChange={setPeriod} />

      {query.isError ? (
        <ErrorBanner
          message={query.error?.message ?? t("stats.loadFailed")}
          onRetry={() => void query.refetch()}
        />
      ) : null}

      <div className="cards page-enter" id="cards">
        <StatCard
          glyph="↗"
          value={formatNumber(summary?.requests ?? 0, language)}
          label={requestsLabel}
        />
        <StatCard
          glyph="◈"
          value={formatNumber(summary?.tokens ?? 0, language)}
          label={tokensLabel}
        />
        <StatCard
          glyph="◎"
          value={`${summary?.proxies_ready ?? 0}/${summary?.proxies_total ?? 0}`}
          label={t("stats.proxiesRotation")}
        />
      </div>

      <TokenStatsPanel query={statsQuery} />

      <ProxyStatsPanel
        rows={statsQuery.data?.proxies ?? []}
        isLoading={statsQuery.isLoading}
        note={periodSection(period, language)}
      />

      <Section
        id="requests-title"
        icon="↗"
        title={t("stats.recentTitle")}
        note={t("stats.recentNote")}
      >
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>{t("stats.time")}</th>
                <th>{t("stats.key")}</th>
                <th>{t("stats.model")}</th>
                <th>{t("stats.status")}</th>
                <th>{t("stats.latency")}</th>
                <th>{t("stats.tokens")}</th>
                <th>{t("stats.proxy")}</th>
                <th>{t("stats.error")}</th>
              </tr>
            </thead>
            <tbody>
              {query.isLoading ? (
                <LoadingRows colSpan={8} />
              ) : snapshot && snapshot.recent.length ? (
                snapshot.recent.map((request, index) => {
                  const tokens = requestTokens(
                    request.prompt_tokens,
                    request.completion_tokens,
                  );
                  const failed = request.status >= 400 || Boolean(request.error);
                  return (
                    <tr key={`${request.ts}-${index}`} className={failed ? "dead" : undefined}>
                      <td className="mono muted">
                        {formatTimestamp(request.ts, language)}
                      </td>
                      <td className="mono">{request.key_id}</td>
                      <td className="mono">{request.model || request.path}</td>
                      <td>{request.status}</td>
                      <td>
                        {t("stats.latencyValue", { value: request.latency_ms })}
                      </td>
                      <td>{tokens ? formatNumber(tokens, language) : "—"}</td>
                      <td className="mono">{request.account || "—"}</td>
                      <td className="muted">{request.error || ""}</td>
                    </tr>
                  );
                })
              ) : (
                <EmptyRow colSpan={8}>{t("stats.empty")}</EmptyRow>
              )}
            </tbody>
          </table>
        </div>
      </Section>
    </>
  );
}
