import { signal } from "@preact/signals";
import { getBoard, getSessions, getTask } from "./api/client";
import { connect, type OutputLine } from "./api/events";
import type { BoardView } from "./api/gen/BoardView";
import type { Event } from "./api/gen/Event";
import type { Session } from "./api/gen/Session";
import type { TaskDetail } from "./api/gen/TaskDetail";

export const sessions = signal<Session[]>([]);
export const currentSession = signal<number | null>(null);
export const board = signal<BoardView | null>(null);
export const selectedTask = signal<number | null>(null);
export const taskDetail = signal<TaskDetail | null>(null);
export const liveLines = signal<Record<number, string[]>>({});
export const now = signal(Date.now());

const MAX_LINES = 200;
const THROTTLE_MS = 250;

export function pushLine(o: OutputLine) {
  const old = liveLines.value[o.task] ?? [];
  liveLines.value = { ...liveLines.value, [o.task]: [...old, o.line].slice(-MAX_LINES) };
}

let boardTimer: ReturnType<typeof setTimeout> | undefined;
let boardDirty = false;
let boardSeq = 0;

/** Refetches the board: one fetch now, then at most one more every 250 ms while events keep coming. */
export function refreshBoard() {
  if (boardTimer) {
    boardDirty = true;
    return;
  }
  boardTimer = setTimeout(() => {
    boardTimer = undefined;
    if (boardDirty) {
      boardDirty = false;
      refreshBoard();
    }
  }, THROTTLE_MS);
  const s = currentSession.value;
  if (s === null) return;
  const seq = ++boardSeq;
  getBoard(s).then(
    (b) => {
      // Drop answers for another session and answers overtaken by a newer fetch.
      if (currentSession.value === s && seq === boardSeq) board.value = b;
    },
    () => {
      /* the next event retries */
    },
  );
}

export async function refreshDetail() {
  const id = selectedTask.value;
  if (id === null) return;
  try {
    const d = await getTask(id);
    if (selectedTask.value === id) taskDetail.value = d;
  } catch {
    /* keep the old one */
  }
}

export async function refreshSessions() {
  try {
    sessions.value = await getSessions();
    const current = currentSession.value;
    if (current === null || !sessions.value.some((x) => x.id === current)) {
      // First load, or the selected session was deleted: the first one left, or none.
      if (sessions.value.length > 0) selectSession(sessions.value[0].id);
      else if (current !== null) {
        currentSession.value = null;
        board.value = null;
        selectTask(null);
      }
    }
  } catch {
    /* unauthorized is shown by the client */
  }
}

export function selectSession(id: number) {
  currentSession.value = id;
  board.value = null;
  selectTask(null);
  refreshBoard();
}

export function selectTask(id: number | null) {
  selectedTask.value = id;
  taskDetail.value = null;
  if (id !== null) void refreshDetail();
}

export function onEvent(e: Event) {
  if (e.kind.type.startsWith("Session")) void refreshSessions();
  if (e.session === null || e.session === currentSession.value) refreshBoard();
  if (e.task !== null && e.task === selectedTask.value) void refreshDetail();
}

export function start(): () => void {
  void refreshSessions();
  const tick = setInterval(() => (now.value = Date.now()), 30_000);
  const stop = connect(onEvent, pushLine);
  return () => {
    clearInterval(tick);
    stop();
  };
}
