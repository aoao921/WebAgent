"""Standalone SQLite persistence; no application/domain state dependencies."""

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
from typing import Any

OPERATION_CANCELLED = "cancelled"
OPERATION_FAILED = "failed"
OPERATION_OUTCOME_UNKNOWN = "outcome_unknown"
OPERATION_RUNNING = "running"
OPERATION_SUCCEEDED = "succeeded"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def compact_operation_result(value: dict) -> dict:
    # Browser output and process output are bounded at their runtime boundaries.
    # Preserve the exact result so a durable replay does not invent observations.
    return value


class CheckpointStore:
    """Private run/operation/artifact storage. Public events are stored separately."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS records (kind TEXT, id TEXT, payload TEXT NOT NULL, PRIMARY KEY(kind,id))")
        if os.name != "nt":
            self.path.parent.chmod(0o700)
            self.path.chmod(0o600)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=15)
        try:
            db.execute("PRAGMA busy_timeout=15000")
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _put(db, kind: str, identity: str, value: dict):
        db.execute("INSERT INTO records VALUES(?,?,?) ON CONFLICT(kind,id) DO UPDATE SET payload=excluded.payload",
                   (kind, identity, json.dumps(value, ensure_ascii=False, default=str)))

    def get(self, kind: str, identity: str) -> dict | None:
        with self.connection() as db:
            row = db.execute("SELECT payload FROM records WHERE kind=? AND id=?", (kind, identity)).fetchone()
        return json.loads(row[0]) if row else None

    def rows(self, kind: str) -> list[dict]:
        with self.connection() as db:
            return [json.loads(row[0]) for row in db.execute("SELECT payload FROM records WHERE kind=? ORDER BY id", (kind,))]

    def save(self, kind: str, identity: str, value: dict):
        with self.connection() as db:
            self._put(db, kind, identity, value)

    def prepare_operation(self, *, operation_id, **values):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM records WHERE kind='operation' AND id=?", (operation_id,)).fetchone()
            if row:
                return json.loads(row[0])
            value = {"operation_id": operation_id, "status": "prepared", "created_at": now(), **values}
            self._put(db, "operation", operation_id, value)
            return value

    def update_operation(self, operation_id, status, **values):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM records WHERE kind='operation' AND id=?", (operation_id,)).fetchone()
            if row is None:
                raise KeyError(operation_id)
            value = {**json.loads(row[0]), **values, "status": status, "updated_at": now()}
            self._put(db, "operation", operation_id, value)

    def get_operation(self, operation_id):
        return self.get("operation", operation_id)

    def save_run(self, run):
        self.save("run", run.run_id, run.model_dump(mode="json"))

    def get_agent_run(self, task_id, run_id):
        value = self.get("run", run_id)
        return value if value and value["job"]["task_id"] == task_id else None

    def save_workspace_artifact(self, *, artifact_id, **values):
        self.save("artifact", artifact_id, {"artifact_id": artifact_id, **values})

    def list_workspace_artifacts(self, workspace_id):
        return [row for row in self.rows("artifact") if row["workspace_id"] == workspace_id]

    def record_workspace_mutation(self, *, mutation_id, **values):
        self.save("mutation", mutation_id, {"mutation_id": mutation_id, **values})

    def start_process_runtime(self, *, process_id, **values):
        identity = values.pop("identity", {})
        self.save("process", process_id, {"process_id": process_id, "status": "running", "identity_json": json.dumps(identity), **values})

    def list_process_runtimes(self, *, task_id, run_id, statuses=()):
        return [row for row in self.rows("process") if row["task_id"] == task_id and row["run_id"] == run_id
                and (not statuses or row["status"] in statuses)]

    def get_process_runtime(self, process_id):
        return self.get("process", process_id)

    def finish_process_runtime(self, process_id, *, status, **values):
        value = self.get_process_runtime(process_id)
        if value:
            self.save("process", process_id, {**value, **values, "status": status, "updated_at": now()})

    def heartbeat_process_runtime(self, process_id, lease_until):
        value = self.get_process_runtime(process_id)
        if value:
            self.save("process", process_id, {**value, "lease_until": lease_until})

    def mark_expired_process_runtimes(self, *, task_id, run_id):
        for row in self.list_process_runtimes(task_id=task_id, run_id=run_id, statuses=("running",)):
            if str(row.get("lease_until") or "") < now():
                self.finish_process_runtime(row["process_id"], status="outcome_unknown", error="process lease expired")


def public_event(event) -> dict[str, Any]:
    """Publish metadata only; values/code/page text remain in private transcripts."""
    value = event.model_dump(mode="json")
    data = value.pop("data", {})
    value["data_keys"] = sorted(data)
    value["operation_id"] = data.get("operation_id")
    return value
