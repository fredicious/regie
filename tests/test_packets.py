from pathlib import Path

from regie.models import TaskSpec
from regie.packets import SECTION_BUDGET, render_packet, write_packet


def _task() -> TaskSpec:
    return TaskSpec(id="T1", title="Add divide", profile="builder",
                    criteria=["Given a and b, When divide(a,b), Then returns a/b"],
                    checklist=["no float surprises"])


def test_packet_has_fixed_section_order():
    md = render_packet(_task(), spec_excerpt="SPEC", decisions="D", conventions="C")
    positions = [md.index(h) for h in
                 ("# Task", "## Acceptance criteria", "## Reviewer checklist",
                  "## Spec excerpt", "## Decisions so far", "## Conventions")]
    assert positions == sorted(positions)
    assert "Add divide" in md and "SPEC" in md


def test_oversized_section_is_truncated_with_marker():
    md = render_packet(_task(), spec_excerpt="x" * (SECTION_BUDGET + 500),
                       decisions="", conventions="")
    assert "[... truncated]" in md
    assert len(md) < SECTION_BUDGET + 2000


def test_write_packet(tmp_path: Path):
    p = write_packet(tmp_path, "hello")
    assert p.name == "context.md" and p.read_text() == "hello"
