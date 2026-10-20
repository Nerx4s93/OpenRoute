import { PERIODS, type Period } from "../api/types";

export function PeriodTabs({
  active,
  onChange,
}: {
  active: Period;
  onChange: (period: Period) => void;
}) {
  const tabs = PERIODS.map((period) => (
    <button
      key={period.value}
      type="button"
      className={period.value === active ? "active" : undefined}
      aria-pressed={period.value === active}
      title={period.title}
      onClick={() => onChange(period.value)}
    >
      {period.label}
    </button>
  ));

  return (
    <div className="period-bar page-enter">
      <div className="period-title">Период расходов</div>
      <nav className="period-tabs" aria-label="Период расходов">
        {tabs}
      </nav>
    </div>
  );
}
