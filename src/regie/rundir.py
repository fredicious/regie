from __future__ import annotations

import fcntl
import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path

from regie.models import RunState


class RunLocked(Exception):
    pass


class RunDir:
    def __init__(self, path: Path):
        self.path = path
        self._lock_fh = None
        self._io_lock = threading.RLock()

    @classmethod
    def create(cls, home: Path, run_id: str) -> RunDir:
        path = home / "runs" / run_id
        (path / "tasks").mkdir(parents=True)
        return cls(path)

    @classmethod
    def open(cls, home: Path, run_id: str) -> RunDir:
        path = home / "runs" / run_id
        if not path.is_dir():
            raise FileNotFoundError(path)
        return cls(path)

    def acquire_lock(self) -> None:
        fh = (self.path / "run.lock").open("a")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            fh.close()
            raise RunLocked(str(self.path)) from exc
        self._lock_fh = fh

    def release_lock(self) -> None:
        if self._lock_fh:
            fcntl.flock(self._lock_fh, fcntl.LOCK_UN)
            self._lock_fh.close()
            self._lock_fh = None

    def write_state(self, state: RunState) -> None:
        with self._io_lock:
            tmp = self.path / f"state.json.tmp.{os.getpid()}.{threading.get_ident()}"
            tmp.write_text(state.model_dump_json(indent=2))
            os.replace(tmp, self.path / "state.json")

    def read_state(self) -> RunState:
        return RunState.model_validate_json((self.path / "state.json").read_text())

    def _append_jsonl(self, name: str, record: dict) -> None:
        with self._io_lock, (self.path / name).open("a") as f:
            f.write(json.dumps(record) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def append_intent(self, record: dict) -> None:
        self._append_jsonl(
            "intent.jsonl",
            {"ts": datetime.now(UTC).isoformat(), **record},
        )

    def read_intents(self) -> list[dict]:
        file = self.path / "intent.jsonl"
        if not file.exists():
            return []
        out = []
        for line in file.read_text().splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # torn tail line from a crash mid-append
        return out

    def append_event(self, record: dict) -> None:
        self._append_jsonl("events.jsonl", {"ts": datetime.now(UTC).isoformat(), **record})

    def task_dir(self, task_id: str) -> Path:
        d = self.path / "tasks" / task_id
        d.mkdir(parents=True, exist_ok=True)
        return d
