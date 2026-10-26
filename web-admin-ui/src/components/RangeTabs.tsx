import type { StatsRange } from "../api/types";

const RANGES: ReadonlyArray<{ value: StatsRange; label: string }> = [
  { value: 7, label: "7 дней" },
  { value: 30, label: "30 дней" },
  { value: 90, label: "90 дней" },
];

export function RangeTabs({
  active,
  onChange,
}: {
  active: StatsRange;
  onChange: (range: StatsRange) => void;
}) {
  return (
    <div className="period-bar page-enter">
      <div className="period-title">Период статистики</div>
      <nav className="period-tabs range-tabs" aria-label="Период статистики">
        {RANGES.map((range) => (
          <button
            key={range.value}
            type="button"
            className={range.value === active ? "active" : undefined}
            aria-pressed={range.value === active}
            onClick={() => onChange(range.value)}
          >
            {range.label}
          </button>
        ))}
      </nav>
    </div>
  );
}
