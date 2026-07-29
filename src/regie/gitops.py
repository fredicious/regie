from __future__ import annotations

import os
import subprocess
from pathlib import Path

_AUTHOR_ENV = {"GIT_AUTHOR_NAME": "regie", "GIT_AUTHOR_EMAIL": "regie@local",
               "GIT_COMMITTER_NAME": "regie", "GIT_COMMITTER_EMAIL": "regie@local"}


class GitError(Exception):
    pass


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True,
                          env={**os.environ, **_AUTHOR_ENV})
    if proc.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {proc.stderr.strip()}")
    return proc.stdout


def changed_files(repo: Path) -> list[str]:
    out = git(repo, "status", "--porcelain")
    paths: list[str] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        entry = line[3:].strip()
        # Renames are reported as "orig -> new"; a rename is as suspicious as
        # a plain edit on either side, so report both paths.
        if " -> " in entry:
            old, new = entry.split(" -> ", 1)
            paths.append(old)
            paths.append(new)
        else:
            paths.append(entry)
    return paths


def commit_all(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-m", message)
    return git(repo, "rev-parse", "--short", "HEAD").strip()
