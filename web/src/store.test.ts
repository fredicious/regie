import type { BoardView } from "./api/gen/BoardView";

const getBoard = vi.fn();
vi.mock("./api/client", () => ({ getBoard: (s: number) => getBoard(s), getSessions: vi.fn(), getTask: vi.fn() }));

let store: typeof import("./store");

const view = (id: number): BoardView => ({
  session: { id, name: `S${id}`, repos: [], paused: false },
  cards: [],
  running: 0,
  usage: [],
  paused_all: false,
});
const ev = (id: number) => ({ id, at: 0, session: 1, task: null, actor: "Regie", kind: { type: "TaskRetried" } }) as never;

beforeEach(async () => {
  vi.resetModules();
  store = await import("./store");
  vi.useFakeTimers();
  getBoard.mockReset();
  store.board.value = null;
  store.currentSession.value = 1;
});
afterEach(() => vi.useRealTimers());

test("many synchronous events cause one fetch now and one trailing fetch", async () => {
  getBoard.mockResolvedValue(view(1));
  for (let i = 1; i <= 50; i++) store.onEvent(ev(i));
  expect(getBoard).toHaveBeenCalledTimes(1);
  await vi.advanceTimersByTimeAsync(300);
  expect(getBoard).toHaveBeenCalledTimes(2);
  await vi.advanceTimersByTimeAsync(1000);
  expect(getBoard).toHaveBeenCalledTimes(2);
});

test("a board that arrives after a session switch is dropped", async () => {
  let resolve!: (v: BoardView) => void;
  getBoard.mockReturnValueOnce(new Promise((r) => (resolve = r)));
  store.refreshBoard();
  store.currentSession.value = 2;
  resolve(view(1));
  await vi.advanceTimersByTimeAsync(0);
  expect(store.board.value).toBeNull();
});
