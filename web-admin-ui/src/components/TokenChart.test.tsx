import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { TokenChart } from "./TokenChart";

const dates = ["2026-10-19", "2026-10-20"];

describe("TokenChart", () => {
  it("renders stacked bars with a legend and no inline styles", () => {
    const { container } = render(
      <TokenChart
        dates={dates}
        series={[
          { label: "proxy-a", values: [10, 20], colorIndex: 0 },
          { label: "proxy-b", values: [5, 0], colorIndex: 1 },
        ]}
      />,
    );

    expect(screen.getByRole("img", { name: "Токены по дням (UTC)" })).toBeInTheDocument();
    expect(screen.getByText("proxy-a")).toBeInTheDocument();
    expect(screen.getByText("proxy-b")).toBeInTheDocument();
    expect(container.querySelectorAll(".chart-bar").length).toBe(3);
    expect(container.querySelector("rect[style]")).toBeNull();
    expect(container.querySelector("[style]")).toBeNull();
  });

  it("shows the date and daily spend when hovering a column", () => {
    const { container } = render(
      <TokenChart
        dates={dates}
        series={[
          { label: "proxy-a", values: [10, 20], colorIndex: 0 },
          { label: "proxy-b", values: [5, 0], colorIndex: 1 },
        ]}
      />,
    );

    const zones = container.querySelectorAll(".chart-hover-zone");
    expect(zones).toHaveLength(2);

    fireEvent.mouseEnter(zones[1]);

    expect(screen.getByText(/20 октября 2026/)).toBeInTheDocument();
    expect(screen.getByText("Расход: 20 токенов")).toBeInTheDocument();
  });

  it("shows an empty state for an empty series list", () => {
    render(<TokenChart dates={dates} series={[]} />);

    expect(screen.getByText("Нет данных за выбранный период")).toBeInTheDocument();
  });

  it("shows an empty state when every value is zero", () => {
    const { container } = render(
      <TokenChart
        dates={dates}
        series={[{ label: "proxy-a", values: [0, 0], colorIndex: 0 }]}
      />,
    );

    expect(screen.getByText("Нет данных за выбранный период")).toBeInTheDocument();
    expect(container.querySelector("svg")).toBeNull();
  });
});
