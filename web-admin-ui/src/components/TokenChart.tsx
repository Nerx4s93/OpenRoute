import { formatCompact, formatNumber } from "../lib/format";

export interface TokenChartSeries {
  label: string;
  values: number[];
  colorIndex: number;
}

const VIEW_WIDTH = 900;
const VIEW_HEIGHT = 340;
const PAD_LEFT = 58;
const PAD_RIGHT = 18;
const PAD_TOP = 18;
const PAD_BOTTOM = 42;
const PLOT_WIDTH = VIEW_WIDTH - PAD_LEFT - PAD_RIGHT;
const PLOT_HEIGHT = VIEW_HEIGHT - PAD_TOP - PAD_BOTTOM;
const PLOT_BOTTOM = PAD_TOP + PLOT_HEIGHT;
const GRID_STEPS = 4;
const MAX_X_LABELS = 8;
const PALETTE_SIZE = 9;

function positive(value: number | undefined): number {
  return typeof value === "number" && Number.isFinite(value) && value > 0 ? value : 0;
}

// Round the axis step up to a readable 1/2/2.5/5 · 10^n value.
function niceStep(value: number): number {
  if (!Number.isFinite(value) || value <= 0) return 1;
  const exponent = Math.floor(Math.log10(value));
  const base = 10 ** exponent;
  const fraction = value / base;
  const nice = fraction <= 1 ? 1 : fraction <= 2 ? 2 : fraction <= 2.5 ? 2.5 : fraction <= 5 ? 5 : 10;
  return nice * base;
}

function shortDate(value: string): string {
  const [, month, day] = value.split("-");
  return month && day ? `${day}.${month}` : value;
}

function labelIndices(count: number, limit: number): number[] {
  if (count <= limit) return Array.from({ length: count }, (_, index) => index);
  const step = (count - 1) / (limit - 1);
  return Array.from(new Set(Array.from({ length: limit }, (_, index) => Math.round(index * step))));
}

function colorClass(colorIndex: number): string {
  return `chart-c${((colorIndex % PALETTE_SIZE) + PALETTE_SIZE) % PALETTE_SIZE}`;
}

export function TokenChart({
  dates,
  series,
}: {
  dates: string[];
  series: TokenChartSeries[];
}) {
  const totals = dates.map((_, index) =>
    series.reduce((sum, item) => sum + positive(item.values?.[index]), 0),
  );
  const maxTotal = totals.reduce((max, value) => Math.max(max, value), 0);

  if (!dates.length || !series.length || maxTotal <= 0) {
    return <div className="chart-empty">Нет данных за выбранный период</div>;
  }

  const axisMax = niceStep(maxTotal / GRID_STEPS) * GRID_STEPS;
  const slotWidth = PLOT_WIDTH / dates.length;
  const barWidth = Math.max(2, Math.min(slotWidth * 0.68, 48));
  const xLabelIndices = labelIndices(dates.length, MAX_X_LABELS);

  const gridLines = Array.from({ length: GRID_STEPS + 1 }, (_, tick) => {
    const value = (axisMax / GRID_STEPS) * tick;
    const y = PLOT_BOTTOM - (value / axisMax) * PLOT_HEIGHT;
    return (
      <g key={`grid-${tick}`}>
        <line
          className="chart-grid-line"
          x1={PAD_LEFT}
          x2={VIEW_WIDTH - PAD_RIGHT}
          y1={y}
          y2={y}
        />
        <text className="chart-axis" x={PAD_LEFT - 10} y={y + 4} textAnchor="end">
          {formatCompact(value)}
        </text>
      </g>
    );
  });

  const bars = dates.map((date, dayIndex) => {
    const x = PAD_LEFT + dayIndex * slotWidth + (slotWidth - barWidth) / 2;
    let cumulative = 0;
    const segments = series.map((item, seriesIndex) => {
      const value = positive(item.values?.[dayIndex]);
      if (!value) return null;
      const y = PLOT_BOTTOM - ((cumulative + value) / axisMax) * PLOT_HEIGHT;
      const height = (value / axisMax) * PLOT_HEIGHT;
      cumulative += value;
      return (
        <rect
          key={`${date}-${seriesIndex}`}
          className={`chart-bar ${colorClass(item.colorIndex)}`}
          x={x}
          y={y}
          width={barWidth}
          height={height}
          rx={1}
        >
          <title>{`${date} · ${item.label}: ${formatNumber(value)} токенов`}</title>
        </rect>
      );
    });
    return <g key={date}>{segments}</g>;
  });

  const xLabels = xLabelIndices.map((index) => {
    const x = PAD_LEFT + index * slotWidth + slotWidth / 2;
    return (
      <text
        key={`x-${dates[index]}`}
        className="chart-axis"
        x={x}
        y={PLOT_BOTTOM + 20}
        textAnchor="middle"
      >
        {shortDate(dates[index])}
      </text>
    );
  });

  return (
    <div className="chart">
      <div
        className="chart-legend"
        role="img"
        aria-label={`Легенда: ${series.map((item) => item.label).join(", ")}`}
      >
        {series.map((item) => (
          <span key={item.label} className="chart-legend-item">
            <span
              className={`chart-swatch ${colorClass(item.colorIndex)}`}
              aria-hidden="true"
            />
            {item.label}
          </span>
        ))}
      </div>
      <svg
        className="chart-svg"
        viewBox={`0 0 ${VIEW_WIDTH} ${VIEW_HEIGHT}`}
        role="img"
        aria-label="Токены по дням (UTC)"
      >
        {gridLines}
        {bars}
        {xLabels}
      </svg>
    </div>
  );
}
