from pathlib import Path

from regie.models import TaskSpec
from regie.packets import SECTION_BUDGET, render_packet, write_packet


def _task() -> TaskSpec:
    return TaskSpec(id="T1", title="Add divide", profile="builder",
                    criteria=["Given a and b, When divide(a,b), Then returns a/b"],
                    checklist=["no float surprises"],
                    planned_tests=["test_divide"], file_scope=["src/calc.py"])


def test_packet_has_fixed_section_order():
    md = render_packet(_task(), spec_excerpt="SPEC about divide", decisions="D",
                       conventions="Divide rule must hold", stage="review",
                       artifacts={"full spec": "/run/spec.md"},
                       change_manifest="src/calc.py | 2 ++")
    positions = [md.index(h) for h in
                 ("# Task", "## Execution mode", "## Acceptance criteria",
                  "## Reviewer checklist",
                  "## Change manifest", "## Full artifacts", "## Relevant spec context",
                  "## Relevant conventions")]
    assert positions == sorted(positions)
    assert "Add divide" in md and "test_divide" in md and "src/calc.py" in md


def test_direct_packet_authorizes_owner_authored_focused_tests():
    task = _task().model_copy(update={"execution": "direct"})

    md = render_packet(task, stage="review")

    assert "direct — one owner is authorized to edit production code and focused tests" in md


def test_oversized_section_is_truncated_with_marker():
    relevant = "divide must follow this rule " + "x" * (SECTION_BUDGET + 500)
    md = render_packet(_task(), spec_excerpt=relevant, decisions="", conventions="",
                       stage="test", context_budget=6000)
    assert "[... truncated; read the referenced artifact for more]" in md
    assert len(md) < 7000


def test_write_packet(tmp_path: Path):
    p = write_packet(tmp_path, "hello")
    assert p.name == "context.md" and p.read_text() == "hello"
