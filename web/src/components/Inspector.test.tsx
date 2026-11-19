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

let nextId = 1;
function ev(kind: TaskDetail["timeline"][number]["kind"]): TaskDetail["timeline"][number] {
  return { id: nextId++, at: 0, session: 1, task: 7, actor: "Regie", kind };
}

function run(id: number, role: "Builder" | "Reviewer" | "Verify", final_message: string | null): TaskDetail["runs"][number] {
  return {
    id, task: 7, role, purpose: "Initial", provider: "Claude", model: "claude-opus-5-5", effort: "Medium",
    native_session: null, pid: null, started_at: 0, outcome: "Finished", final_message,
  };
}

test("the builder's question is shown above the reply box", () => {
  const d = detail([{ NeedsMe: "BuilderQuestion" }]);
  d.runs = [run(1, "Builder", "Earlier words."), run(2, "Builder", "Should archived rows be exported?\n\nStatus: question")];
  d.timeline = [ev({ type: "RunFinished", run: 2, outcome: "Finished", final_message: d.runs[1].final_message })];
  render(<Inspector detail={d} lines={[]} send={() => {}} onClose={() => {}} />);
  const asked = screen.getByRole("region", { name: "What you are asked" });
  expect(asked.textContent).toContain("Should archived rows be exported?");
  expect(asked.textContent).not.toContain("Earlier words.");
  // it comes before the reply box
  const reply = screen.getByLabelText("Reply to the agent");
  expect(asked.compareDocumentPosition(reply) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
});

test("a failed setup shows its output, in the timeline and above the reply box", () => {
  const d = detail([{ NeedsMe: "SetupFailed" }]);
  d.timeline = [ev({ type: "SetupFinished", ok: false, output_tail: "npm ERR! missing script: setup" })];
  render(<Inspector detail={d} lines={[]} send={() => {}} onClose={() => {}} />);
  expect(screen.getByRole("region", { name: "What you are asked" }).textContent).toContain("npm ERR! missing script: setup");
  const line = screen.getByText("Setup failed");
  expect(line.closest("details")?.textContent).toContain("npm ERR! missing script: setup");
});

test("the timeline opens a run's final message", () => {
  const d = detail([]);
  d.timeline = [ev({ type: "RunFinished", run: 1, outcome: "Crashed", final_message: "Invalid API key · Please run /login" })];
  render(<Inspector detail={d} lines={[]} send={() => {}} onClose={() => {}} />);
  expect(screen.getByText("Run ended: crashed").closest("details")?.textContent).toContain("Please run /login");
});

test("findings, verify notes and an unreadable review are shown", () => {
  const d = detail([{ NeedsMe: "ReviewUnparsed" }]);
  d.timeline = [
    ev({
      type: "ReviewReported", run: 2, round: 1, outcome: { Findings: [
        { id: 1, title: "Panics on empty input", kind: "Bug", blocking: true,
          evidence: { type: "Command", detail: "echo '' | app parse", file: "src/parse.rs", line: 12 } },
        { id: 2, title: "Name is vague", kind: "Design", blocking: false, evidence: null },
      ] },
    }),
    ev({ type: "VerifyReported", run: 4, outcome: { Results: [{ finding: 1, status: "NotFixed", note: "still panics on a lone newline" }] } }),
    ev({ type: "ReviewReported", run: 5, round: 2, outcome: { Unparsed: { raw: "I looked and it seems fine overall." } } }),
  ];
  render(<Inspector detail={d} lines={[]} send={() => {}} onClose={() => {}} />);
  const review = screen.getByRole("region", { name: "Review" });
  const text = review.textContent ?? "";
  expect(text).toContain("Panics on empty input");
  expect(text).toContain("bug, blocking");
  expect(text).toContain("echo '' | app parse");
  expect(text).toContain("src/parse.rs:12");
  expect(text).toContain("design, advisory");
  expect(text).toContain("still panics on a lone newline");
  expect(text).toContain("not fixed");
  expect(text).toContain("I looked and it seems fine overall.");
  expect(screen.getByRole("region", { name: "What you are asked" }).textContent).toContain("I looked and it seems fine overall.");
});

test("agent text is shown as text, never as HTML", () => {
  const d = detail([{ NeedsMe: "BuilderQuestion" }]);
  const evil = '<img src="x" onerror="alert(1)"><b>bold</b>';
  d.runs = [run(1, "Builder", evil)];
  d.timeline = [ev({ type: "RunFinished", run: 1, outcome: "Finished", final_message: evil })];
  render(<Inspector detail={d} lines={[]} send={() => {}} onClose={() => {}} />);
  expect(document.querySelector("img")).toBeNull();
  expect(document.querySelector("b b, pre b")).toBeNull();
  expect(screen.getByRole("region", { name: "What you are asked" }).textContent).toContain(evil);
});
