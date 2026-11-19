import { useEffect, useState } from "preact/hooks";
import { send, unauthorized } from "../api/client";
import type { Command } from "../api/gen/Command";
import * as s from "../store";
import { Board } from "./Board";
import { Inspector } from "./Inspector";
import { TopBar } from "./TopBar";

export function App() {
  const [error, setError] = useState<string | null>(null);
  useEffect(() => s.start(), []);

  if (unauthorized.value) {
    return (
      <main class="gate">
        <p>
          Open Régie from the <code>regie</code> command.
        </p>
      </main>
    );
  }

  const run = async (cmd: Command) => {
    try {
      await send(cmd);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const detail = s.taskDetail.value;
  const sel = s.selectedTask.value;
  return (
    <div class="app">
      <TopBar
        sessions={s.sessions.value}
        current={s.currentSession.value}
        board={s.board.value}
        now={s.now.value}
        onSession={s.selectSession}
        send={run}
      />
      {error && (
        <div class="error" role="alert">
          {error}{" "}
          <button type="button" class="x" aria-label="Dismiss" onClick={() => setError(null)}>
            ✕
          </button>
        </div>
      )}
      <div class="body">
        <main class="center">
          {s.board.value ? (
            <Board cards={s.board.value.cards} selected={sel} onSelect={s.selectTask} />
          ) : (
            <p class="muted">{s.sessions.value.length === 0 ? "No studio yet. Create one with the regie command." : "Loading…"}</p>
          )}
        </main>
        {sel !== null && detail && (
          <Inspector
            detail={detail}
            lines={s.liveLines.value[sel] ?? []}
            send={run}
            onClose={() => s.selectTask(null)}
          />
        )}
      </div>
    </div>
  );
}
