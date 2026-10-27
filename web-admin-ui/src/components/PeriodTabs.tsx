import { PERIODS, type Period } from "../api/types";
import { periodLabel, periodTitle, useI18n } from "../i18n";

export function PeriodTabs({
  active,
  onChange,
}: {
  active: Period;
  onChange: (period: Period) => void;
}) {
  const { language, t } = useI18n();
  const tabs = PERIODS.map((period) => (
    <button
      key={period}
      type="button"
      className={period === active ? "active" : undefined}
      aria-pressed={period === active}
      title={periodTitle(period, language)}
      onClick={() => onChange(period)}
    >
      {periodLabel(period, language)}
    </button>
  ));

  return (
    <div className="period-bar page-enter">
      <div className="period-title">{t("period.title")}</div>
      <nav className="period-tabs" aria-label={t("period.aria")}>
        {tabs}
      </nav>
    </div>
  );
}
