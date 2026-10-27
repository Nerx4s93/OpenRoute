import { useEffect, useState, type FormEvent } from "react";
import { useBuyProxies, usePx6Countries, usePx6Price } from "../hooks/usePx6";
import { useDebouncedValue } from "../hooks/useDebouncedValue";
import { formatDays, translate, useI18n } from "../i18n";
import { formatMoney } from "../lib/format";
import {
  VERSIONS,
  countryLabel,
  periodsForVersion,
  versionLabel,
} from "../lib/px6";
import { ConfirmDialog } from "./ConfirmDialog";

const DEFAULT_VERSION = 6;
const DEFAULT_PERIOD = 30;
const DEFAULT_COUNT = "1";

export function PurchaseProxyDialog({
  open,
  onClose,
}: {
  open: boolean;
  onClose: () => void;
}) {
  const { language, t } = useI18n();
  const [version, setVersion] = useState(DEFAULT_VERSION);
  const [country, setCountry] = useState("");
  const [countText, setCountText] = useState(DEFAULT_COUNT);
  const [buyPeriod, setBuyPeriod] = useState(DEFAULT_PERIOD);

  // All px6.net queries are disabled until the operator explicitly opens the
  // purchase dialog. Merely visiting /admin/proxies must not hit px6.net.
  const countriesQuery = usePx6Countries(version, open);
  const countryList = countriesQuery.data?.countries;

  useEffect(() => {
    if (!open || !countryList?.length) return;
    if (!countryList.includes(country)) {
      setCountry(countryList.includes("ru") ? "ru" : countryList[0]);
    }
  }, [countryList, country, open]);

  const parsedCount = Number(countText);
  const count = Number.isFinite(parsedCount) && Number.isInteger(parsedCount)
    ? parsedCount
    : 0;
  const countValid = count >= 1 && count <= 1000;
  const debouncedCount = useDebouncedValue(countValid ? count : 0, 500);
  const priceQuery = usePx6Price(version, debouncedCount, buyPeriod, open);
  const buy = useBuyProxies();

  const price = priceQuery.data;
  const priceReady = Boolean(
    price &&
      !priceQuery.isFetching &&
      price.count === count &&
      price.period === buyPeriod &&
      price.version === version,
  );
  const currency = price?.currency ?? "RUB";
  const priceText = price
    ? formatMoney(price.price, price.currency || currency, language)
    : "—";
  const priceLoading = countValid && !priceReady && !priceQuery.isError;
  const balance = price?.balance;
  const balanceAfter =
    price && priceReady && balance !== undefined
      ? balance - price.price
      : undefined;
  const periods = periodsForVersion(version);
  const canBuy = Boolean(
    open &&
      country &&
      countryList?.includes(country) &&
      countValid &&
      priceReady &&
      !buy.isPending &&
      !countriesQuery.isLoading &&
      !countriesQuery.isError,
  );

  const confirmBuy = () => {
    if (!canBuy) return;
    buy.mutate(
      {
        count,
        period: buyPeriod,
        country,
        version,
      },
      { onSuccess: onClose },
    );
  };

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    confirmBuy();
  };

  return (
    <ConfirmDialog
      open={open}
      title={t("proxies.buyTitle")}
      confirmLabel={t("common.buy")}
      pendingLabel={t("common.buying")}
      tone="accent"
      pending={buy.isPending}
      confirmDisabled={!canBuy}
      className="buy-modal"
      onCancel={() => !buy.isPending && onClose()}
      onConfirm={confirmBuy}
    >
      <form className="add buy-form" onSubmit={submit}>
        <div className="field">
          <label htmlFor="buy-version">{t("proxies.version")}</label>
          <select
            id="buy-version"
            autoFocus
            value={version}
            onChange={(event) => {
              const nextVersion = Number(event.target.value);
              setVersion(nextVersion);
              if (!periodsForVersion(nextVersion).includes(buyPeriod)) {
                setBuyPeriod(7);
              }
            }}
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
            {periods.map((days) => (
              <option key={days} value={days}>
                {formatDays(days, language)}
              </option>
            ))}
          </select>
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

        <div className="buy-dialog-summary">
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
          <div className="buy-dialog-balance">
            <span>
              {t("proxies.balance")}:{" "}
              <strong>
                {balance !== undefined
                  ? formatMoney(balance, currency, language)
                  : priceQuery.isFetching
                    ? "…"
                    : "—"}
              </strong>
            </span>
            <span>
              {t("proxies.confirmBalanceAfter")}:{" "}
              <strong>
                {balanceAfter !== undefined
                  ? formatMoney(balanceAfter, currency, language)
                  : "—"}
              </strong>
            </span>
          </div>
        </div>
      </form>
    </ConfirmDialog>
  );
}
