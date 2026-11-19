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
