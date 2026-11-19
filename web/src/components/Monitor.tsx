import type { TaskCard } from "../api/gen/TaskCard";
import { tally } from "../describe";

type Props = { card: TaskCard; selected: boolean; onSelect: (id: number) => void };

export function Monitor({ card: c, selected, onSelect }: Props) {
  const t = tally(c);
  const meta = [c.level.toLowerCase(), c.model].filter(Boolean).join(" · ");
  return (
    <button
      type="button"
      class={`mon ${t.colour}${selected ? " sel" : ""}${c.lane === "Merged" || c.lane === "Cancelled" ? " done" : ""}`}
      aria-pressed={selected}
      onClick={() => onSelect(c.id)}
    >
      <span class="tally">
        <i class={`tl ${t.colour}`} aria-hidden="true" />
        <span class="tl-text">{t.label}</span>
      </span>
      <span class="t">
        <span class="tid">T{c.id}</span> {c.title}
      </span>
      <span class="m">{meta}</span>
      {(c.tickets.length > 0 || c.pr) && (
        <span class="m">
          {c.tickets.map((x) => (
            <span key={x}>{x} </span>
          ))}
          {c.pr && <span>PR #{c.pr[0]}</span>}
        </span>
      )}
      {c.last_output && <span class="out">{c.last_output}</span>}
    </button>
  );
}
