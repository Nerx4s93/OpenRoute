import { useEffect, useMemo, useState, type FormEvent } from "react";
import type { ProxyRow } from "../api/types";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { EmptyRow, ErrorBanner, LoadingRows } from "../components/Feedback";
import { Section } from "../components/Section";
import { StatCard } from "../components/StatCard";
import { StatusBadge } from "../components/StatusBadge";
import { useRefreshProxies, useSetProxy } from "../hooks/useAdminMutations";
import { useDebouncedValue } from "../hooks/useDebouncedValue";
import { useProxiesLive } from "../hooks/useLiveSnapshot";
import { usePeriod } from "../hooks/usePeriod";
import { useBuyProxies, usePx6Account, usePx6Countries, usePx6Price } from "../hooks/usePx6";
import {
  formatDays,
  translate,
  useI18n,
  type Language,
  type TranslationKey,
} from "../i18n";
import { formatMoney, formatNumber } from "../lib/format";
import { proxyState } from "../lib/proxyState";

const VERSIONS: ReadonlyArray<{ value: number; labelKey: TranslationKey }> = [
  { value: 3, labelKey: "proxies.version3" },
  { value: 4, labelKey: "proxies.version4" },
  { value: 6, labelKey: "proxies.version6" },
];

const PERIODS = [3, 7, 14, 30, 60, 90] as const;

function versionLabel(value: string | number, language: Language): string {
  if (value === "") return "-";
  const version = String(value);
  const item = VERSIONS.find((entry) => String(entry.value) === version);
  return item ? translate(language, item.labelKey) : version;
}

function countryLabel(code: string, language: Language): string {
  if (!code) return "—";
  try {
    const name = new Intl.DisplayNames([language === "ru" ? "ru" : "en"], {
      type: "region",
    }).of(code.toUpperCase());
    return name ? `${name} (${code.toUpperCase()})` : code.toUpperCase();
  } catch {
    return code.toUpperCase();
  }
}

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
  const buy = useBuyProxies();

  const [version, setVersion] = useState(6);
  const [country, setCountry] = useState("");
  const [countText, setCountText] = useState("1");
  const [buyPeriod, setBuyPeriod] = useState(30);
  const [descr, setDescr] = useState("");
  const [confirmOpen, setConfirmOpen] = useState(false);

  const countriesQuery = usePx6Countries(version);
  const countryList = countriesQuery.data?.countries;

  useEffect(() => {
    if (!countryList?.length) return;
    if (!countryList.includes(country)) {
      setCountry(countryList.includes("ru") ? "ru" : countryList[0]);
    }
  }, [countryList, country]);

  const parsedCount = Number(countText);
  const count = Number.isFinite(parsedCount) && Number.isInteger(parsedCount)
    ? parsedCount
    : 0;
  const countValid = count >= 1 && count <= 1000;
  const debouncedCount = useDebouncedValue(countValid ? count : 0, 500);
  const priceQuery = usePx6Price(version, debouncedCount, buyPeriod);

  const account = accountQuery.data;
  const price = priceQuery.data;
  const priceReady = Boolean(
    price &&
      !priceQuery.isFetching &&
      price.count === count &&
      price.period === buyPeriod &&
      price.version === version,
  );
  const currency = price?.currency ?? account?.currency ?? "RUB";
  const priceText = price
    ? formatMoney(price.price, price.currency || currency, language)
    : "—";
  const priceLoading = countValid && !priceReady && !priceQuery.isError;
  const balance = account?.balance ?? price?.balance;
  const balanceAfter =
    price && priceReady && balance !== undefined
      ? balance - price.price
      : undefined;

  const summary = query.data?.summary;
  const rows = useMemo(() => query.data?.proxies ?? [], [query.data?.proxies]);
  const enabledCount =
    summary?.proxies_enabled ?? rows.filter((row) => row.enabled).length;

  const canBuy = Boolean(
    country &&
      countryList?.includes(country) &&
      countValid &&
      priceReady &&
      !buy.isPending &&
      !countriesQuery.isLoading &&
      !countriesQuery.isError,
  );

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (canBuy) setConfirmOpen(true);
  };

  const confirmBuy = () => {
    buy.mutate(
      {
        count,
        period: buyPeriod,
        country,
        version,
        descr: descr.trim() || undefined,
      },
      { onSuccess: () => setConfirmOpen(false) },
    );
  };

  return (
    <>
      <div className="hero page-enter">
        <div className="hero-content">
          <div className="eyebrow">{t("proxies.eyebrow")}</div>
          <h1>{t("proxies.title")}</h1>
        </div>
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
          hint={account?.user_id ? `ID ${account.user_id}` : undefined}
        />
        <StatCard
          glyph="⇄"
          value={`${enabledCount}/${summary?.proxies_total ?? rows.length}`}
          label={t("proxies.enabled")}
          hint={t("proxies.enabledHint")}
        />
        <StatCard
          glyph="◎"
          value={formatNumber(summary?.proxies_ready ?? 0, language)}
          label={t("proxies.ready")}
        />
      </div>

      <Section
        id="buy-title"
        icon="＋"
        title={t("proxies.buyTitle")}
        note={t("proxies.buyNote")}
      >
        <div className="form-content">
          <form className="add buy-form" onSubmit={submit}>
            <div className="field">
              <label htmlFor="buy-version">{t("proxies.version")}</label>
              <select
                id="buy-version"
                value={version}
                onChange={(event) => setVersion(Number(event.target.value))}
              >
                {VERSIONS.map((item) => (
                  <option key={item.value} value={item.value}>
                    {translate(language, item.labelKey)}
                  </option>
                ))}
              </select>
            </div>

            <div className="field">
              <label htmlFor="buy-country">{t("proxies.country")}</label>
              <select
                id="buy-country"
                value={country}
                disabled={countriesQuery.isLoading || !countryList?.length}
                onChange={(event) => setCountry(event.target.value)}
              >
                {!countryList?.length ? (
                  <option value="">{t("proxies.loading")}</option>
                ) : null}
                {(countryList ?? []).map((code) => (
                  <option key={code} value={code}>
                    {countryLabel(code, language)}
                  </option>
                ))}
              </select>
            </div>

            <div className="field">
              <label htmlFor="buy-count">{t("proxies.count")}</label>
              <input
                id="buy-count"
                type="text"
                inputMode="numeric"
                autoComplete="off"
                value={countText}
                onChange={(event) => setCountText(event.target.value.replace(/\D/g, ""))}
              />
            </div>

            <div className="field">
              <label htmlFor="buy-period">{t("proxies.period")}</label>
              <select
                id="buy-period"
                value={buyPeriod}
                onChange={(event) => setBuyPeriod(Number(event.target.value))}
              >
                {PERIODS.map((days) => (
                  <option key={days} value={days}>
                    {formatDays(days, language)}
                  </option>
                ))}
              </select>
            </div>

            <div className="field buy-comment">
              <label htmlFor="buy-descr">{t("proxies.comment")}</label>
              <input
                id="buy-descr"
                type="text"
                maxLength={50}
                placeholder={t("proxies.commentPlaceholder")}
                value={descr}
                onChange={(event) => setDescr(event.target.value)}
              />
            </div>

            {countriesQuery.isError ? (
              <p className="form-hint buy-error">
                {t("proxies.countriesError", {
                  message: countriesQuery.error?.message ?? "",
                })}
              </p>
            ) : null}
            {priceQuery.isError ? (
              <p className="form-hint buy-error">
                {t("proxies.priceError", {
                  message: priceQuery.error?.message ?? "",
                })}
              </p>
            ) : null}

            <div className="form-footer">
              <div className="buy-summary">
                <span className="muted">
                  {countValid
                    ? t("proxies.summary", {
                        count,
                        version: versionLabel(version, language),
                        country: countryLabel(country, language),
                        days: formatDays(buyPeriod, language),
                      })
                    : t("proxies.invalidCount")}
                </span>
                <strong className="buy-price">
                  {priceLoading ? t("common.calculating") : priceText}
                </strong>
              </div>
              <button type="submit" disabled={!canBuy}>
                {buy.isPending ? t("common.buying") : t("common.buy")}
              </button>
            </div>
          </form>
        </div>
      </Section>

      <Section
        id="proxies-title"
        icon="⇄"
        title={t("proxies.listTitle")}
        note={t("proxies.listNote", {
          enabled: enabledCount,
          total: summary?.proxies_total ?? rows.length,
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
                <th>{t("proxies.commentColumn")}</th>
                <th>{t("proxies.status")}</th>
                <th>{t("proxies.tokens")}</th>
                <th>{t("proxies.actions")}</th>
              </tr>
            </thead>
            <tbody>
              {query.isLoading ? (
                <LoadingRows colSpan={8} />
              ) : rows.length ? (
                rows.map((row) => <ProxyTableRow key={row.id ?? row.display} row={row} />)
              ) : (
                <EmptyRow colSpan={8}>{t("proxies.empty")}</EmptyRow>
              )}
            </tbody>
          </table>
        </div>
      </Section>

      <ConfirmDialog
        open={confirmOpen}
        title={t("proxies.confirmTitle")}
        confirmLabel={t("common.buy")}
        pendingLabel={t("common.buying")}
        tone="accent"
        pending={buy.isPending}
        onCancel={() => !buy.isPending && setConfirmOpen(false)}
        onConfirm={confirmBuy}
      >
        <dl className="confirm-summary">
          <div>
            <dt>{t("proxies.confirmProxies")}</dt>
            <dd>
              {count} × {versionLabel(version, language)},{" "}
              {countryLabel(country, language)}
            </dd>
          </div>
          <div>
            <dt>{t("proxies.confirmPeriod")}</dt>
            <dd>{formatDays(buyPeriod, language)}</dd>
          </div>
          <div>
            <dt>{t("proxies.confirmPrice")}</dt>
            <dd>
              {priceQuery.isFetching ? t("common.calculating") : priceText}
            </dd>
          </div>
          <div>
            <dt>{t("proxies.confirmBalanceAfter")}</dt>
            <dd>
              {balanceAfter !== undefined
                ? formatMoney(balanceAfter, currency, language)
                : "—"}
            </dd>
          </div>
          {descr.trim() ? (
            <div>
              <dt>{t("proxies.confirmComment")}</dt>
              <dd>{descr.trim()}</dd>
            </div>
          ) : null}
        </dl>
        <p className="muted confirm-note">{t("proxies.confirmNote")}</p>
      </ConfirmDialog>
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
      <td className="muted">{meta?.descr || "—"}</td>
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
