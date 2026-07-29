from __future__ import annotations

from pathlib import Path

from regie.models import TaskSpec

SECTION_BUDGET = 8000  # chars, ≈2k tokens per free-text section


def _clip(text: str) -> str:
    if len(text) <= SECTION_BUDGET:
        return text
    return text[:SECTION_BUDGET] + "\n[... truncated]"


def render_packet(task: TaskSpec, spec_excerpt: str, decisions: str,
                  conventions: str, extra: str = "") -> str:
    criteria = "\n".join(f"- {c}" for c in task.criteria)
    checklist = "\n".join(f"- {c}" for c in task.checklist) or "- (none)"
    return "\n\n".join([
        f"# Task {task.id}: {task.title}",
        f"## Acceptance criteria\n{criteria}",
        f"## Reviewer checklist\n{checklist}",
        f"## Spec excerpt\n{_clip(spec_excerpt)}",
        f"## Decisions so far\n{_clip(decisions) or '(none yet)'}",
        f"## Conventions\n{_clip(conventions)}",
        f"## Notes\n{_clip(extra) or '(none)'}",
    ]) + "\n"


def write_packet(task_dir: Path, content: str) -> Path:
    path = task_dir / "context.md"
    path.write_text(content)
    return path
