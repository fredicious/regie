import type { Event } from "./gen/Event";

export type OutputLine = { task: number; run: number; line: string };

/** Subscribes to the event stream; returns a function that stops it. */
export function connect(onEvent: (e: Event) => void, onOutput: (o: OutputLine) => void): () => void {
  let last = 0;
  let delay = 2000;
  let es: EventSource | null = null;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let stopped = false;

  const open = () => {
    es = new EventSource(`/api/events?after=${last}`);
    es.onopen = () => {
      delay = 2000;
    };
    es.addEventListener("event", (m) => {
      const e = JSON.parse((m as MessageEvent).data) as Event;
      last = Math.max(last, e.id);
      onEvent(e);
    });
    es.addEventListener("output", (m) => onOutput(JSON.parse((m as MessageEvent).data)));
    es.onerror = () => {
      es?.close();
      if (stopped) return;
      timer = setTimeout(open, delay);
      delay = Math.min(delay * 2, 30000);
    };
  };
  open();
  return () => {
    stopped = true;
    clearTimeout(timer);
    es?.close();
  };
}
