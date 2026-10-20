import type {
  ApiMessage,
  KeyAddPayload,
  KeysSnapshot,
  Period,
  ProxiesSnapshot,
  SessionInfo,
} from "./types";

let csrfToken = "";

export function setCsrfToken(token: string): void {
  csrfToken = token;
}

export class ApiError extends Error {
  readonly status: number;
  readonly retryAfter?: number;

  constructor(status: number, message: string, retryAfter?: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.retryAfter = retryAfter;
  }
}

function messageFromPayload(payload: unknown): string | null {
  if (!payload || typeof payload !== "object") return null;
  const body = payload as {
    detail?: unknown;
    message?: unknown;
    error?: { message?: unknown };
  };
  if (typeof body.detail === "string" && body.detail.trim()) return body.detail;
  if (Array.isArray(body.detail)) {
    const parts = body.detail
      .map((item) => (item && typeof item === "object" && "msg" in item ? String(item.msg) : ""))
      .filter(Boolean);
    if (parts.length) return parts.join("; ");
  }
  if (typeof body.error?.message === "string" && body.error.message.trim()) {
    return body.error.message;
  }
  if (typeof body.message === "string" && body.message.trim()) return body.message;
  return null;
}

async function responseError(response: Response): Promise<string> {
  const text = await response.text().catch(() => "");
  if (text) {
    try {
      const message = messageFromPayload(JSON.parse(text));
      if (message) return message;
    } catch {
      // not JSON — use the raw body below
    }
    if (!/<html[\s>]/i.test(text)) return text.slice(0, 300);
  }
  return `Ошибка запроса (${response.status})`;
}

export async function apiRequest<T>(path: string, options: RequestInit = {}): Promise<T> {
  const method = (options.method ?? "GET").toUpperCase();
  const headers = new Headers(options.headers);
  if (options.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  if (method !== "GET" && method !== "HEAD" && csrfToken) {
    headers.set("X-CSRF-Token", csrfToken);
  }

  let response: Response;
  try {
    response = await fetch(path, {
      credentials: "same-origin",
      cache: "no-store",
      ...options,
      headers,
    });
  } catch {
    throw new ApiError(0, "Сервер недоступен. Проверьте соединение и повторите попытку.");
  }

  if (!response.ok) {
    const retryAfter = Number(response.headers.get("Retry-After") ?? "") || undefined;
    if (response.status === 401 && path !== "/api/admin/login") {
      window.dispatchEvent(new Event("openroute:unauthorized"));
    }
    throw new ApiError(response.status, await responseError(response), retryAfter);
  }

  if (response.status === 204) return undefined as T;
  try {
    return (await response.json()) as T;
  } catch {
    throw new ApiError(response.status, "Сервер вернул некорректный ответ");
  }
}

function jsonBody(value: unknown): string {
  return JSON.stringify(value);
}

export const api = {
  session: () => apiRequest<SessionInfo>("/api/admin/session"),

  login: (password: string) =>
    apiRequest<SessionInfo>("/api/admin/login", {
      method: "POST",
      body: jsonBody({ password }),
    }),

  logout: () => apiRequest<void>("/api/admin/logout", { method: "POST" }),

  proxies: (period: Period) =>
    apiRequest<ProxiesSnapshot>(`/api/admin/proxies?period=${period}`),

  setProxy: (proxyId: string, enabled: boolean) =>
    apiRequest<ApiMessage>("/api/admin/proxies/set", {
      method: "POST",
      body: jsonBody({ proxy_id: proxyId, enabled }),
    }),

  refreshProxies: () =>
    apiRequest<ApiMessage>("/api/admin/proxies/refresh", { method: "POST" }),

  keys: (period: Period) =>
    apiRequest<KeysSnapshot>(`/api/admin/keys?period=${period}`),

  addKey: (payload: KeyAddPayload) =>
    apiRequest<ApiMessage>("/api/admin/keys/add", {
      method: "POST",
      body: jsonBody(payload),
    }),

  setKey: (keyHash: string, enabled: boolean) =>
    apiRequest<ApiMessage>("/api/admin/keys/set", {
      method: "POST",
      body: jsonBody({ key_hash: keyHash, enabled }),
    }),

  deleteKey: (keyHash: string) =>
    apiRequest<ApiMessage>("/api/admin/keys/delete", {
      method: "POST",
      body: jsonBody({ key_hash: keyHash }),
    }),
};
