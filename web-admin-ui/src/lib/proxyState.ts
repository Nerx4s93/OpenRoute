import type { KeyState, ProxyRow } from "../api/types";
import { translate, type Language } from "../i18n";
import { formatDuration } from "./format";

export type Tone = "ok" | "warn" | "err" | "off";

export interface ProxyState {
  key: string;
  label: string;
  tone: Tone;
}

export const TONE_CLASS: Record<Tone, string> = {
  ok: "b-ok",
  warn: "b-warn",
  err: "b-err",
  off: "b-off",
};

export function proxyState(proxy: ProxyRow, language: Language = "en"): ProxyState {
  // A permanent abusive-traffic ban is the strongest state: even if an
  // operator later disables the row, the panel must keep showing "banned".
  if (proxy.banned) {
    return {
      key: "ban",
      label: translate(language, "proxyState.banned"),
      tone: "err",
    };
  }
  if (proxy.manual) {
    return {
      key: "off",
      label: translate(language, "proxyState.disabled"),
      tone: "off",
    };
  }
  if (!proxy.enabled) {
    return {
      key: "noproxy",
      label: translate(language, "proxyState.notInPool"),
      tone: "warn",
    };
  }
  if (proxy.quota_seconds_left > 0) {
    return {
      key: "quota",
      label: translate(language, "proxyState.quota", {
        duration: formatDuration(proxy.quota_seconds_left, language),
      }),
      tone: "warn",
    };
  }
  if (proxy.cooldown_seconds_left > 0) {
    return {
      key: "cooldown",
      label: translate(language, "proxyState.cooldown", {
        seconds: Math.round(proxy.cooldown_seconds_left),
      }),
      tone: "warn",
    };
  }
  if (proxy.rpm_capped) {
    return {
      key: "rpm",
      label: translate(language, "proxyState.rpmCapped"),
      tone: "warn",
    };
  }
  return {
    key: "ok",
    label: translate(language, "proxyState.ok"),
    tone: "ok",
  };
}

export function keyStateLabel(
  state: KeyState,
  language: Language = "en",
): { label: string; tone: Tone } {
  switch (state) {
    case "active":
      return { label: translate(language, "keyState.active"), tone: "ok" };
    case "revoked":
      return { label: translate(language, "keyState.revoked"), tone: "off" };
    case "deleted":
      return { label: translate(language, "keyState.deleted"), tone: "err" };
  }
}
