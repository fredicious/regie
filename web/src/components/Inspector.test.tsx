import { render, screen, fireEvent } from "@testing-library/preact";
import { Inspector } from "./Inspector";
import { card } from "../testdata";
import type { TaskDetail } from "../api/gen/TaskDetail";

function detail(flags: TaskDetail["card"]["flags"]): TaskDetail {
  return { card: card({ id: 7, title: "Fix dates", lane: "Working", flags, tickets: ["ABC-1"] }), instruction: "do it", worktree: null, runs: [], timeline: [], reviews: [] };
}

test("reply box appears only for needs-me cards and sends MessageAgent", () => {
  const send = vi.fn();
  const { rerender } = render(<Inspector detail={detail([])} lines={[]} send={send} onClose={() => {}} />);
  expect(screen.queryByLabelText("Reply to the agent")).toBeNull();

  rerender(<Inspector detail={detail([{ NeedsMe: "BuilderQuestion" }])} lines={[]} send={send} onClose={() => {}} />);
  fireEvent.input(screen.getByLabelText("Reply to the agent"), { target: { value: "exclude archived" } });
  fireEvent.click(screen.getByRole("button", { name: "Send reply" }));
  expect(send).toHaveBeenCalledWith({ type: "MessageAgent", task: 7, text: "exclude archived", level: null });
});

test("cancel asks for confirmation and can also close the PR", () => {
  const send = vi.fn();
  const d = detail([]);
  d.card.pr = [5, "http://pr"];
  render(<Inspector detail={d} lines={[]} send={send} onClose={() => {}} />);
  fireEvent.click(screen.getByRole("button", { name: "Cancel task" }));
  expect(send).not.toHaveBeenCalled();
  fireEvent.click(screen.getByLabelText("Also close the PR"));
  fireEvent.click(screen.getByRole("button", { name: "Confirm cancel" }));
  expect(send).toHaveBeenCalledWith({ type: "CancelTask", task: 7, close_pr: true });
});

test("raise level sends RaiseLevel, and is hidden on finished cards", () => {
  const send = vi.fn();
  const { rerender } = render(<Inspector detail={detail([])} lines={[]} send={send} onClose={() => {}} />);
  fireEvent.click(screen.getByRole("button", { name: "Raise level" }));
  expect(send).toHaveBeenCalledWith({ type: "RaiseLevel", task: 7, level: "Hard" });
  const done = detail([]);
  done.card.lane = "Merged";
  rerender(<Inspector detail={done} lines={[]} send={send} onClose={() => {}} />);
  expect(screen.queryByRole("button", { name: "Raise level" })).toBeNull();
});

test("only https PR links are rendered as links", () => {
  const d = detail([]);
  d.card.pr = [5, "javascript:alert(1)"];
  render(<Inspector detail={d} lines={[]} send={() => {}} onClose={() => {}} />);
  expect(document.querySelector("a")).toBeNull();
  expect(screen.getByText("PR #5")).toBeTruthy();
});
