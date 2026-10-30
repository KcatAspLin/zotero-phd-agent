import contextlib
import fcntl
import hashlib
import json
import os
import tempfile
from pathlib import Path

from .net import AgentError


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".pending-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(value, out, ensure_ascii=False, indent=2)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class State:
    def __init__(self, directory):
        self.directory = Path(directory).expanduser().resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.journal_path = self.directory / "journal.json"
        self.journal = {}

    @contextlib.contextmanager
    def lock(self):
        with (self.directory / "agent.lock").open("a") as file:
            try:
                fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise AgentError("Another agent is using this state directory") from exc
            try:
                self.journal = json.loads(self.journal_path.read_text()) if self.journal_path.exists() else {}
                yield
            finally:
                fcntl.flock(file, fcntl.LOCK_UN)

    def save(self):
        atomic_json(self.journal_path, self.journal)

    def job(self, library, doi, sha):
        identity = hashlib.sha256(f"{library}:{doi}:{sha}".encode()).hexdigest()
        return self.journal.setdefault(identity, {}), identity
