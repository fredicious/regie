import type { TaskCard } from "./api/gen/TaskCard";

export function card(over: Partial<TaskCard> & { id: number }): TaskCard {
  return {
    title: `Task ${over.id}`,
    lane: "Queued",
    flags: [],
    level: "Standard",
    model: null,
    tickets: [],
    pr: null,
    last_output: null,
    ...over,
  };
}
