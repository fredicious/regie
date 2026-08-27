from __future__ import annotations

import re
from pathlib import Path

from regie.models import TaskSpec

SECTION_BUDGET = 4000


def _clip(text: str, budget: int = SECTION_BUDGET) -> str:
    if len(text) <= budget:
        return text
    boundary = text.rfind("\n", 0, budget)
    end = boundary if boundary > budget // 2 else budget
    return text[:end] + "\n[... truncated; read the referenced artifact for more]"


def _relevant(text: str, task: TaskSpec, budget: int) -> str:
    """Select complete paragraphs related to this task, preserving global rules.

    This is deterministic retrieval, not a lossy model summary. Full artifacts
    remain addressable through the packet's artifact index.
    """
    if not text.strip():
        return ""
    terms = {task.id.lower(), *re.findall(r"[a-zA-Z_][a-zA-Z0-9_.-]{3,}", task.title.lower())}
    for path in task.file_scope:
        terms.update(p for p in re.split(r"[/_.-]+", path.lower()) if len(p) > 3)
    chunks = re.split(r"\n\s*\n", text)
    scored: list[tuple[int, int, str]] = []
    for index, chunk in enumerate(chunks):
        low = chunk.lower()
        score = sum(1 for term in terms if term in low)
        # Instruction/rule paragraphs are useful across tasks.
        if re.search(r"\b(must|never|required|convention|rule)\b", low):
            score += 1
        scored.append((score, -index, chunk.strip()))
    chosen = [c for score, _index, c in sorted(scored, reverse=True) if score > 0]
    return _clip("\n\n".join(chosen), budget)


def render_packet(task: TaskSpec, spec_excerpt: str = "", decisions: str = "",
                  conventions: str = "", extra: str = "", *, stage: str = "build",
                  context_budget: int = 16_000, artifacts: dict[str, str] | None = None,
                  change_manifest: str = "") -> str:
    """Compile a role-specific packet with progressive-disclosure references."""
    criteria = "\n".join(f"- {c}" for c in task.criteria) or "- (none)"
    tests = "\n".join(f"- {t}" for t in task.planned_tests) or "- (none named)"
    scope = "\n".join(f"- {p}" for p in task.file_scope) or "- (advisory scope absent)"
    checklist = "\n".join(f"- {c}" for c in task.checklist) or "- (none)"
    artifact_lines = "\n".join(
        f"- {name}: `{path}`" for name, path in (artifacts or {}).items()) or "- (none)"

    fixed = [
        f"# Task {task.id}: {task.title}",
        ("## Execution mode\n"
         + ("direct — one owner is authorized to edit production code and focused tests"
            if task.execution == "direct"
            else "tdd — test and implementation authorship is mechanically separated")),
        f"## Acceptance criteria\n{criteria}",
        f"## Planned tests\n{tests}",
        f"## Predicted file scope\n{scope}",
    ]
    if stage in ("review", "specialist-review"):
        fixed.append(f"## Reviewer checklist\n{checklist}")
        fixed.append(f"## Change manifest\n{_clip(change_manifest, 5000) or '(read the diff from git)' }")
    if extra:
        fixed.append(f"## Failure/review delta\n{_clip(extra, 4000)}")
    fixed.append(f"## Full artifacts (read only if needed)\n{artifact_lines}")

    used = sum(len(part) for part in fixed)
    remaining = max(1000, context_budget - used)
    # Criteria are the task's spec contract. Inline spec text is only useful
    # when it contains a directly relevant paragraph not already decomposed.
    snippets = []
    if stage in ("test", "review", "specialist-review") or task.execution == "direct":
        relevant_spec = _relevant(spec_excerpt, task, min(remaining // 3, 3000))
        if relevant_spec:
            snippets.append(f"## Relevant spec context\n{relevant_spec}")
    relevant_decisions = _relevant(decisions, task, min(remaining // 3, 3000))
    if relevant_decisions:
        snippets.append(f"## Relevant decisions\n{relevant_decisions}")
    relevant_conventions = _relevant(conventions, task, min(remaining // 2, 5000))
    if relevant_conventions:
        snippets.append(f"## Relevant conventions\n{relevant_conventions}")
    return "\n\n".join(fixed + snippets) + "\n"


def write_packet(task_dir: Path, content: str) -> Path:
    path = task_dir / "context.md"
    path.write_text(content)
    return path
