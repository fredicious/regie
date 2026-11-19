import type { TaskCard } from "../api/gen/TaskCard";
import { LANES } from "../describe";
import { Monitor } from "./Monitor";

type Props = { cards: TaskCard[]; selected: number | null; onSelect: (id: number) => void };

export function Board({ cards, selected, onSelect }: Props) {
  return (
    <div class="lanes">
      {LANES.map(({ lane, label }) => {
        const mine = cards.filter((c) => c.lane === lane);
        return (
          <section class="lane" role="region" aria-label={label} key={lane}>
            <h2 class="lh">
              <span>{label}</span>
              <span>{mine.length}</span>
            </h2>
            {mine.map((c) => (
              <Monitor key={c.id} card={c} selected={c.id === selected} onSelect={onSelect} />
            ))}
          </section>
        );
      })}
    </div>
  );
}
