"""Darwin's lab state: the lineage of generations, the running experiment and the pending approval card.

One JSON file (data/lab.json) guarded by a file lock, so the CLI and the Lab dashboard can both act.
"""

from __future__ import annotations

import fcntl
import json
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


def empty_state(flag: str) -> dict:
    return {
        "flag": flag,
        "feature_id": None,
        "metrics": {},  # metric name -> Flagsmith metric id
        "champion": None,
        "generations": {},  # name -> generation record
        "experiment": None,  # the running experiment, if any
        "pending": None,  # the approval card waiting for a human, if any
        "busy": None,  # {"what": ..., "since": ...} while the Coach or a sequence is working
        "history": [],  # every decision and approval, newest last
    }


class StateStore:
    def __init__(self, path: Path, flag: str):
        self.path = Path(path)
        self.flag = flag
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_path = self.path.with_suffix(".lock")

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with open(self._lock_path, "a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _read(self) -> dict:
        if self.path.exists():
            return json.loads(self.path.read_text())
        return empty_state(self.flag)

    def read(self) -> dict:
        with self._locked():
            return self._read()

    @contextmanager
    def transaction(self) -> Iterator[dict]:
        """Read-modify-write under the lock. Changes are saved when the block exits without an error."""
        with self._locked():
            state = self._read()
            yield state
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False))
            tmp.replace(self.path)


def log_event(state: dict, kind: str, text: str, actor: str = "darwin", **details) -> None:
    state["history"].append({"ts": time.time(), "kind": kind, "actor": actor, "text": text, **details})
