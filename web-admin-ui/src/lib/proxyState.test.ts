import { describe, expect, it } from "vitest";
import type { ProxyRow } from "../api/types";
import { keyStateLabel, proxyState } from "./proxyState";

function row(overrides: Partial<ProxyRow> = {}): ProxyRow {
  return {
    id: "id",
    display: "10.0.0.1:8080",
    enabled: true,
    manual: false,
    banned: false,
    quota_seconds_left: 0,
    cooldown_seconds_left: 0,
    rpm_capped: false,
    last_error: null,
    usage: { requests: 0, errors: 0, tokens: 0 },
    ...overrides,
  };
}

describe("proxyState precedence", () => {
  it("permanent ban wins over a manual disable", () => {
    const state = proxyState(row({ banned: true, manual: true }), "en");
    expect(state).toMatchObject({ key: "ban", label: "banned", tone: "err" });
  });

  it("distinguishes manual and not-in-pool proxies", () => {
    expect(proxyState(row({ manual: true }), "en")).toMatchObject({
      key: "off",
      label: "disabled",
    });
    expect(proxyState(row({ enabled: false }), "en")).toMatchObject({
      key: "noproxy",
      label: "not in pool",
      tone: "warn",
    });
  });

  it("reports time-based and rpm states", () => {
    expect(proxyState(row({ quota_seconds_left: 7200 }), "en").label).toBe(
      "limit 2h 00m",
    );
    expect(proxyState(row({ cooldown_seconds_left: 12.4 }), "en").label).toBe(
      "cooldown 12s",
    );
    expect(proxyState(row({ rpm_capped: true }), "en")).toMatchObject({
      key: "rpm",
      label: "RPM capped",
    });
    expect(proxyState(row(), "en")).toMatchObject({
      key: "ok",
      label: "ok",
      tone: "ok",
    });
  });

  it("localizes labels into Russian", () => {
    expect(proxyState(row({ banned: true }), "ru").label).toBe("бан");
    expect(proxyState(row({ quota_seconds_left: 125 }), "ru").label).toBe(
      "лимит 2м",
    );
  });
});

describe("keyStateLabel", () => {
  it("maps key states to English badges", () => {
    expect(keyStateLabel("active", "en")).toEqual({ label: "active", tone: "ok" });
    expect(keyStateLabel("revoked", "en")).toEqual({
      label: "revoked",
      tone: "off",
    });
    expect(keyStateLabel("deleted", "en")).toEqual({
      label: "deleted",
      tone: "err",
    });
  });

  it("maps key states to Russian badges", () => {
    expect(keyStateLabel("active", "ru")).toEqual({ label: "активен", tone: "ok" });
    expect(keyStateLabel("revoked", "ru")).toEqual({
      label: "отозван",
      tone: "off",
    });
    expect(keyStateLabel("deleted", "ru")).toEqual({
      label: "удалён",
      tone: "err",
    });
  });
});
