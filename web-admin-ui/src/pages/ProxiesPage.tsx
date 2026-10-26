import { PERIOD_CAPTIONS, PERIOD_SECTIONS } from "../api/types";
import { EmptyRow, ErrorBanner, LoadingRows } from "../components/Feedback";
import { PeriodTabs } from "../components/PeriodTabs";
import { Section } from "../components/Section";
import { StatCard } from "../components/StatCard";
import { StatusBadge } from "../components/StatusBadge";
import { useRefreshProxies, useSetProxy } from "../hooks/useAdminMutations";
import { useProxiesLive } from "../hooks/useLiveSnapshot";
import { usePeriod } from "../hooks/usePeriod";
import {
  formatLimit,
  formatNumber,
  formatTimestamp,
  requestTokens,
} from "../lib/format";
import { proxyState } from "../lib/proxyState";

export function ProxiesPage() {
  const [period, setPeriod] = usePeriod();
  const query = useProxiesLive(period);
  const setProxy = useSetProxy();
  const refreshProxies = useRefreshProxies();
  const snapshot = query.data;
  const caption = PERIOD_CAPTIONS[period];
  const requestsLabel = caption ? `Запросов ${caption}` : "Запросов всего";
  const tokensLabel = caption ? `Токенов ${caption}` : "Токенов потрачено";
  const summary = snapshot?.summary;
  const totals = snapshot?.totals;

  return (
    <>
      <div className="hero page-enter">
        <div className="hero-content">
          <div className="eyebrow">Система / обзор</div>
          <h1>Пул прокси</h1>
        </div>
        <button
          type="button"
          className="hero-refresh"
          disabled={refreshProxies.isPending}
          title="Запросить свежий список из px6 и сбросить кэш"
          aria-busy={refreshProxies.isPending}
          onClick={() => refreshProxies.mutate()}
        >
          <span aria-hidden="true">↻</span>
          {refreshProxies.isPending ? "Обновляю…" : "Обновить список"}
        </button>
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
          glyph="Σ"
          value={formatNumber(totals?.tokens_total ?? 0)}
          label="Токенов за всё время"
          title="Суммарный расход токенов по всем прокси за всё время"
        />
        <StatCard
          glyph="↺"
          value={formatNumber(totals?.tokens_since_limit ?? 0)}
          label="Токенов после последнего лимита"
          title="Токены, потраченные после последнего 429 FreeUsageLimitError"
        />
        <StatCard
          glyph="◎"
          value={`${summary?.proxies_ready ?? 0}/${summary?.proxies_total ?? 0}`}
          label="Прокси в ротации"
        />
      </div>

      <Section
        id="pool-title"
        icon="◎"
        title="Пул прокси"
        note={`Расходы: ${PERIOD_SECTIONS[period].toLowerCase()}`}
      >
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Прокси</th>
                <th>Статус</th>
                <th>Запросы</th>
                <th>Ошибки</th>
                <th>После лимита</th>
                <th>Последний лимит</th>
                <th>Последняя ошибка</th>
                <th>Действия</th>
              </tr>
            </thead>
            <tbody>
              {query.isLoading ? (
                <LoadingRows colSpan={8} />
              ) : snapshot && snapshot.proxies.length ? (
                snapshot.proxies.map((proxy) => {
                  const state = proxyState(proxy);
                  const pending =
                    setProxy.isPending && setProxy.variables?.proxyId === proxy.id;
                  return (
                    <tr key={proxy.id ?? proxy.display} className={state.dead ? "dead" : undefined}>
                      <td className="mono">{proxy.display}</td>
                      <td><StatusBadge label={state.label} tone={state.tone} /></td>
                      <td>{formatNumber(proxy.usage.requests)}</td>
                      <td>{formatNumber(proxy.usage.errors)}</td>
                      <td>{formatLimit(proxy.lifetime?.tokens_since_limit ?? null)}</td>
                      <td className="mono muted">
                        {typeof proxy.lifetime?.last_limit_at === "number"
                          ? formatTimestamp(proxy.lifetime.last_limit_at)
                          : "—"}
                      </td>
                      <td className="muted">{proxy.last_error ?? ""}</td>
                      <td className="actions">
                        <button
                          type="button"
                          className={proxy.manual ? "acc" : undefined}
                          disabled={!proxy.id || pending}
                          title={proxy.id ? undefined : "Прокси недоступен для управления"}
                          onClick={() => {
                            if (!proxy.id) return;
                            setProxy.mutate({ proxyId: proxy.id, enabled: proxy.manual });
                          }}
                        >
                          {pending ? "…" : proxy.manual ? "Включить" : "Отключить"}
                        </button>
                      </td>
                    </tr>
                  );
                })
              ) : (
                <EmptyRow colSpan={8}>Прокси нет</EmptyRow>
              )}
            </tbody>
          </table>
        </div>
      </Section>

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
