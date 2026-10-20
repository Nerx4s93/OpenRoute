import { afterEach, describe, expect, it, vi } from "vitest";
import { api, ApiError, setCsrfToken } from "./client";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
  setCsrfToken("");
});

describe("api client", () => {
  it("posts the login password without a CSRF header", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({ authenticated: true, csrf: "csrf-1" }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await api.login("pw");

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    const headers = new Headers(init.headers);
    expect(init.method).toBe("POST");
    expect(headers.get("X-CSRF-Token")).toBeNull();
    expect(init.credentials).toBe("same-origin");
    expect(init.body).toBe(JSON.stringify({ password: "pw" }));
  });

  it("adds the CSRF token to mutations after login", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({ ok: true, message: "Прокси отключён" }),
    );
    vi.stubGlobal("fetch", fetchMock);
    setCsrfToken("csrf-1");

    await api.setProxy("proxy-id", false);

    const [path, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    const headers = new Headers(init.headers);
    expect(path).toBe("/api/admin/proxies/set");
    expect(headers.get("X-CSRF-Token")).toBe("csrf-1");
    expect(init.body).toBe(JSON.stringify({ proxy_id: "proxy-id", enabled: false }));
  });

  it("unwraps the gateway/openai error envelope", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        jsonResponse(
          { error: { message: "Прокси не найден", type: "invalid_request_error", code: 404 } },
          404,
        ),
      ),
    );

    await expect(api.setProxy("nope", true)).rejects.toMatchObject({
      status: 404,
      message: "Прокси не найден",
    } satisfies Partial<ApiError>);
  });

  it("reports a friendly network error", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("failed")));
    await expect(api.session()).rejects.toMatchObject({
      status: 0,
      message: expect.stringContaining("Сервер недоступен"),
    });
  });

  it("handles 204 responses", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(null, { status: 204 })));
    await expect(api.logout()).resolves.toBeUndefined();
  });
});
