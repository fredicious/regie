import { render, screen, fireEvent } from "@testing-library/preact";
import { TopBar } from "./TopBar";
import type { BoardView } from "../api/gen/BoardView";

const board = (paused_all: boolean): BoardView => ({
  session: { id: 1, name: "S", repos: [], paused: false },
  cards: [],
  running: 0,
  usage: [],
  paused_all,
});

test("fade button follows the board's paused_all", () => {
  const send = vi.fn();
  const props = { sessions: [], current: 1, now: 0, onSession: () => {}, send };
  const { rerender } = render(<TopBar {...props} board={board(false)} />);
  fireEvent.click(screen.getByRole("button", { name: /Fade to black/ }));
  expect(send).toHaveBeenCalledWith({ type: "PauseAll" });
  rerender(<TopBar {...props} board={board(true)} />);
  fireEvent.click(screen.getByRole("button", { name: /Fade in/ }));
  expect(send).toHaveBeenCalledWith({ type: "ResumeAll" });
});
