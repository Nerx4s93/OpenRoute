import { PERIOD_CAPTIONS, PERIOD_SECTIONS } from "../api/types";
import { EmptyRow, ErrorBanner, LoadingRows } from "../components/Feedback";
import { PeriodTabs } from "../components/PeriodTabs";
import { ProxyStatsPanel } from "../components/ProxyStatsPanel";
import { Section } from "../components/Section";
import { StatCard } from "../components/StatCard";
import { TokenStatsPanel } from "../components/TokenStatsPanel";
import { useProxiesLive } from "../hooks/useLiveSnapshot";
import { usePeriod } from "../hooks/usePeriod";
import { useStatsPeriod } from "../hooks/useStatsPeriod";
import { formatNumber, formatTimestamp, requestTokens } from "../lib/format";

export function StatsPage() {
  const [period, setPeriod] = usePeriod();
  const query = useProxiesLive(period);
  const statsQuery = useStatsPeriod(period);
  const snapshot = query.data;
  const caption = PERIOD_CAPTIONS[period];
  const requestsLabel = caption ? `Запросов ${caption}` : "Запросов всего";
  const tokensLabel = caption ? `Токенов ${caption}` : "Токенов потрачено";
  const summary = snapshot?.summary;

  return (
    <>
      <div className="hero page-enter">
        <div className="hero-content">
          <div className="eyebrow">Система / статистика</div>
          <h1>Статистика</h1>
        </div>
      </div>

      <PeriodTabs active={period} onChange={setPeriod} />

      {query.isError ? (
        <ErrorBanner
          message={query.error?.message ?? "Не удалось загрузить данные"}
          onRetry={() => void query.refetch()}
        />
      ) : null}

      <div className="cards page-enter" id="cards">
        <StatCard
          glyph="↗"
          value={formatNumber(summary?.requests ?? 0)}
          label={requestsLabel}
        />
        <StatCard
          glyph="◈"
          value={formatNumber(summary?.tokens ?? 0)}
          label={tokensLabel}
        />
        <StatCard
          glyph="◎"
          value={`${summary?.proxies_ready ?? 0}/${summary?.proxies_total ?? 0}`}
          label="Прокси в ротации"
        />
      </div>

      <TokenStatsPanel query={statsQuery} />

      <ProxyStatsPanel
        rows={statsQuery.data?.proxies ?? []}
        isLoading={statsQuery.isLoading}
        note={PERIOD_SECTIONS[period]}
      />

      <Section id="requests-title" icon="↗" title="Последние запросы" note="Журнал активности">
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Время</th>
                <th>Key</th>
                <th>Модель</th>
                <th>Статус</th>
                <th>Задержка</th>
                <th>Токены</th>
                <th>Прокси</th>
                <th>Ошибка</th>
              </tr>
            </thead>
            <tbody>
              {query.isLoading ? (
                <LoadingRows colSpan={8} />
              ) : snapshot && snapshot.recent.length ? (
                snapshot.recent.map((request, index) => {
                  const tokens = requestTokens(request.prompt_tokens, request.completion_tokens);
                  const failed = request.status >= 400 || Boolean(request.error);
                  return (
                    <tr key={`${request.ts}-${index}`} className={failed ? "dead" : undefined}>
                      <td className="mono muted">{formatTimestamp(request.ts)}</td>
                      <td className="mono">{request.key_id}</td>
                      <td className="mono">{request.model || request.path}</td>
                      <td>{request.status}</td>
                      <td>{request.latency_ms} мс</td>
                      <td>{tokens ? formatNumber(tokens) : "—"}</td>
                      <td className="mono">{request.account || "—"}</td>
                      <td className="muted">{request.error || ""}</td>
                    </tr>
                  );
                })
              ) : (
                <EmptyRow colSpan={8}>Пусто</EmptyRow>
              )}
            </tbody>
          </table>
        </div>
      </Section>
    </>
  );
}
