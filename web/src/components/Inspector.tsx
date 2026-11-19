import { useState } from "preact/hooks";
import type { Command } from "../api/gen/Command";
import type { Level } from "../api/gen/Level";
import type { TaskDetail } from "../api/gen/TaskDetail";
import type { Event } from "../api/gen/Event";
import { askedText, eventBody, eventText, findingText, flagText, laneLabel, needsMe, tally, verifyText } from "../describe";

type Props = { detail: TaskDetail; lines: string[]; send: (c: Command) => void; onClose: () => void };

const NEXT: Record<Level, Level | null> = { Light: "Standard", Standard: "Hard", Hard: null };

/** Agent and tool text, always rendered as text (Preact escapes it), never as HTML. */
function Said({ text }: { text: string }) {
  return <pre class="said">{text}</pre>;
}

/** One review or verify report from the timeline. */
function Report({ e }: { e: Event }) {
  const k = e.kind;
  if (k.type === "ReviewReported") {
    return (
      <div class="report">
        <b>Round {k.round}</b>
        {"Unparsed" in k.outcome ? (
          <>
            <p class="muted">Régie could not read this review. What the reviewer wrote:</p>
            <Said text={k.outcome.Unparsed.raw} />
          </>
        ) : k.outcome.Findings.length === 0 ? (
          <p class="muted">No findings.</p>
        ) : (
          <ul class="findings">
            {k.outcome.Findings.map((f) => (
              <li key={f.id}>
                <Said text={findingText(f)} />
              </li>
            ))}
          </ul>
        )}
      </div>
    );
  }
  if (k.type === "VerifyReported") {
    return (
      <div class="report">
        <b>Verify</b>
        {"Unparsed" in k.outcome ? (
          <>
            <p class="muted">Régie could not read this answer. What the verify run wrote:</p>
            <Said text={k.outcome.Unparsed.raw} />
          </>
        ) : (
          <ul class="findings">
            {k.outcome.Results.map((r) => (
              <li key={r.finding}>
                <Said text={verifyText(r)} />
              </li>
            ))}
          </ul>
        )}
      </div>
    );
  }
  return null;
}

export function Inspector({ detail, lines, send, onClose }: Props) {
  const { card: c } = detail;
  const [reply, setReply] = useState("");
  const [confirming, setConfirming] = useState(false);
  const [closePr, setClosePr] = useState(false);
  const t = tally(c);
  const finished = c.lane === "Merged" || c.lane === "Cancelled";
  const next = finished ? null : NEXT[c.level];
  const asked = askedText(detail);
  const reports = detail.timeline.filter((e) => e.kind.type === "ReviewReported" || e.kind.type === "VerifyReported");
  const usage = new Map<number, string>();
  for (const e of detail.timeline) {
    if (e.kind.type === "RunUsage") {
      usage.set(e.kind.run, `${Math.round((e.kind.input_tokens + e.kind.output_tokens) / 1000)}k tokens · ${Math.round(e.kind.duration_ms / 60000)} min`);
    }
  }
  return (
    <aside class="drawer" aria-label={`Task T${c.id}`}>
      <div class="dh">
        <b>
          <i class={`tl ${t.colour}`} aria-hidden="true" /> T{c.id} {c.title}
        </b>
        <button type="button" class="x" aria-label="Close inspector" onClick={onClose}>
          ✕
        </button>
      </div>
      <div class="kv">
        <span>Lane</span>
        <span>
          {laneLabel(c.lane)}
          {c.flags.map((f) => (
            <span class="flag" key={flagText(f)}>
              {flagText(f)}
            </span>
          ))}
        </span>
        <span>Level</span>
        <span>
          {c.level.toLowerCase()}
          {c.model ? ` · ${c.model}` : ""}
        </span>
        {c.tickets.length > 0 && (
          <>
            <span>Tickets</span>
            <span>{c.tickets.join(", ")}</span>
          </>
        )}
        {detail.worktree && (
          <>
            <span>Branch</span>
            <span>{detail.worktree.branch}</span>
          </>
        )}
        {c.pr && (
          <>
            <span>PR</span>
            <span>
              {c.pr[1].startsWith("https:") ? (
                <a href={c.pr[1]} target="_blank" rel="noreferrer">
                  PR #{c.pr[0]}
                </a>
              ) : (
                `PR #${c.pr[0]}`
              )}
            </span>
          </>
        )}
      </div>

      <h3 class="h">Runs</h3>
      {detail.runs.length === 0 && <p class="muted">No run yet.</p>}
      <ul class="runs">
        {detail.runs.map((r) => (
          <li key={r.id}>
            {r.role} · {r.provider} {r.model} ·{" "}
            {r.outcome ? r.outcome.replace(/([a-z])([A-Z])/g, "$1 $2").toLowerCase() : "running"}
            {usage.has(r.id) && <span class="muted"> · {usage.get(r.id)}</span>}
          </li>
        ))}
      </ul>

      {reports.length > 0 && (
        <section aria-labelledby="review-h">
          <h3 class="h" id="review-h">
            Review
          </h3>
          {reports.map((e) => (
            <Report key={e.id} e={e} />
          ))}
        </section>
      )}

      <h3 class="h">Timeline</h3>
      <div class="tlx">
        {detail.timeline.map((e) => {
          const body = eventBody(e);
          return body === null ? (
            <div key={e.id}>{eventText(e)}</div>
          ) : (
            <details key={e.id}>
              <summary>{eventText(e)}</summary>
              <Said text={body} />
            </details>
          );
        })}
      </div>

      <h3 class="h">Live output</h3>
      <pre class="term" aria-label="Live output">
        {lines.length > 0 ? lines.join("\n") : "(nothing yet)"}
      </pre>

      {asked && (
        <section class="asked" aria-labelledby="asked-h">
          <h3 class="h" id="asked-h">
            What you are asked
          </h3>
          <p class="muted">{asked.title}</p>
          <Said text={asked.text} />
        </section>
      )}

      {needsMe(c.flags) && (
        <div class="reply">
          <label>
            <span class="h">Reply to the agent</span>
            <textarea
              aria-label="Reply to the agent"
              value={reply}
              onInput={(e) => setReply(e.currentTarget.value)}
            />
          </label>
          <button
            type="button"
            class="btn"
            disabled={reply.trim() === ""}
            onClick={() => {
              send({ type: "MessageAgent", task: c.id, text: reply, level: null });
              setReply("");
            }}
          >
            Send reply
          </button>
        </div>
      )}

      <div class="actions">
        <button type="button" class="btn" onClick={() => send({ type: "StopAgent", task: c.id })}>
          Stop agent
        </button>
        <button type="button" class="btn" onClick={() => send({ type: "RetryTask", task: c.id })}>
          Retry
        </button>
        {next && (
          <button
            type="button"
            class="btn"
            onClick={() => send({ type: "RaiseLevel", task: c.id, level: next })}
          >
            Raise level
          </button>
        )}
        <button type="button" class="btn danger" onClick={() => setConfirming(true)}>
          Cancel task
        </button>
      </div>
      {confirming && (
        <div class="confirm" role="alertdialog" aria-label="Confirm cancel">
          <p>Cancel T{c.id}? The agent stops and the worktree is cleaned.</p>
          {c.pr && (
            <label>
              <input type="checkbox" checked={closePr} onChange={(e) => setClosePr(e.currentTarget.checked)} /> Also close the PR
            </label>
          )}
          <button
            type="button"
            class="btn danger"
            onClick={() => {
              send({ type: "CancelTask", task: c.id, close_pr: closePr });
              setConfirming(false);
            }}
          >
            Confirm cancel
          </button>
          <button type="button" class="btn" onClick={() => setConfirming(false)}>
            Keep task
          </button>
        </div>
      )}
    </aside>
  );
}
