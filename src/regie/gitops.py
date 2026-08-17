from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

# Commits should carry the DEVELOPER's identity (repo/global git config); the
# Régie identity is only a fallback for environments with no identity at all
# (bare CI containers, test fixtures). Régie's involvement is recorded via the
# Co-authored-by trailer the PR stage appends, not by authoring as itself.
_FALLBACK_IDENTITY = ["-c", "user.name=Régie", "-c", "user.email=regie@noreply.local"]


class GitError(Exception):
    pass


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True,
                          env=os.environ.copy(), check=False)
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip()
        raise GitError(f"git {' '.join(args)}: {detail}")
    return proc.stdout


def _identity_args(repo: Path) -> list[str]:
    try:
        if git(repo, "config", "user.email").strip():
            return []
    except GitError:
        pass
    return list(_FALLBACK_IDENTITY)


def changed_files(repo: Path) -> list[str]:
    # -z gives NUL-separated records with paths never quoted (plain
    # --porcelain C-quotes paths containing spaces/special chars, e.g.
    # `"b file.txt"`, which then fails to match any glob). For renames/copies
    # the record is `XY new_path\0orig_path\0` — the original path is a
    # separate NUL-terminated field, not " -> "-joined as in plain mode.
    out = git(repo, "status", "--porcelain", "-z")
    tokens = out.split("\0")
    paths: list[str] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if not token:
            i += 1
            continue
        status, path = token[:2], token[3:]
        paths.append(path)
        # A rename is as suspicious as a plain edit on either side, so report
        # both paths.
        if "R" in status or "C" in status:
            i += 1
            if i < len(tokens) and tokens[i]:
                paths.append(tokens[i])
        i += 1
    return paths


def commit_all(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    git(repo, *_identity_args(repo), "commit", "-m", message)
    return git(repo, "rev-parse", "--short", "HEAD").strip()


def head_sha(repo: Path, ref: str = "HEAD") -> str:
    return git(repo, "rev-parse", ref).strip()


def fetch_base_sha(repo: Path, base_branch: str) -> str:
    git(repo, "fetch", "origin", base_branch)
    return git(repo, "rev-parse", f"origin/{base_branch}").strip()


def create_run_worktree(repo: Path, branch: str, base_sha: str, dest: Path) -> Path:
    git(repo, "worktree", "add", "-b", branch, str(dest), base_sha)
    return dest


def remove_run_worktree(repo: Path, dest: Path) -> None:
    try:
        git(repo, "worktree", "remove", "--force", str(dest))
    except GitError:
        listed = str(dest) in git(repo, "worktree", "list", "--porcelain")
        if dest.exists() or listed:
            raise
    finally:
        git(repo, "worktree", "prune")


def delete_branch(repo: Path, branch: str) -> None:
    if not branch.startswith("regie/"):
        raise GitError(f"refusing to delete non-regie branch: {branch}")
    git(repo, "branch", "-D", branch)


def push_branch(worktree: Path, branch: str) -> None:
    git(worktree, "push", "-u", "origin", branch)


_GROUP_KEY_RE = re.compile(r"^(?:test|feat|fix)\(([^)]+)\):")


def run_commit_groups(worktree: Path, base_sha: str) -> list[tuple[str, list[str]]]:
    out = git(worktree, "log", "--reverse", "--format=%H%x09%s", f"{base_sha}..HEAD")
    groups: list[tuple[str, list[str]]] = []
    keys: list[str | None] = []
    for line in out.splitlines():
        if not line:
            continue
        sha, _, subject = line.partition("\t")
        m = _GROUP_KEY_RE.match(subject)
        key = m.group(1) if m else None
        if groups and key is not None and keys[-1] == key:
            groups[-1] = (subject, groups[-1][1] + [sha])
        elif groups and key is None:
            # Non-matching commits join the current group without changing
            # its default message.
            groups[-1] = (groups[-1][0], groups[-1][1] + [sha])
        else:
            groups.append((subject, [sha]))
            keys.append(key)
    return groups


def rebuild_history(worktree: Path, base_sha: str,
                     groups: list[tuple[str, list[str]]], run_id: str) -> None:
    if changed_files(worktree):
        raise GitError("worktree dirty — refusing history rewrite")
    pre_tree = git(worktree, "rev-parse", "HEAD^{tree}").strip()
    backup_ref = f"refs/regie/backup/{run_id}"
    git(worktree, "update-ref", backup_ref, "HEAD")
    git(worktree, "reset", "--hard", base_sha)
    try:
        for message, shas in groups:
            git(worktree, "cherry-pick", "-n", *shas)
            git(worktree, *_identity_args(worktree), "commit", "-m", message)
    except GitError:
        git(worktree, "reset", "--hard", backup_ref)
        raise
    post_tree = git(worktree, "rev-parse", "HEAD^{tree}").strip()
    if post_tree != pre_tree:
        git(worktree, "reset", "--hard", backup_ref)
        raise GitError("tree mismatch after rewrite")


def _tool(cwd: Path, *argv: str) -> str:
    proc = subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise GitError(f"{' '.join(argv)}: {proc.stderr.strip()}")
    return proc.stdout


def create_pr(worktree: Path, base_branch: str, title: str, body_file: Path) -> str:
    out = _tool(worktree, "gh", "pr", "create", "--base", base_branch,
                "--title", title, "--body-file", str(body_file))
    return out.strip()


def ci_failures(worktree: Path) -> str:
    """Raw `gh pr checks` output for feeding a debugger round's failure
    context. Best-effort: a nonzero exit (still no checks reported, `gh`
    missing, etc.) is tolerated and its output captured regardless."""
    proc = subprocess.run(["gh", "pr", "checks"], cwd=str(worktree),
                          capture_output=True, text=True, check=False)
    return (proc.stdout + proc.stderr)[-4000:]


def ci_status(worktree: Path) -> str:
    try:
        out = _tool(worktree, "gh", "pr", "checks", "--json", "state",
                    "--jq", "[.[].state] | unique | join(\",\")")
    except GitError:
        return "green"
    states = out.strip()
    if not states:
        return "green"
    parts = states.split(",")
    if "FAILURE" in parts:
        return "red"
    if all(p == "SUCCESS" for p in parts):
        return "green"
    return "pending"


def pr_snapshot(worktree: Path) -> dict:
    """Best-effort PR lifecycle state; CI remains independently authoritative."""
    try:
        raw = _tool(worktree, "gh", "pr", "view", "--json",
                    "number,state,reviewDecision,mergeStateStatus,url,comments,reviews")
        data = json.loads(raw)
    except (GitError, json.JSONDecodeError):
        return {}
    data["unresolvedThreads"] = _unresolved_threads(worktree, data.get("number"))
    comments = data.get("comments") or []
    data["lastCommentId"] = str(comments[-1].get("id", "")) if comments else ""
    return data


def pr_feedback(worktree: Path) -> str:
    snapshot = pr_snapshot(worktree)
    parts = []
    for review in snapshot.get("reviews") or []:
        body = (review.get("body") or "").strip()
        if body:
            parts.append(f"Review ({review.get('state', 'unknown')}): {body}")
    for comment in snapshot.get("comments") or []:
        body = (comment.get("body") or "").strip()
        if body:
            parts.append(f"Comment: {body}")
    return "\n\n".join(parts)[-8000:]


def _unresolved_threads(worktree: Path, number: int | None) -> int:
    if not number:
        return 0
    try:
        owner_name = json.loads(_tool(
            worktree, "gh", "repo", "view", "--json", "nameWithOwner"))["nameWithOwner"]
        owner, name = owner_name.split("/", 1)
        query = ("query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,"
                 "name:$name){pullRequest(number:$number){reviewThreads(first:100){nodes{"
                 "isResolved}}}}}")
        raw = _tool(worktree, "gh", "api", "graphql", "-f", f"query={query}",
                    "-f", f"owner={owner}", "-f", f"name={name}",
                    "-F", f"number={number}")
        nodes = json.loads(raw)["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]
        return sum(not node.get("isResolved", False) for node in nodes)
    except (GitError, KeyError, ValueError, json.JSONDecodeError):
        return 0
