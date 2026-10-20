import { TONE_CLASS, type Tone } from "../lib/proxyState";

export function StatusBadge({ label, tone }: { label: string; tone: Tone }) {
  return <span className={`badge ${TONE_CLASS[tone]}`}>{label}</span>;
}
