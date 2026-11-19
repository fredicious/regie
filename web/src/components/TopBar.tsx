import type { BoardView } from "../api/gen/BoardView";
import type { Command } from "../api/gen/Command";
import type { Session } from "../api/gen/Session";
import { opennessText } from "../describe";
import { Meter } from "./Meter";

type Props = {
  sessions: Session[];
  current: number | null;
  board: BoardView | null;
  pausedAll: boolean;
  now: number;
  onSession: (id: number) => void;
  send: (c: Command) => void;
};

export function TopBar({ sessions, current, board, pausedAll, now, onSession, send }: Props) {
  const running = board?.running ?? 0;
  const session = board?.session;
  return (
    <header class="top">
      <span class="logo">RÉGIE</span>
      <label class="studio">
        <span>Studio</span>
        <select value={current ?? ""} onChange={(e) => onSession(Number(e.currentTarget.value))}>
          {sessions.map((s) => (
            <option key={s.id} value={s.id}>
              {s.name}
            </option>
          ))}
        </select>
      </label>
      <span class={`onair${running > 0 ? " lit" : ""}`} role="status">
        {running > 0 ? `ON AIR · ${running}` : "OFF AIR"}
      </span>
      <div class="vu">
        {board?.usage.map((u) =>
          u.reading ? (
            <span class="vu-pair" key={u.provider}>
              <Meter label={`${u.provider} 5h`} kind="5h" window={u.reading.five_hour} readAt={u.reading.read_at} now={now} reason={opennessText(u.openness)} />
              <Meter label={`${u.provider} 7d`} kind="7d" window={u.reading.seven_day} readAt={u.reading.read_at} now={now} reason={opennessText(u.openness)} />
            </span>
          ) : (
            <span class="meter none" key={u.provider} title={opennessText(u.openness)}>
              {u.provider}: no reading
            </span>
          ),
        )}
      </div>
      {session &&
        (session.paused ? (
          <button type="button" class="btn" onClick={() => send({ type: "ResumeSession", session: session.id })}>
            ▶ Resume
          </button>
        ) : (
          <button type="button" class="btn" onClick={() => send({ type: "PauseSession", session: session.id })}>
            ⏸ Standby
          </button>
        ))}
      {pausedAll ? (
        <button type="button" class="btn fade on" onClick={() => send({ type: "ResumeAll" })}>
          ▶ Fade in
        </button>
      ) : (
        <button type="button" class="btn fade" onClick={() => send({ type: "PauseAll" })}>
          ■ Fade to black
        </button>
      )}
    </header>
  );
}
