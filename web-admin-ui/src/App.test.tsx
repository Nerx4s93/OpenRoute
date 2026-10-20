import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";
import { AuthProvider } from "./auth/AuthProvider";
import { ToastProvider } from "./components/Toast";
import type { KeysSnapshot, ProxiesSnapshot } from "./api/types";

const proxies: ProxiesSnapshot = {
  server_time: "2026-10-20T10:00:00+00:00",
  summary: { requests: 12, errors: 1, tokens: 340, proxies_ready: 1, proxies_total: 1 },
  proxies: [
    {
      id: "proxy-1",
      display: "10.0.0.1:8080",
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

const keys: KeysSnapshot = {
  server_time: "2026-10-20T10:00:00+00:00",
  keys: [
    {
      key_hash: "hash-1",
      key_id: "default",
      name: "Default",
      admin: true,
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

  it("renders the proxies page for an authenticated session", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/api/admin/session")) {
          return json({ authenticated: true, csrf: "csrf-1", expires_at: 9_999_999_999 });
        }
        if (url.includes("/api/admin/proxies")) return json(proxies);
        return json({});
      }),
    );

    renderApp("/admin/ui");

    expect(
      await screen.findByRole("heading", { name: "Пул прокси", level: 1 }),
    ).toBeInTheDocument();
    expect(await screen.findByText("10.0.0.1:8080")).toBeInTheDocument();
    expect(screen.getByText("Прокси в ротации")).toBeInTheDocument();
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
  });
});
