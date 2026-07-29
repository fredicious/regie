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
    return [line[3:].strip() for line in out.splitlines() if line.strip()]


def commit_all(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-m", message)
    return git(repo, "rev-parse", "--short", "HEAD").strip()
