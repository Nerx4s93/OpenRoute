import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";
import { AuthProvider } from "./auth/AuthProvider";
import { ToastProvider } from "./components/Toast";
import { I18nProvider } from "./i18n";
import type { KeysSnapshot, ProxiesSnapshot, StatsSnapshot } from "./api/types";

const proxies: ProxiesSnapshot = {
  server_time: "2026-10-20T10:00:00+00:00",
  summary: { requests: 12, errors: 1, tokens: 340, proxies_ready: 1, proxies_total: 1 },
  proxies: [
    {
      id: "proxy-1",
      display: "203.0.113.10:8080",
      enabled: true,
      manual: false,
      banned: false,
      quota_seconds_left: 0,
      cooldown_seconds_left: 0,
      rpm_capped: false,
      last_error: null,
      usage: { requests: 12, errors: 1, tokens: 340 },
    },
  ],
  recent: [],
};

const datedProxies: ProxiesSnapshot = {
  ...proxies,
  proxies: [
    {
      ...proxies.proxies[0],
      id: "proxy-dated",
      display: "203.0.113.11:8080",
      px6: {
        id: "1",
        version: "6",
        country: "ru",
        date_end: "2026-12-01 00:00:00",
        descr: "",
      },
    },
    {
      ...proxies.proxies[0],
      id: "proxy-undated",
      display: "203.0.113.12:8080",
      px6: { id: "2", version: "6", country: "ru", date_end: "", descr: "" },
    },
  ],
};

const stats: StatsSnapshot = {
  server_time: "2026-10-20T10:00:00+00:00",
  days: 7,
  dates: [
    "2026-10-14",
    "2026-10-15",
    "2026-10-16",
    "2026-10-17",
    "2026-10-18",
    "2026-10-19",
    "2026-10-20",
  ],
  totals: {
    tokens_total: 3500,
    requests_total: 36,
    errors_total: 2,
  },
  proxies: [
    {
      proxy: "203.0.113.10:8080",
      in_pool: true,
      tokens_total: 3400,
      requests_total: 34,
      errors_total: 2,
    },
    {
      proxy: "203.0.113.7:8080",
      in_pool: false,
      tokens_total: 100,
      requests_total: 2,
      errors_total: 0,
    },
  ],
  series_total: {
    tokens: [100, 200, 400, 500, 600, 700, 1000],
    requests: [1, 2, 4, 5, 6, 7, 11],
    errors: [0, 0, 0, 0, 1, 0, 1],
  },
  series_by_proxy: {
    "203.0.113.10:8080": {
      tokens: [100, 200, 400, 500, 600, 700, 900],
      requests: [1, 2, 4, 5, 6, 7, 9],
      errors: [0, 0, 0, 0, 1, 0, 1],
    },
    "203.0.113.7:8080": {
      tokens: [0, 0, 0, 0, 0, 0, 100],
      requests: [0, 0, 0, 0, 0, 0, 2],
      errors: [0, 0, 0, 0, 0, 0, 0],
    },
  },
};

const keys: KeysSnapshot = {
  server_time: "2026-10-20T10:00:00+00:00",
  keys: [
    {
      key_hash: "hash-1",
      key_id: "default",
      rpm: null,
      tpm: null,
      models: null,
      enabled: true,
    },
  ],
  stats: [{ key_id: "default", state: "active", requests: 3, errors: 0, tokens: 42 }],
};

function json(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

function LocationProbe() {
  const location = useLocation();
  return (
    <span data-testid="location">
      {location.pathname}
      {location.search}
    </span>
  );
}

function renderApp(path: string) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[path]}>
        <I18nProvider>
          <AuthProvider>
            <ToastProvider>
              <App />
              <LocationProbe />
            </ToastProvider>
          </AuthProvider>
        </I18nProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  window.localStorage.clear();
  document.documentElement.lang = "";
  vi.stubGlobal("EventSource", undefined);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("App", () => {
  it("shows the login form when there is no session", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => json({ authenticated: false })),
    );

    renderApp("/login");

    expect(
      await screen.findByRole("heading", { name: "OpenRoute Admin" }),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("Password")).toBeInTheDocument();
  });

  it("renders the statistics page at its dedicated route", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/api/admin/session")) {
          return json({ authenticated: true, csrf: "csrf-1", expires_at: 9_999_999_999 });
        }
        if (url.includes("/api/admin/proxies")) return json(proxies);
        if (url.includes("/api/admin/stats")) return json(stats);
        return json({});
      }),
    );

    renderApp("/admin/stats");

    expect(
      await screen.findByRole("heading", { name: "Statistics", level: 1 }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /Refresh list/ }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Proxies" })).toHaveClass("nav-proxies");
    expect(screen.getByRole("link", { name: "Statistics" })).toHaveClass("nav-stats");
    expect(screen.getByRole("link", { name: "Statistics" })).toHaveAttribute(
      "href",
      "/admin/stats",
    );
    expect(screen.getByRole("link", { name: "Settings" })).toHaveClass("nav-settings");
    expect(screen.getByRole("link", { name: "Settings" })).toHaveAttribute(
      "href",
      "/admin/settings",
    );
    expect(
      within(screen.getByRole("banner"))
        .getAllByRole("link")
        .map((link) => link.textContent),
    ).toEqual(["Proxies", "Statistics", "Keys", "Settings"]);
    expect(
      screen.getByRole("link", { name: "Settings" }).closest(".sidebar-bottom"),
    ).not.toBeNull();
    expect(
      await screen.findByRole("cell", { name: "203.0.113.10:8080" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Proxies in rotation")).toBeInTheDocument();
    expect(screen.getByText("Tokens spent")).toBeInTheDocument();
    expect(screen.getByText("Out of pool")).toBeInTheDocument();
    expect(
      screen.getByRole("columnheader", { name: "Tokens for the period" }),
    ).toBeInTheDocument();
    expect(screen.getAllByRole("columnheader", { name: "Tokens" })).toHaveLength(1);
    expect(screen.queryByRole("columnheader", { name: "Actions" })).not.toBeInTheDocument();
    expect(
      await screen.findByRole("heading", { name: "Tokens by day" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
    const sectionTitles = screen
      .getAllByRole("heading", { level: 2 })
      .map((node) => node.textContent);
    expect(sectionTitles.indexOf("Proxies")).toBe(
      sectionTitles.indexOf("Tokens by day") + 1,
    );
    for (const label of ["1d", "3d", "7d", "30d", "90d", "All time"]) {
      expect(screen.getByRole("button", { name: label })).toBeInTheDocument();
    }
  });

  it("requests token statistics for the selected statistics period", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/admin/session")) {
        return json({ authenticated: true, csrf: "csrf-1", expires_at: 9_999_999_999 });
      }
      if (url.includes("/api/admin/proxies")) return json(proxies);
      if (url.includes("/api/admin/stats")) return json(stats);
      return json({});
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    renderApp("/admin/stats");

    await screen.findByRole("heading", { name: "Tokens by day" });
    await waitFor(() => {
      expect(
        fetchMock.mock.calls.some(([url]) =>
          String(url).includes("/api/admin/stats?period=all"),
        ),
      ).toBe(true);
    });

    await user.click(screen.getByRole("button", { name: "7d" }));

    expect(await screen.findByText("Last 7 days")).toBeInTheDocument();
    await waitFor(() => {
      expect(
        fetchMock.mock.calls.some(([url]) =>
          String(url).includes("/api/admin/stats?period=7d"),
        ),
      ).toBe(true);
    });
  });

  it("restores the last selected statistics period after reopening", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/admin/session")) {
        return json({ authenticated: true, csrf: "csrf-1", expires_at: 9_999_999_999 });
      }
      if (url.includes("/api/admin/proxies")) return json(proxies);
      if (url.includes("/api/admin/stats")) return json(stats);
      return json({});
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    const first = renderApp("/admin/stats");
    await screen.findByRole("heading", { name: "Tokens by day" });
    await user.click(screen.getByRole("button", { name: "7d" }));
    expect(screen.getByRole("button", { name: "7d" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );

    first.unmount();
    fetchMock.mockClear();
    renderApp("/admin/stats");

    await screen.findByRole("heading", { name: "Tokens by day" });
    expect(screen.getByRole("button", { name: "7d" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    await waitFor(() => {
      expect(
        fetchMock.mock.calls.some(([url]) =>
          String(url).includes("/api/admin/stats?period=7d"),
        ),
      ).toBe(true);
    });
  });

  it("refreshes the proxy list from the proxies page", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, _init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/api/admin/session")) {
        return json({ authenticated: true, csrf: "csrf-1", expires_at: 9_999_999_999 });
      }
      if (url.includes("/api/admin/proxies/refresh")) {
        return json({ ok: true, message: "Список прокси обновлён" });
      }
      if (url.includes("/api/admin/proxies/account")) {
        return json({ user_id: "px6-user", balance: 250.5, currency: "RUB" });
      }
      if (url.includes("/api/admin/proxies/countries")) {
        return json({ version: 6, countries: ["de", "ru"] });
      }
      if (url.includes("/api/admin/proxies/price")) {
        return json({
          user_id: "px6-user", balance: 250.5, currency: "RUB",
          price: 12.5, price_single: 12.5, period: 30, count: 1, version: 6,
        });
      }
      if (url.includes("/api/admin/proxies")) return json(proxies);
      return json({});
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    renderApp("/admin/proxies");

    const button = await screen.findByRole("button", { name: /Refresh list/ });
    await user.click(button);

    expect(await screen.findByText("Proxy list refreshed")).toBeInTheDocument();
    const refreshCall = fetchMock.mock.calls.find(([url]) =>
      String(url).includes("/api/admin/proxies/refresh"),
    );
    expect(refreshCall).toBeTruthy();
    expect((refreshCall?.[1] as RequestInit).method).toBe("POST");
  });

  it("renders balance without purchase calculations and toggles a proxy", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, _init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/api/admin/session")) {
        return json({ authenticated: true, csrf: "csrf-1", expires_at: 9_999_999_999 });
      }
      if (url.includes("/api/admin/proxies/account")) {
        return json({ user_id: "px6-user", balance: 250.5, currency: "RUB" });
      }
      if (url.includes("/api/admin/proxies/countries")) {
        return json({ version: 6, countries: ["de", "ru"] });
      }
      if (url.includes("/api/admin/proxies/price")) {
        return json({
          user_id: "px6-user", balance: 250.5, currency: "RUB",
          price: 12.5, price_single: 12.5, period: 30, count: 1, version: 6,
        });
      }
      if (url.includes("/api/admin/proxies/set")) {
        return json({ ok: true, message: "Прокси отключён" });
      }
      if (url.includes("/api/admin/proxies")) return json(proxies);
      if (url.includes("/api/admin/stats")) return json(stats);
      return json({});
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    renderApp("/admin/proxies");

    expect(
      await screen.findByRole("heading", { name: "Proxies", level: 1 }),
    ).toBeInTheDocument();
    expect(await screen.findByRole("cell", { name: "203.0.113.10:8080" })).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Buy proxies" }),
    ).toBeInTheDocument();
    expect(await screen.findByText(/250\.50/)).toBeInTheDocument();
    expect(screen.queryByLabelText("Version")).not.toBeInTheDocument();
    expect(
      fetchMock.mock.calls.some(([url]) =>
        String(url).includes("/api/admin/proxies/account"),
      ),
    ).toBe(true);
    expect(
      fetchMock.mock.calls.some(([url]) =>
        /\/api\/admin\/proxies\/(countries|price)/.test(String(url)),
      ),
    ).toBe(false);
    expect(
      screen.queryByRole("columnheader", { name: "Comment" }),
    ).not.toBeInTheDocument();
    expect(screen.queryByText("ID px6-user")).not.toBeInTheDocument();
    expect(
      screen.queryByText("Only enabled proxies enter the gateway pool"),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByLabelText("Comment (optional)"),
    ).not.toBeInTheDocument();

    const toggle = await screen.findByRole("button", { name: "Disable" });
    await user.click(toggle);

    expect(await screen.findByText("Proxy disabled")).toBeInTheDocument();
    const setCall = fetchMock.mock.calls.find(([url]) =>
      String(url).includes("/api/admin/proxies/set"),
    );
    expect(setCall).toBeTruthy();
    expect((setCall?.[1] as RequestInit).method).toBe("POST");
    expect(String((setCall?.[1] as RequestInit).body)).toContain('"enabled":false');
  });

  it("formats the px6 valid-until date and keeps a dash when it is empty", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/api/admin/session")) {
          return json({ authenticated: true, csrf: "csrf-1", expires_at: 9_999_999_999 });
        }
        if (url.includes("/api/admin/proxies/account")) {
          return json({ user_id: "px6-user", balance: 250.5, currency: "RUB" });
        }
        if (url.includes("/api/admin/proxies")) return json(datedProxies);
        return json({});
      }),
    );

    renderApp("/admin/proxies");

    const dated = await screen.findByRole("row", { name: /203\.0\.113\.11:8080/ });
    expect(within(dated).getByText("Dec 1")).toBeInTheDocument();
    const undated = screen.getByRole("row", { name: /203\.0\.113\.12:8080/ });
    expect(within(undated).getByText("—")).toBeInTheDocument();
  });

  it("allows 3-day purchases only for IPv6", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/admin/session")) {
        return json({ authenticated: true, csrf: "csrf-1", expires_at: 9_999_999_999 });
      }
      if (url.includes("/api/admin/proxies/account")) {
        return json({ user_id: "px6-user", balance: 250.5, currency: "RUB" });
      }
      if (url.includes("/api/admin/proxies/countries")) {
        return json({ version: 6, countries: ["de", "ru"] });
      }
      if (url.includes("/api/admin/proxies/price")) {
        return json({
          user_id: "px6-user", balance: 250.5, currency: "RUB",
          price: 12.5, price_single: 12.5, period: 30, count: 1, version: 6,
        });
      }
      if (url.includes("/api/admin/proxies")) return json(proxies);
      return json({});
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    renderApp("/admin/proxies");

    await user.click(
      await screen.findByRole("button", { name: "Buy proxies" }),
    );
    const dialog = await screen.findByRole("dialog");
    const versionSelect = within(dialog).getByLabelText("Version");
    const periodSelect = within(dialog).getByLabelText("Period");

    expect(
      within(periodSelect).getByRole("option", { name: "3 days" }),
    ).toBeInTheDocument();

    await user.selectOptions(periodSelect, "3");
    expect(periodSelect).toHaveValue("3");

    await user.selectOptions(versionSelect, "4");
    expect(periodSelect).toHaveValue("7");
    expect(
      within(periodSelect).queryByRole("option", { name: "3 days" }),
    ).not.toBeInTheDocument();

    await user.selectOptions(versionSelect, "3");
    expect(
      within(periodSelect).queryByRole("option", { name: "3 days" }),
    ).not.toBeInTheDocument();

    await user.selectOptions(versionSelect, "6");
    expect(
      within(periodSelect).getByRole("option", { name: "3 days" }),
    ).toBeInTheDocument();
  });

  it("buys proxies from the purchase dialog", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, _init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/api/admin/session")) {
        return json({ authenticated: true, csrf: "csrf-1", expires_at: 9_999_999_999 });
      }
      if (url.includes("/api/admin/proxies/account")) {
        return json({ user_id: "px6-user", balance: 250.5, currency: "RUB" });
      }
      if (url.includes("/api/admin/proxies/countries")) {
        return json({ version: 6, countries: ["de", "ru"] });
      }
      if (url.includes("/api/admin/proxies/price")) {
        return json({
          user_id: "px6-user", balance: 250.5, currency: "RUB",
          price: 12.5, price_single: 12.5, period: 30, count: 1, version: 6,
        });
      }
      if (url.includes("/api/admin/proxies/buy")) {
        return json({
          ok: true,
          message: "Куплено 1 прокси. Новые прокси включены в ротацию.",
          order_id: "order-1", count: 1, price: 12.5, price_single: 12.5,
          period: 30, country: "ru", version: 6, currency: "RUB",
          balance: 238.0, refresh_ok: true,
        });
      }
      if (url.includes("/api/admin/proxies")) return json(proxies);
      if (url.includes("/api/admin/stats")) return json(stats);
      return json({});
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    renderApp("/admin/proxies");

    await user.click(
      await screen.findByRole("button", { name: "Buy proxies" }),
    );
    const dialog = await screen.findByRole("dialog");
    await waitFor(() => {
      expect(within(dialog).getByLabelText("Country")).toHaveValue("ru");
    });
    expect(await within(dialog).findByText(/250\.50/)).toBeInTheDocument();
    expect(
      within(dialog).queryByText(/Proxies will be added disabled/),
    ).not.toBeInTheDocument();

    const submit = within(dialog).getByRole("button", { name: "Buy" });
    await waitFor(() => expect(submit).toBeEnabled());
    await user.click(submit);

    expect(
      await screen.findByText("1 proxy purchased and added to rotation."),
    ).toBeInTheDocument();
    const buyCall = fetchMock.mock.calls.find(([url]) =>
      String(url).includes("/api/admin/proxies/buy"),
    );
    expect(buyCall).toBeTruthy();
    expect((buyCall?.[1] as RequestInit).method).toBe("POST");
    const body = JSON.parse(String((buyCall?.[1] as RequestInit).body));
    expect(body).toMatchObject({ count: 1, period: 30, country: "ru", version: 6 });
  });

  it("renders the keys page for an authenticated session", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/api/admin/session")) {
          return json({ authenticated: true, csrf: "csrf-1", expires_at: 9_999_999_999 });
        }
        if (url.includes("/api/admin/keys")) return json(keys);
        return json({});
      }),
    );

    renderApp("/admin/keys");

    expect(
      await screen.findByRole("heading", { name: "Client keys", level: 1 }),
    ).toBeInTheDocument();
    expect((await screen.findAllByText("default")).length).toBeGreaterThan(0);
    expect(screen.queryByLabelText("Access key")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Key ID")).toBeInTheDocument();
    expect(
      screen.queryByRole("columnheader", { name: "Name" }),
    ).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Name")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("columnheader", { name: "Admin" }),
    ).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Administrator")).not.toBeInTheDocument();
  });

  it("creates a key and shows the generated secret once", async () => {
    const generated = `openroute-${"a".repeat(48)}`;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, _init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/api/admin/session")) {
        return json({ authenticated: true, csrf: "csrf-1", expires_at: 9_999_999_999 });
      }
      if (url.includes("/api/admin/keys/add")) {
        return json({
          ok: true,
          message: "Ключ k1 добавлен",
          key: generated,
          key_id: "k1",
        });
      }
      if (url.includes("/api/admin/keys")) return json(keys);
      return json({});
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    renderApp("/admin/keys");
    await screen.findByRole("heading", { name: "Client keys", level: 1 });
    await user.click(screen.getByRole("button", { name: /Create key/ }));

    const dialog = await screen.findByRole("dialog", { name: "Key created" });
    expect(within(dialog).getByLabelText("Access key")).toHaveValue(generated);
    expect(within(dialog).getByText("k1")).toBeInTheDocument();

    const addCall = fetchMock.mock.calls.find(([url]) =>
      String(url).includes("/api/admin/keys/add"),
    );
    const payload = JSON.parse(String((addCall?.[1] as RequestInit).body));
    expect(payload).not.toHaveProperty("key");
    expect(payload).toMatchObject({ key_id: "", rpm: null, tpm: null, models: [] });

    await user.click(within(dialog).getByRole("button", { name: "Done" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog", { name: "Key created" })).not.toBeInTheDocument();
    });
    expect(screen.queryByLabelText("Access key")).not.toBeInTheDocument();
  });

  it("reissues a key and shows the new secret once", async () => {
    const generated = `openroute-${"b".repeat(48)}`;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, _init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/api/admin/session")) {
        return json({ authenticated: true, csrf: "csrf-1", expires_at: 9_999_999_999 });
      }
      if (url.includes("/api/admin/keys/rotate")) {
        return json({
          ok: true,
          message: "Ключ перевыпущен",
          key: generated,
          key_id: "default",
        });
      }
      if (url.includes("/api/admin/keys")) return json(keys);
      return json({});
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    renderApp("/admin/keys");
    await screen.findByRole("heading", { name: "Client keys", level: 1 });
    await user.click(
      await screen.findByRole("button", { name: "Reissue" }),
    );

    const confirm = await screen.findByRole("dialog", {
      name: /Reissue key default/,
    });
    expect(
      within(confirm).getByText(/The old key will stop working immediately/),
    ).toBeInTheDocument();
    await user.click(
      within(confirm).getByRole("button", { name: "Reissue" }),
    );

    const dialog = await screen.findByRole("dialog", { name: "Key reissued" });
    expect(within(dialog).getByLabelText("Access key")).toHaveValue(generated);

    const rotateCall = fetchMock.mock.calls.find(([url]) =>
      String(url).includes("/api/admin/keys/rotate"),
    );
    const payload = JSON.parse(String((rotateCall?.[1] as RequestInit).body));
    expect(payload).toEqual({ key_hash: "hash-1" });

    await user.click(within(dialog).getByRole("button", { name: "Done" }));
    await waitFor(() => {
      expect(
        screen.queryByRole("dialog", { name: "Key reissued" }),
      ).not.toBeInTheDocument();
    });
    expect(screen.queryByLabelText("Access key")).not.toBeInTheDocument();
  });

  it("redirects the admin root to the statistics endpoint", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/api/admin/session")) {
          return json({ authenticated: true, csrf: "csrf-1", expires_at: 9_999_999_999 });
        }
        if (url.includes("/api/admin/proxies")) return json(proxies);
        if (url.includes("/api/admin/stats")) return json(stats);
        return json({});
      }),
    );

    renderApp("/admin?period=7d");

    expect(
      await screen.findByRole("heading", { name: "Statistics", level: 1 }),
    ).toBeInTheDocument();
    expect(screen.getByTestId("location")).toHaveTextContent(
      "/admin/stats?period=7d",
    );
  });

  it("navigates to statistics from the sidebar", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/admin/session")) {
        return json({ authenticated: true, csrf: "csrf-1", expires_at: 9_999_999_999 });
      }
      if (url.includes("/api/admin/keys")) return json(keys);
      if (url.includes("/api/admin/stats")) return json(stats);
      if (url.includes("/api/admin/proxies")) return json(proxies);
      return json({});
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    renderApp("/admin/keys");

    await screen.findByRole("heading", { name: "Client keys", level: 1 });
    await user.click(screen.getByRole("link", { name: "Statistics" }));

    expect(
      await screen.findByRole("heading", { name: "Statistics", level: 1 }),
    ).toBeInTheDocument();
    expect(screen.getByTestId("location")).toHaveTextContent("/admin/stats");
    await waitFor(() => {
      expect(
        fetchMock.mock.calls.some(([url]) =>
          String(url).includes("/api/admin/stats?period=all"),
        ),
      ).toBe(true);
    });
  });

  it("defaults to English and persists the selected language", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/api/admin/session")) {
          return json({ authenticated: true, csrf: "csrf-1", expires_at: 9_999_999_999 });
        }
        return json({});
      }),
    );
    const user = userEvent.setup();

    const first = renderApp("/admin/settings");

    expect(
      await screen.findByRole("heading", { name: "Settings", level: 1 }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^English/ })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.getByRole("button", { name: /^Русский/ })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
    await waitFor(() => expect(document.documentElement.lang).toBe("en"));

    await user.click(screen.getByRole("button", { name: /^Русский/ }));

    expect(
      await screen.findByRole("heading", { name: "Настройки", level: 1 }),
    ).toBeInTheDocument();
    expect(window.localStorage.getItem("openroute.admin.language")).toBe("ru");
    await waitFor(() => expect(document.documentElement.lang).toBe("ru"));

    first.unmount();
    renderApp("/admin/settings");

    expect(
      await screen.findByRole("heading", { name: "Настройки", level: 1 }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Русский/ })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });
});
