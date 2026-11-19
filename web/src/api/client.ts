import { signal } from "@preact/signals";
import type { BoardView } from "./gen/BoardView";
import type { Command } from "./gen/Command";
import type { Session } from "./gen/Session";
import type { TaskDetail } from "./gen/TaskDetail";

/** Set when the server answers 401: the page then asks to be opened from the CLI. */
export const unauthorized = signal(false);

export class ApiError extends Error {}

async function call(url: string, init?: RequestInit): Promise<Response> {
  const res = await fetch(url, { credentials: "same-origin", ...init });
  if (res.status === 401) {
    unauthorized.value = true;
    throw new ApiError("unauthorized");
  }
  if (!res.ok) {
    let msg = `${res.status}`;
    try {
      const body = await res.json();
      msg = typeof body.error === "string" ? body.error : JSON.stringify(body.error);
    } catch {
      /* keep the status */
    }
    throw new ApiError(msg);
  }
  return res;
}

export const getSessions = async (): Promise<Session[]> => (await call("/api/sessions")).json();
export const getBoard = async (session: number): Promise<BoardView> =>
  (await call(`/api/board?session=${session}`)).json();
export const getTask = async (id: number): Promise<TaskDetail> => (await call(`/api/tasks/${id}`)).json();
export const getOutput = async (run: number, lines: number): Promise<string> =>
  (await call(`/api/runs/${run}/output?lines=${lines}`)).text();

export async function send(cmd: Command): Promise<unknown> {
  const res = await call("/api/commands", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(cmd),
  });
  return (await res.json()).result;
}
