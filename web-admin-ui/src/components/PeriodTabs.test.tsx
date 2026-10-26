import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { PeriodTabs } from "./PeriodTabs";

describe("PeriodTabs", () => {
  it("marks the active period and reports clicks", async () => {
    const onChange = vi.fn();
    render(<PeriodTabs active="all" onChange={onChange} />);

    expect(screen.getByRole("button", { name: "Всё время" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.getByRole("button", { name: "7д" })).toHaveAttribute(
      "aria-pressed",
      "false",
    );

    await userEvent.click(screen.getByRole("button", { name: "7д" }));
    expect(onChange).toHaveBeenCalledWith("7d");
  });
});
