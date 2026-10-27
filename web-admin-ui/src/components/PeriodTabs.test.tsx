import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { I18nProvider } from "../i18n";
import { PeriodTabs } from "./PeriodTabs";

describe("PeriodTabs", () => {
  it("marks the active period and reports clicks", async () => {
    const onChange = vi.fn();
    render(
      <I18nProvider>
        <PeriodTabs active="all" onChange={onChange} />
      </I18nProvider>,
    );

    expect(screen.getByRole("button", { name: "All time" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.getByRole("button", { name: "7d" })).toHaveAttribute(
      "aria-pressed",
      "false",
    );

    await userEvent.click(screen.getByRole("button", { name: "7d" }));
    expect(onChange).toHaveBeenCalledWith("7d");
  });
});
