import { describe, expect, it } from "vitest";
import type { ProxyRow } from "../api/types";
import { keyStateLabel, proxyState } from "./proxyState";

function row(overrides: Partial<ProxyRow> = {}): ProxyRow {
  return {
    id: "id",
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
    usage: { requests: 0, errors: 0, tokens: 0 },
    ...overrides,
  };
}

describe("proxyState precedence", () => {
  it("permanent ban wins over a manual disable", () => {
    const state = proxyState(row({ banned: true, manual: true }));
    expect(state).toMatchObject({ key: "ban", label: "бан", tone: "err" });
  });

  it("distinguishes dead, manual and not-in-pool proxies", () => {
    expect(proxyState(row({ disabled: true }))).toMatchObject({
      key: "dead",
      label: "мёртв",
      dead: true,
    });
    expect(proxyState(row({ manual: true }))).toMatchObject({
      key: "off",
      label: "выключен",
      dead: true,
    });
    expect(proxyState(row({ enabled: false }))).toMatchObject({
      key: "noproxy",
      label: "нет в пуле",
      tone: "warn",
    });
  });

  it("reports time-based and rpm states", () => {
    expect(proxyState(row({ banned_seconds_left: 125 })).label).toBe("бан 2м");
    expect(proxyState(row({ quota_seconds_left: 7200 })).label).toBe("лимит 2ч 00м");
    expect(proxyState(row({ cooldown_seconds_left: 12.4 })).label).toBe("cooldown 12s");
    expect(proxyState(row({ rpm_capped: true }))).toMatchObject({
      key: "rpm",
      label: "RPM-кап",
    });
    expect(proxyState(row())).toMatchObject({ key: "ok", label: "ok", tone: "ok" });
  });
});

describe("keyStateLabel", () => {
  it("maps key states to badges", () => {
    expect(keyStateLabel("active")).toEqual({ label: "активен", tone: "ok" });
    expect(keyStateLabel("revoked")).toEqual({ label: "отозван", tone: "off" });
    expect(keyStateLabel("deleted")).toEqual({ label: "удалён", tone: "err" });
  });
});
