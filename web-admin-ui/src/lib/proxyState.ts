import type { KeyState, ProxyRow } from "../api/types";
import { formatDuration } from "./format";

export type Tone = "ok" | "warn" | "err" | "off";

export interface ProxyState {
  key: string;
  label: string;
  tone: Tone;
  dead: boolean;
}

export const TONE_CLASS: Record<Tone, string> = {
  ok: "b-ok",
  warn: "b-warn",
  err: "b-err",
  off: "b-off",
};

export function proxyState(proxy: ProxyRow): ProxyState {
  // A permanent abusive-traffic ban is the strongest state: even if an
  // operator later disables the row, the panel must keep showing «бан».
  if (proxy.banned) {
    return { key: "ban", label: "бан", tone: "err", dead: false };
  }
  if (proxy.disabled) {
    return { key: "dead", label: "мёртв", tone: "err", dead: true };
  }
  if (proxy.manual) {
    return { key: "off", label: "выключен", tone: "off", dead: true };
  }
  if (!proxy.enabled) {
    return { key: "noproxy", label: "нет в пуле", tone: "warn", dead: false };
  }
  if (proxy.banned_seconds_left > 0) {
    return {
      key: "ban",
      label: `бан ${formatDuration(proxy.banned_seconds_left)}`,
      tone: "err",
      dead: false,
    };
  }
  if (proxy.quota_seconds_left > 0) {
    return {
      key: "quota",
      label: `лимит ${formatDuration(proxy.quota_seconds_left)}`,
      tone: "warn",
      dead: false,
    };
  }
  if (proxy.cooldown_seconds_left > 0) {
    return {
      key: "cooldown",
      label: `cooldown ${Math.round(proxy.cooldown_seconds_left)}s`,
      tone: "warn",
      dead: false,
    };
  }
  if (proxy.rpm_capped) {
    return { key: "rpm", label: "RPM-кап", tone: "warn", dead: false };
  }
  return { key: "ok", label: "ok", tone: "ok", dead: false };
}

export function keyStateLabel(state: KeyState): { label: string; tone: Tone } {
  switch (state) {
    case "active":
      return { label: "активен", tone: "ok" };
    case "revoked":
      return { label: "отозван", tone: "off" };
    case "deleted":
      return { label: "удалён", tone: "err" };
  }
}
