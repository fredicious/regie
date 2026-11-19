import { render, screen, within } from "@testing-library/preact";
import { Board } from "./Board";
import { card } from "../testdata";

test("board groups cards into lane columns in lane order", () => {
  render(
    <Board
      cards={[
        card({ id: 3, lane: "Merged" }),
        card({ id: 1, lane: "Working" }),
        card({ id: 2, lane: "Queued" }),
        card({ id: 4, lane: "Working" }),
      ]}
      selected={null}
      onSelect={() => {}}
    />,
  );
  const heads = screen.getAllByRole("heading", { level: 2 }).map((h) => h.textContent);
  expect(heads).toEqual(["Queued1", "Working2", "In review0", "CI0", "Ready0", "Merged1", "Cancelled0"]);
  const working = screen.getByRole("region", { name: "Working" });
  expect(within(working).getAllByRole("button")).toHaveLength(2);
});

test("monitor shows tally label text, not colour only", () => {
  render(
    <Board
      cards={[
        card({ id: 1, lane: "Working", flags: [{ NeedsMe: "BuilderQuestion" }] }),
        card({ id: 2, lane: "Working" }),
        card({ id: 3, lane: "ReadyToMerge", pr: [9, "http://x"] }),
        card({ id: 4, lane: "Queued", flags: [{ Waiting: "Slot" }] }),
      ]}
      selected={null}
      onSelect={() => {}}
    />,
  );
  expect(within(screen.getByRole("button", { name: /T1/ })).getByText("NEEDS ME")).toBeTruthy();
  expect(within(screen.getByRole("button", { name: /T2/ })).getByText("ON AIR")).toBeTruthy();
  expect(within(screen.getByRole("button", { name: /T3/ })).getByText("READY")).toBeTruthy();
  expect(within(screen.getByRole("button", { name: /T3/ })).getByText("PR #9")).toBeTruthy();
  expect(within(screen.getByRole("button", { name: /T4/ })).getByText("OFF")).toBeTruthy();
});
