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
import { formatMoney, formatNumber } from "../lib/format";
import { proxyState } from "../lib/proxyState";

const VERSIONS = [
  { value: 3, label: "3 — IPv4 Shared" },
  { value: 4, label: "4 — IPv4" },
  { value: 6, label: "6 — IPv6" },
] as const;

const PERIODS = [3, 7, 14, 30, 60, 90] as const;

function versionLabel(value: string | number): string {
  if (value === "") return "-";
  const version = String(value);
  return VERSIONS.find((item) => String(item.value) === version)?.label ?? version;
}

function countryLabel(code: string): string {
  if (!code) return "—";
  try {
    const name = new Intl.DisplayNames(["ru"], { type: "region" }).of(code.toUpperCase());
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
  const priceText = price ? formatMoney(price.price, price.currency || currency) : "—";
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
          <div className="eyebrow">px6.net / управление</div>
          <h1>Прокси</h1>
        </div>
        <button
          type="button"
          className="hero-refresh"
          disabled={refreshProxies.isPending}
          title="Запросить свежий список из px6.net и сбросить кэш"
          aria-busy={refreshProxies.isPending}
          onClick={() => refreshProxies.mutate()}
        >
          <span aria-hidden="true">↻</span>
          {refreshProxies.isPending ? "Обновляю…" : "Обновить список"}
        </button>
      </div>

      {query.isError ? (
        <ErrorBanner
          message={query.error?.message ?? "Не удалось загрузить прокси"}
          onRetry={() => void query.refetch()}
        />
      ) : null}
      {accountQuery.isError ? (
        <ErrorBanner
          message={`Баланс px6.net: ${accountQuery.error?.message ?? "не удалось загрузить"}`}
          onRetry={() => void accountQuery.refetch()}
        />
      ) : null}

      <div className="cards page-enter" id="cards">
        <StatCard
          glyph="₽"
          value={account ? formatMoney(account.balance, account.currency) : accountQuery.isLoading ? "…" : "—"}
          label="Баланс px6.net"
          hint={account?.user_id ? `ID ${account.user_id}` : undefined}
        />
        <StatCard
          glyph="⇄"
          value={`${enabledCount}/${summary?.proxies_total ?? rows.length}`}
          label="Прокси включено"
          hint="Только включённые попадают в пул шлюза"
        />
        <StatCard
          glyph="◎"
          value={formatNumber(summary?.proxies_ready ?? 0)}
          label="Готовы к ротации"
        />
      </div>

      <Section
        id="buy-title"
        icon="＋"
        title="Купить прокси"
        note="Оплата с баланса px6.net"
      >
        <div className="form-content">
          <form className="add buy-form" onSubmit={submit}>
            <div className="field">
              <label htmlFor="buy-version">Версия</label>
              <select
                id="buy-version"
                value={version}
                onChange={(event) => setVersion(Number(event.target.value))}
              >
                {VERSIONS.map((item) => (
                  <option key={item.value} value={item.value}>{item.label}</option>
                ))}
              </select>
            </div>

            <div className="field">
              <label htmlFor="buy-country">Страна</label>
              <select
                id="buy-country"
                value={country}
                disabled={countriesQuery.isLoading || !countryList?.length}
                onChange={(event) => setCountry(event.target.value)}
              >
                {!countryList?.length ? <option value="">Загрузка…</option> : null}
                {(countryList ?? []).map((code) => (
                  <option key={code} value={code}>{countryLabel(code)}</option>
                ))}
              </select>
            </div>

            <div className="field">
              <label htmlFor="buy-count">Количество</label>
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
              <label htmlFor="buy-period">Период</label>
              <select
                id="buy-period"
                value={buyPeriod}
                onChange={(event) => setBuyPeriod(Number(event.target.value))}
              >
                {PERIODS.map((days) => (
                  <option key={days} value={days}>{days} дней</option>
                ))}
              </select>
            </div>

            <div className="field buy-comment">
              <label htmlFor="buy-descr">Комментарий (необязательно)</label>
              <input
                id="buy-descr"
                type="text"
                maxLength={50}
                placeholder="Например, для проекта X"
                value={descr}
                onChange={(event) => setDescr(event.target.value)}
              />
            </div>

            {countriesQuery.isError ? (
              <p className="form-hint buy-error">
                Не удалось получить список стран: {countriesQuery.error?.message}
              </p>
            ) : null}
            {priceQuery.isError ? (
              <p className="form-hint buy-error">
                Не удалось рассчитать стоимость: {priceQuery.error?.message}
              </p>
            ) : null}

            <div className="form-footer">
              <div className="buy-summary">
                <span className="muted">
                  {countValid
                    ? `${count} × ${versionLabel(version)}, ${countryLabel(country)}, ${buyPeriod} дн.`
                    : "Укажите количество от 1 до 1000"}
                </span>
                <strong className="buy-price">
                  {priceLoading ? "Считаю…" : priceText}
                </strong>
              </div>
              <button type="submit" disabled={!canBuy}>
                {buy.isPending ? "Покупаю…" : "Купить"}
              </button>
            </div>
          </form>
        </div>
      </Section>

      <Section
        id="proxies-title"
        icon="⇄"
        title="Прокси px6.net"
        note={`${enabledCount} из ${summary?.proxies_total ?? rows.length} включено`}
      >
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Прокси</th>
                <th>Страна</th>
                <th>Версия</th>
                <th>Действует до</th>
                <th>Комментарий</th>
                <th>Статус</th>
                <th>Токены</th>
                <th>Действия</th>
              </tr>
            </thead>
            <tbody>
              {query.isLoading ? (
                <LoadingRows colSpan={8} />
              ) : rows.length ? (
                rows.map((row) => <ProxyTableRow key={row.id ?? row.display} row={row} />)
              ) : (
                <EmptyRow colSpan={8}>
                  Прокси не найдены. Нажмите «Обновить список» или купите прокси.
                </EmptyRow>
              )}
            </tbody>
          </table>
        </div>
      </Section>

      <ConfirmDialog
        open={confirmOpen}
        title="Купить прокси?"
        confirmLabel="Купить"
        pendingLabel="Покупаю…"
        tone="accent"
        pending={buy.isPending}
        onCancel={() => !buy.isPending && setConfirmOpen(false)}
        onConfirm={confirmBuy}
      >
        <dl className="confirm-summary">
          <div>
            <dt>Прокси</dt>
            <dd>{count} × {versionLabel(version)}, {countryLabel(country)}</dd>
          </div>
          <div>
            <dt>Период</dt>
            <dd>{buyPeriod} дней</dd>
          </div>
          <div>
            <dt>Стоимость</dt>
            <dd>{priceQuery.isFetching ? "Считаю…" : priceText}</dd>
          </div>
          <div>
            <dt>Баланс после</dt>
            <dd>{balanceAfter !== undefined ? formatMoney(balanceAfter, currency) : "—"}</dd>
          </div>
          {descr.trim() ? (
            <div>
              <dt>Комментарий</dt>
              <dd>{descr.trim()}</dd>
            </div>
          ) : null}
        </dl>
        <p className="muted confirm-note">
          Прокси будут добавлены выключенными. Включите нужные в таблице ниже — только
          они начнут использоваться шлюзом. Стоимость ориентировочная и фиксируется px6.net
          при покупке.
        </p>
      </ConfirmDialog>
    </>
  );
}

function ProxyTableRow({ row }: { row: ProxyRow }) {
  const setProxy = useSetProxy();
  const pending = setProxy.isPending && setProxy.variables?.proxyId === row.id;
  const state = proxyState(row);
  const meta = row.px6;

  return (
    <tr className={row.enabled ? undefined : "dead"}>
      <td className="mono">{row.display}</td>
      <td>{countryLabel(meta?.country ?? "")}</td>
      <td>{versionLabel(meta?.version ?? "")}</td>
      <td className="mono muted">{proxyDate(meta?.date_end ?? "")}</td>
      <td className="muted">{meta?.descr || "—"}</td>
      <td><StatusBadge label={state.label} tone={state.tone} /></td>
      <td>{formatNumber(row.usage.tokens)}</td>
      <td className="actions">
        <button
          type="button"
          className={row.enabled ? "danger" : "acc"}
          disabled={!row.id || pending}
          title={
            row.enabled
              ? "Исключить прокси из пула шлюза"
              : "Разрешить использование этого прокси шлюзом"
          }
          onClick={() => {
            if (row.id) setProxy.mutate({ proxyId: row.id, enabled: !row.enabled });
          }}
        >
          {pending ? "…" : row.enabled ? "Выключить" : "Включить"}
        </button>
      </td>
    </tr>
  );
}
