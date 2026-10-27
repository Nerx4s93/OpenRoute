import { useMemo, useState } from "react";
import type { ProxyRow } from "../api/types";
import { EmptyRow, ErrorBanner, LoadingRows } from "../components/Feedback";
import { PurchaseProxyDialog } from "../components/PurchaseProxyDialog";
import { Section } from "../components/Section";
import { StatCard } from "../components/StatCard";
import { StatusBadge } from "../components/StatusBadge";
import { useRefreshProxies, useSetProxy } from "../hooks/useAdminMutations";
import { useProxiesLive } from "../hooks/useLiveSnapshot";
import { usePeriod } from "../hooks/usePeriod";
import { usePx6Account } from "../hooks/usePx6";
import { useI18n } from "../i18n";
import { formatMoney, formatNumber } from "../lib/format";
import { countryLabel, versionLabel } from "../lib/px6";
import { proxyState } from "../lib/proxyState";

function proxyDate(value: string): string {
  if (!value) return "—";
  return value;
}

export function ProxiesPage() {
  const { language, t } = useI18n();
  const [period] = usePeriod();
  const query = useProxiesLive(period);
  const refreshProxies = useRefreshProxies();
  const accountQuery = usePx6Account();
  const [buyOpen, setBuyOpen] = useState(false);

  const account = accountQuery.data;
  const summary = query.data?.summary;
  const rows = useMemo(() => query.data?.proxies ?? [], [query.data?.proxies]);
  const total = summary?.proxies_total ?? rows.length;
  const enabledCount =
    summary?.proxies_enabled ?? rows.filter((row) => row.enabled).length;

  return (
    <>
      <div className="hero page-enter">
        <div className="hero-content">
          <div className="eyebrow">{t("proxies.eyebrow")}</div>
          <h1>{t("proxies.title")}</h1>
        </div>
        <div className="hero-actions">
          <button
            type="button"
            className="hero-refresh hero-buy"
            onClick={() => setBuyOpen(true)}
          >
            <span aria-hidden="true">＋</span>
            {t("proxies.buyTitle")}
          </button>
          <button
            type="button"
            className="hero-refresh"
            disabled={refreshProxies.isPending}
            title={t("proxies.refreshTitle")}
            aria-busy={refreshProxies.isPending}
            onClick={() => refreshProxies.mutate()}
          >
            <span aria-hidden="true">↻</span>
            {refreshProxies.isPending ? t("proxies.refreshing") : t("proxies.refresh")}
          </button>
        </div>
      </div>

      {query.isError ? (
        <ErrorBanner
          message={query.error?.message ?? t("proxies.loadFailed")}
          onRetry={() => void query.refetch()}
        />
      ) : null}
      {accountQuery.isError ? (
        <ErrorBanner
          message={t("proxies.accountError", {
            message: accountQuery.error?.message ?? t("proxies.accountLoadFailed"),
          })}
          onRetry={() => void accountQuery.refetch()}
        />
      ) : null}

      <div className="cards page-enter" id="cards">
        <StatCard
          glyph="₽"
          value={
            account
              ? formatMoney(account.balance, account.currency, language)
              : accountQuery.isLoading
                ? "…"
                : "—"
          }
          label={t("proxies.balance")}
        />
        <StatCard
          glyph="⇄"
          value={`${enabledCount}/${total}`}
          label={t("proxies.enabled")}
        />
        <StatCard
          glyph="◎"
          value={formatNumber(summary?.proxies_ready ?? 0, language)}
          label={t("proxies.ready")}
        />
      </div>

      <Section
        id="proxies-title"
        icon="⇄"
        title={t("proxies.listTitle")}
        note={t("proxies.listNote", {
          enabled: enabledCount,
          total,
        })}
      >
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>{t("proxies.proxy")}</th>
                <th>{t("proxies.countryColumn")}</th>
                <th>{t("proxies.versionColumn")}</th>
                <th>{t("proxies.validUntil")}</th>
                <th>{t("proxies.status")}</th>
                <th>{t("proxies.tokens")}</th>
                <th>{t("proxies.actions")}</th>
              </tr>
            </thead>
            <tbody>
              {query.isLoading ? (
                <LoadingRows colSpan={7} />
              ) : rows.length ? (
                rows.map((row) => <ProxyTableRow key={row.id ?? row.display} row={row} />)
              ) : (
                <EmptyRow colSpan={7}>{t("proxies.empty")}</EmptyRow>
              )}
            </tbody>
          </table>
        </div>
      </Section>

      <PurchaseProxyDialog open={buyOpen} onClose={() => setBuyOpen(false)} />
    </>
  );
}

function ProxyTableRow({ row }: { row: ProxyRow }) {
  const { language, t } = useI18n();
  const setProxy = useSetProxy();
  const pending = setProxy.isPending && setProxy.variables?.proxyId === row.id;
  const state = proxyState(row, language);
  const meta = row.px6;

  return (
    <tr className={row.enabled ? undefined : "dead"}>
      <td className="mono">{row.display}</td>
      <td>{countryLabel(meta?.country ?? "", language)}</td>
      <td>{versionLabel(meta?.version ?? "", language)}</td>
      <td className="mono muted">{proxyDate(meta?.date_end ?? "")}</td>
      <td><StatusBadge label={state.label} tone={state.tone} /></td>
      <td>{formatNumber(row.usage.tokens, language)}</td>
      <td className="actions">
        <button
          type="button"
          className={row.enabled ? "danger" : "acc"}
          disabled={!row.id || pending}
          title={
            row.enabled
              ? t("proxies.toggleOffTitle")
              : t("proxies.toggleOnTitle")
          }
          onClick={() => {
            if (row.id) setProxy.mutate({ proxyId: row.id, enabled: !row.enabled });
          }}
        >
          {pending ? "…" : row.enabled ? t("proxies.disable") : t("proxies.enable")}
        </button>
      </td>
    </tr>
  );
}
