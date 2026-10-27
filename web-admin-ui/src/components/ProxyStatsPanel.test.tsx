import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { StatsProxyRow } from "../api/types";
import { I18nProvider } from "../i18n";
import { ProxyStatsPanel } from "./ProxyStatsPanel";

function row(proxy: string, in_pool: boolean, tokens_total: number): StatsProxyRow {
  return {
    proxy,
    in_pool,
    tokens_total,
    requests_total: 1,
    errors_total: 0,
  };
}

describe("ProxyStatsPanel", () => {
  it("puts in-pool proxies first and orders each group by token spend", () => {
    render(
      <I18nProvider>
        <ProxyStatsPanel
          rows={[
            row("out-high:8080", false, 900),
            row("in-low:8080", true, 10),
            row("in-high:8080", true, 500),
            row("out-low:8080", false, 1),
          ]}
        />
      </I18nProvider>,
    );

    const names = screen
      .getAllByRole("row")
      .slice(1)
      .map((tableRow) => within(tableRow).getAllByRole("cell")[0].textContent);

    expect(names).toEqual([
      "in-high:8080",
      "in-low:8080",
      "out-high:8080",
      "out-low:8080",
    ]);
  });
});
