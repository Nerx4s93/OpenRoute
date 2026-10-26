import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";
import { AuthProvider } from "./auth/AuthProvider";
import { ToastProvider } from "./components/Toast";
import type { KeysSnapshot, ProxiesSnapshot, StatsSnapshot } from "./api/types";

const proxies: ProxiesSnapshot = {
  server_time: "2026-10-20T10:00:00+00:00",
  summary: { requests: 12, errors: 1, tokens: 340, proxies_ready: 1, proxies_total: 1 },
  totals: { tokens_total: 3400 },
  proxies: [
    {
      id: "proxy-1",
      display: "203.0.113.10:8080",
      enabled: true,
      manual: false,
      disabled: false,
      banned: false,
      quota_seconds_left: 0,
      banned_seconds_left: 0,
      cooldown_seconds_left: 0,
      rpm_capped: false,
      last_error: null,
      usage: { requests: 12, errors: 1, tokens: 340 },
    },
  ],
  recent: [],
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
      name: "Default",
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

function renderApp(path: string) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[path]}>
        <AuthProvider>
          <ToastProvider>
            <App />
          </ToastProvider>
        </AuthProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
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
    expect(screen.getByLabelText("Пароль")).toBeInTheDocument();
  });

  it("renders the statistics page for an authenticated session", async () => {
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

    renderApp("/admin/ui");

    expect(
      await screen.findByRole("heading", { name: "Статистика", level: 1 }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /Обновить список/ }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Прокси" })).toHaveClass("nav-proxies");
    expect(screen.getByRole("link", { name: "Статистика" })).toHaveClass("nav-stats");
    expect(
      within(screen.getByRole("banner"))
        .getAllByRole("link")
        .map((link) => link.textContent),
    ).toEqual(["Прокси", "Статистика", "Ключи"]);
    expect(
      await screen.findByRole("cell", { name: "203.0.113.10:8080" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Прокси в ротации")).toBeInTheDocument();
    expect(screen.queryByText("Токенов за всё время")).not.toBeInTheDocument();
    expect(screen.getByText("Вне пула")).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Всего токенов" })).toBeInTheDocument();
    expect(screen.getAllByRole("columnheader", { name: "Токены" })).toHaveLength(1);
    expect(screen.queryByRole("columnheader", { name: "Действия" })).not.toBeInTheDocument();
    expect(
      await screen.findByRole("heading", { name: "Токены по дням" }),
    ).toBeInTheDocument();
    const sectionTitles = screen
      .getAllByRole("heading", { level: 2 })
      .map((node) => node.textContent);
    expect(sectionTitles.indexOf("Прокси")).toBe(
      sectionTitles.indexOf("Токены по дням") + 1,
    );
    for (const label of ["1д", "3д", "7д", "30д", "90д", "Всё время"]) {
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

    renderApp("/admin/ui");

    await screen.findByRole("heading", { name: "Токены по дням" });
    await waitFor(() => {
      expect(
        fetchMock.mock.calls.some(([url]) =>
          String(url).includes("/api/admin/stats?period=all"),
        ),
      ).toBe(true);
    });

    await user.click(screen.getByRole("button", { name: "7д" }));

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

    renderApp("/admin/ui/proxies");

    const button = await screen.findByRole("button", { name: /Обновить список/ });
    await user.click(button);

    expect(await screen.findByText("Список прокси обновлён")).toBeInTheDocument();
    const refreshCall = fetchMock.mock.calls.find(([url]) =>
      String(url).includes("/api/admin/proxies/refresh"),
    );
    expect(refreshCall).toBeTruthy();
    expect((refreshCall?.[1] as RequestInit).method).toBe("POST");
  });

  it("renders the proxies page with balance and toggles a proxy", async () => {
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

    renderApp("/admin/ui/proxies");

    expect(
      await screen.findByRole("heading", { name: "Прокси", level: 1 }),
    ).toBeInTheDocument();
    expect(await screen.findByText(/250,50/)).toBeInTheDocument();
    expect(await screen.findByRole("cell", { name: "203.0.113.10:8080" })).toBeInTheDocument();

    const toggle = await screen.findByRole("button", { name: "Выключить" });
    await user.click(toggle);

    expect(await screen.findByText("Прокси отключён")).toBeInTheDocument();
    const setCall = fetchMock.mock.calls.find(([url]) =>
      String(url).includes("/api/admin/proxies/set"),
    );
    expect(setCall).toBeTruthy();
    expect((setCall?.[1] as RequestInit).method).toBe("POST");
    expect(String((setCall?.[1] as RequestInit).body)).toContain('"enabled":false');
  });

  it("buys proxies after confirmation", async () => {
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
          message: "Куплено 1 прокси. Включите их в списке.",
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

    renderApp("/admin/ui/proxies");

    await screen.findByText(/250,50/);
    await waitFor(() => {
      expect(screen.getByLabelText("Страна")).toHaveValue("ru");
    });

    const submit = screen.getByRole("button", { name: "Купить" });
    await waitFor(() => expect(submit).toBeEnabled());
    await user.click(submit);

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/Прокси будут добавлены выключенными/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Купить" }));

    expect(
      await screen.findByText("Куплено 1 прокси. Включите их в списке."),
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

    renderApp("/admin/ui/keys");

    expect(
      await screen.findByRole("heading", { name: "Клиентские ключи", level: 1 }),
    ).toBeInTheDocument();
    expect(await screen.findByText("Default")).toBeInTheDocument();
    expect(screen.getByLabelText("Ключ доступа")).toBeInTheDocument();
    expect(
      screen.queryByRole("columnheader", { name: "Admin" }),
    ).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Администратор")).not.toBeInTheDocument();
  });

  it("redirects the legacy stats route to the statistics page", async () => {
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

    renderApp("/admin/ui/stats");

    expect(
      await screen.findByRole("heading", { name: "Статистика", level: 1 }),
    ).toBeInTheDocument();
    expect(
      await screen.findByRole("heading", { name: "Токены по дням" }),
    ).toBeInTheDocument();
    expect(screen.queryByText("Токенов за всё время")).not.toBeInTheDocument();
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

    renderApp("/admin/ui/keys");

    await screen.findByRole("heading", { name: "Клиентские ключи", level: 1 });
    await user.click(screen.getByRole("link", { name: "Статистика" }));

    expect(
      await screen.findByRole("heading", { name: "Статистика", level: 1 }),
    ).toBeInTheDocument();
    await waitFor(() => {
      expect(
        fetchMock.mock.calls.some(([url]) =>
          String(url).includes("/api/admin/stats?period=all"),
        ),
      ).toBe(true);
    });
  });
});
