import { ago } from "../describe";

type W = { used: number; resets_at: number };
const WEEK = 7 * 86_400_000;
const ZONE_FROM = 0.75;

type Props = { label: string; kind: "5h" | "7d"; window: W; readAt: number; now: number; reason: string };

export function Meter({ label, kind, window: w, readAt, now, reason }: Props) {
  const used = Math.min(1, Math.max(0, w.used));
  const pace = Math.min(1, Math.max(0, 1 - (w.resets_at - now) / WEEK));
  const pct = Math.round(used * 100);
  const hot = kind === "5h" && used >= ZONE_FROM;
  return (
    <div class="meter" title={`${label}: read ${ago(readAt, now)}. ${reason}`}>
      <div class="meter-lbl">
        <span>{label}</span>
        <span class={hot ? "hot" : ""}>{pct}%{hot ? " (held)" : ""}</span>
      </div>
      <div class="bar" role="meter" aria-label={label} aria-valuemin={0} aria-valuemax={100} aria-valuenow={pct}>
        <div class="fill" style={{ width: `${pct}%` }} />
        {kind === "5h" && <div class="zone" data-zone={ZONE_FROM} style={{ left: `${ZONE_FROM * 100}%` }} />}
        {kind === "7d" && <div class="pace" data-pace={pace} style={{ left: `${pace * 100}%` }} title="Share of the week elapsed" />}
      </div>
    </div>
  );
}
