import { connect } from "./events";

class FakeES {
  static all: FakeES[] = [];
  listeners: Record<string, (e: MessageEvent) => void> = {};
  onerror: (() => void) | null = null;
  closed = false;
  constructor(public url: string) {
    FakeES.all.push(this);
  }
  addEventListener(n: string, f: (e: MessageEvent) => void) {
    this.listeners[n] = f;
  }
  close() {
    this.closed = true;
  }
  emit(n: string, data: unknown, id?: string) {
    this.listeners[n]({ data: JSON.stringify(data), lastEventId: id ?? "" } as MessageEvent);
  }
}

beforeEach(() => {
  vi.useFakeTimers();
  FakeES.all = [];
  vi.stubGlobal("EventSource", FakeES);
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

test("events reconnect with the last id", () => {
  const seen: number[] = [];
  const stop = connect((e) => seen.push(e.id), () => {});
  expect(FakeES.all[0].url).toBe("/api/events?after=0");
  FakeES.all[0].emit("event", { id: 41 }, "41");
  FakeES.all[0].emit("event", { id: 42 }, "42");
  FakeES.all[0].onerror!();
  expect(FakeES.all[0].closed).toBe(true);
  vi.advanceTimersByTime(2000);
  expect(FakeES.all).toHaveLength(2);
  expect(FakeES.all[1].url).toBe("/api/events?after=42");
  expect(seen).toEqual([41, 42]);
  stop();
});

test("reconnect backs off up to 30 seconds and output lines are forwarded", () => {
  const lines: string[] = [];
  connect(() => {}, (o) => lines.push(o.line));
  FakeES.all[0].emit("output", { task: 1, run: 2, line: "hi" });
  expect(lines).toEqual(["hi"]);
  const delays = [2000, 4000, 8000, 16000, 30000, 30000];
  delays.forEach((d, i) => {
    FakeES.all[i].onerror!();
    vi.advanceTimersByTime(d - 1);
    expect(FakeES.all).toHaveLength(i + 1);
    vi.advanceTimersByTime(1);
    expect(FakeES.all).toHaveLength(i + 2);
  });
});
