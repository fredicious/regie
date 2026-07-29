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
    git(repo, "commit", "-m", message)
    return git(repo, "rev-parse", "--short", "HEAD").strip()
