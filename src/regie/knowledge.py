from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

from regie.models import RunState, TaskSpec
from regie.rundir import RunDir


class KnowledgeEntry(BaseModel):
    id: str
    kind: str
    fact: str
    recommendation: str = ""
    confidence: str = "medium"
    tags: list[str] = Field(default_factory=list)
    paths: list[str] = Field(default_factory=list)
    source_run: str = ""
    created_at: str = ""


def _project_store(rundir: RunDir, repo: Path) -> Path:
    home = rundir.path.parents[1]
    key = hashlib.sha256(str(repo.resolve()).encode()).hexdigest()[:16]
    path = home / "knowledge" / key / "entries.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def load_entries(rundir: RunDir, repo: Path) -> list[KnowledgeEntry]:
    path = _project_store(rundir, repo)
    if not path.exists():
        return []
    entries = []
    for line in path.read_text().splitlines():
        try:
            entries.append(KnowledgeEntry.model_validate_json(line))
        except ValueError:
            continue
    return entries


def prime(rundir: RunDir, repo: Path, task: TaskSpec | None,
          work_type: str) -> list[KnowledgeEntry]:
    terms = {work_type.lower()}
    if task:
        terms.update(re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{2,}", task.title.lower()))
        terms.update(task.risk_tags)
        for path in task.file_scope:
            terms.update(p for p in re.split(r"[/_.-]", path.lower()) if len(p) >= 3)
    scored = []
    for entry in load_entries(rundir, repo):
        haystack = " ".join([
            entry.fact, entry.recommendation, *entry.tags, *entry.paths,
        ]).lower()
        score = sum(term in haystack for term in terms)
        if score:
            scored.append((score, entry))
    selected = [entry for _, entry in sorted(
        scored, key=lambda item: (-item[0], item[1].id))[:12]]
    out = (rundir.task_dir(task.id) / "knowledge-prime.md"
           if task else rundir.path / "knowledge-prime.md")
    out.write_text("# Knowledge prime\n\n" + "\n".join(
        f"- [{entry.confidence}] {entry.fact}"
        + (f" — {entry.recommendation}" if entry.recommendation else "")
        for entry in selected) + "\n")
    return selected


def propose_learnings(rundir: RunDir, run: RunState) -> list[KnowledgeEntry]:
    candidates: list[KnowledgeEntry] = []
    now = datetime.now(UTC).isoformat()
    decisions = rundir.path / "decisions.md"
    if decisions.exists():
        for line in decisions.read_text().splitlines():
            fact = line.strip(" -")
            if len(fact) >= 20:
                candidates.append(_entry(run.id, "decision", fact, now))
    for task_id, task in run.tasks.items():
        for stage, attempts in task.attempts.items():
            for attempt in attempts:
                if attempt.failure_kind in {"repeated-gate", "budget", "stall", "wall"}:
                    fact = (f"{task_id} {stage} encountered {attempt.failure_kind} "
                            f"({attempt.failure_signature or 'no signature'})")
                    candidates.append(_entry(run.id, "gotcha", fact, now,
                                             tags=[stage, attempt.failure_kind]))
        findings = rundir.task_dir(task_id) / "findings.json"
        if findings.exists():
            for finding in json.loads(findings.read_text()):
                fact = f"{task_id}: {finding.get('title', '')} — {finding.get('detail', '')}"
                candidates.append(_entry(run.id, "anti-pattern", fact[:1200], now,
                                         paths=[finding.get("file")] if finding.get("file") else []))
    unique = {entry.id: entry for entry in candidates}
    result = list(unique.values())
    (rundir.path / "knowledge-candidates.json").write_text(
        json.dumps([entry.model_dump() for entry in result], indent=2))
    return result


def approve_candidates(rundir: RunDir, repo: Path) -> int:
    candidate_path = rundir.path / "knowledge-candidates.json"
    if not candidate_path.exists():
        return 0
    candidates = [KnowledgeEntry(**raw) for raw in json.loads(candidate_path.read_text())]
    existing = load_entries(rundir, repo)
    known = {entry.id for entry in existing}
    fresh = [entry for entry in candidates if entry.id not in known]
    if fresh:
        with _project_store(rundir, repo).open("a") as stream:
            for entry in fresh:
                stream.write(entry.model_dump_json() + "\n")
    return len(fresh)


def _entry(run_id: str, kind: str, fact: str, created_at: str,
           tags: list[str] | None = None, paths: list[str] | None = None) -> KnowledgeEntry:
    digest = hashlib.sha256(f"{kind}:{fact.lower()}".encode()).hexdigest()[:16]
    return KnowledgeEntry(id=f"{kind}-{digest}", kind=kind, fact=fact,
                          confidence="medium", tags=tags or [], paths=paths or [],
                          source_run=run_id, created_at=created_at)
