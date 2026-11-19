import type { Event } from "./api/gen/Event";
import type { Flag } from "./api/gen/Flag";
import type { Lane } from "./api/gen/Lane";
import type { Openness } from "./api/gen/Openness";
import type { TaskCard } from "./api/gen/TaskCard";

export const LANES: { lane: Lane; label: string }[] = [
  { lane: "Queued", label: "Queued" },
  { lane: "Working", label: "Working" },
  { lane: "InReview", label: "In review" },
  { lane: "Ci", label: "CI" },
  { lane: "ReadyToMerge", label: "Ready" },
  { lane: "Merged", label: "Merged" },
  { lane: "Cancelled", label: "Cancelled" },
];

export const laneLabel = (l: Lane) => LANES.find((x) => x.lane === l)!.label;

export type Tally = { label: "ON AIR" | "READY" | "NEEDS ME" | "OFF"; colour: "red" | "green" | "amber" | "off" };

export const needsMe = (flags: Flag[]) => flags.some((f) => typeof f === "object" && "NeedsMe" in f);

/** A card has an active run when it works or is in review and nothing holds it. */
export function tally(c: TaskCard): Tally {
  if (needsMe(c.flags)) return { label: "NEEDS ME", colour: "amber" };
  if (c.lane === "ReadyToMerge") return { label: "READY", colour: "green" };
  if ((c.lane === "Working" || c.lane === "InReview") && c.flags.length === 0) return { label: "ON AIR", colour: "red" };
  return { label: "OFF", colour: "off" };
}

export function flagText(f: Flag): string {
  if (f === "Paused") return "Paused";
  if ("NeedsMe" in f) {
    const r = f.NeedsMe;
    return `Needs me: ${typeof r === "string" ? words(r) : words(Object.keys(r)[0])}`;
  }
  return `Waiting: ${f.Waiting.toLowerCase()}`;
}

const words = (s: string) => s.replace(/([a-z])([A-Z])/g, "$1 $2").toLowerCase();

export function opennessText(o: Openness): string {
  if (o === "Open") return "Open: new work may start";
  if ("Threshold" in o) {
    const w = o.Threshold.window === "FiveHour" ? "5h window" : "weekly pace";
    return `Held: ${w} at ${Math.round(o.Threshold.used * 100)}%`;
  }
  return `Blocked until ${new Date(o.Blocked.until).toLocaleString()}`;
}

export function ago(from: number, now: number): string {
  const min = Math.max(0, Math.round((now - from) / 60_000));
  if (min < 1) return "just now";
  if (min < 60) return `${min} min ago`;
  const h = Math.round(min / 60);
  return h < 48 ? `${h} h ago` : `${Math.round(h / 24)} d ago`;
}

/** One plain-words line per event. */
export function eventText(e: Event): string {
  const k = e.kind;
  switch (k.type) {
    case "TaskCreated": return `Task created: ${k.title}`;
    case "LevelRaised": return `Level raised to ${k.level}`;
    case "WorktreeCreated": return `Worktree ready on ${k.branch}`;
    case "SetupFinished": return k.ok ? "Setup finished" : "Setup failed";
    case "RunStarted": return `${k.role} started on ${k.provider} (${k.model})`;
    case "RunSpawned": return `Process ${k.pid} started`;
    case "RunAttached": return "Agent session attached";
    case "RunFinished": return `Run ended: ${words(k.outcome)}`;
    case "RunUsage": return `Used ${Math.round((k.input_tokens + k.output_tokens) / 1000)}k tokens in ${Math.round(k.duration_ms / 60000)} min`;
    case "MessageQueued": return `You said: ${k.text}`;
    case "MessageDelivered": return "Message delivered to the agent";
    case "ReviewReported": return `Review round ${k.round} reported`;
    case "FindingsRuled": return `Findings ruled (round ${k.round})`;
    case "VerifyReported": return "Checks reported";
    case "PrObserved": return `PR #${k.pr.number} seen (${words(k.pr.lifecycle)})`;
    case "PrMissing": return "PR not found";
    case "PrCommentObserved": return "PR comment seen";
    case "TaskCancelled": return k.close_pr ? "Task cancelled, PR closed" : "Task cancelled";
    case "TaskRetried": return "Task retried";
    case "PrClosedByRegie": return "PR closed by Régie";
    case "WorktreeCleaned": return "Worktree cleaned";
    case "SessionPaused": return "Studio on standby";
    case "SessionResumed": return "Studio resumed";
    case "AllPaused": return "Faded to black";
    case "AllResumed": return "Faded in";
    case "LimitHit": return `${k.provider} limit hit`;
    default: return words(k.type);
  }
}
