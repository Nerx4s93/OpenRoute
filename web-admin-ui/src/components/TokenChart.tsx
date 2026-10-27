import { useState } from "react";
import { useI18n } from "../i18n";
import {
  formatCompact,
  formatLongDate,
  formatNumber,
  formatShortDate,
} from "../lib/format";

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
const TOOLTIP_WIDTH = 250;
const TOOLTIP_HEIGHT = 62;
const TOOLTIP_GAP = 10;

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
  const { language, t } = useI18n();
  const [activeDay, setActiveDay] = useState<number | null>(null);
  const totals = dates.map((_, index) =>
    series.reduce((sum, item) => sum + positive(item.values?.[index]), 0),
  );
  const maxTotal = totals.reduce((max, value) => Math.max(max, value), 0);

  if (!dates.length || !series.length || maxTotal <= 0) {
    return <div className="chart-empty">{t("tokenStats.chartNoData")}</div>;
  }

  const axisMax = niceStep(maxTotal / GRID_STEPS) * GRID_STEPS;
  const slotWidth = PLOT_WIDTH / dates.length;
  const barWidth = Math.max(2, Math.min(slotWidth * 0.68, 48));
  const xLabelIndices = labelIndices(dates.length, MAX_X_LABELS);

  const activeIndex =
    activeDay !== null && activeDay < dates.length ? activeDay : null;
  const activeSlotX = activeIndex === null ? 0 : PAD_LEFT + activeIndex * slotWidth;
  const activeSlotCenter = activeSlotX + slotWidth / 2;
  const activeTotal = activeIndex === null ? 0 : totals[activeIndex];
  const activeBarTop =
    PLOT_BOTTOM - (activeTotal / axisMax) * PLOT_HEIGHT;
  const tooltipX = Math.min(
    Math.max(PAD_LEFT, activeSlotCenter - TOOLTIP_WIDTH / 2),
    VIEW_WIDTH - PAD_RIGHT - TOOLTIP_WIDTH,
  );
  const tooltipY = Math.max(
    PAD_TOP,
    Math.min(
      PLOT_BOTTOM - TOOLTIP_HEIGHT,
      activeBarTop - TOOLTIP_HEIGHT - TOOLTIP_GAP,
    ),
  );

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
          {formatCompact(value, language)}
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
        />
      );
    });
    return (
      <g
        key={date}
        onMouseEnter={() => setActiveDay(dayIndex)}
        onMouseLeave={() => setActiveDay(null)}
      >
        <rect
          className="chart-hover-zone"
          x={PAD_LEFT + dayIndex * slotWidth}
          y={PAD_TOP}
          width={slotWidth}
          height={PLOT_HEIGHT}
          aria-hidden="true"
        />
        {segments}
      </g>
    );
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
        {formatShortDate(dates[index], language)}
      </text>
    );
  });

  return (
    <div className="chart">
      <div
        className="chart-legend"
        role="img"
        aria-label={t("tokenStats.chartLegend", {
          labels: series.map((item) => item.label).join(", "),
        })}
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
        aria-label={t("tokenStats.chartAria")}
      >
        {gridLines}
        {activeIndex !== null ? (
          <g className="chart-hover-guide" pointerEvents="none">
            <rect
              className="chart-hover-highlight"
              x={activeSlotX + 1}
              y={PAD_TOP}
              width={Math.max(0, slotWidth - 2)}
              height={PLOT_HEIGHT}
              rx={5}
            />
            <line
              className="chart-hover-line"
              x1={activeSlotCenter}
              x2={activeSlotCenter}
              y1={PAD_TOP}
              y2={PLOT_BOTTOM}
            />
          </g>
        ) : null}
        {bars}
        {xLabels}
        {activeIndex !== null ? (
          <g className="chart-tooltip" pointerEvents="none">
            <rect
              className="chart-tooltip-bg"
              x={tooltipX}
              y={tooltipY}
              width={TOOLTIP_WIDTH}
              height={TOOLTIP_HEIGHT}
              rx={9}
            />
            <rect
              className="chart-tooltip-accent"
              x={tooltipX}
              y={tooltipY + 9}
              width={3}
              height={TOOLTIP_HEIGHT - 18}
              rx={2}
            />
            <text
              className="chart-tooltip-date"
              x={tooltipX + 14}
              y={tooltipY + 23}
            >
              {formatLongDate(dates[activeIndex], language)}
            </text>
            <text
              className="chart-tooltip-value"
              x={tooltipX + 14}
              y={tooltipY + 45}
            >
              {t("tokenStats.chartSpend", {
                tokens: formatNumber(activeTotal, language),
              })}
            </text>
          </g>
        ) : null}
      </svg>
    </div>
  );
}
