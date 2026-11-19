import { render } from "@testing-library/preact";
import { Meter } from "./Meter";

const DAY = 86_400_000;

test("meter marks the threshold zone and the week's pace", () => {
  const now = 1_000 * DAY;
  const { container } = render(
    <Meter
      label="Claude 7d"
      kind="7d"
      window={{ used: 0.76, resets_at: now + 2 * DAY }}
      readAt={now - 4 * 60_000}
      now={now}
      reason="Open"
    />,
  );
  expect(container.textContent).toContain("76%");
  const pace = container.querySelector("[data-pace]")!;
  expect(Number(pace.getAttribute("data-pace"))).toBeCloseTo(5 / 7, 3);
  expect(container.querySelector("[data-zone]")).toBeNull();
  expect(container.querySelector("[title]")!.getAttribute("title")).toContain("read 4 min ago");
});

test("5h meter has a threshold zone from 75% and no pace tick", () => {
  const { container } = render(
    <Meter label="Claude 5h" kind="5h" window={{ used: 0.3, resets_at: 5 * 3_600_000 }} readAt={0} now={0} reason="Open" />,
  );
  expect(container.querySelector("[data-zone]")!.getAttribute("data-zone")).toBe("0.75");
  expect(container.querySelector("[data-pace]")).toBeNull();
});
