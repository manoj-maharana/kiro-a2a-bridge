"""Where `serve` keeps A2A tasks: in memory (default) or in a SQLite file.

With SQLite, finished tasks survive a restart and can still be fetched with
tasks/get. Tasks that were running when the server stopped are marked failed
on the next start, because the agent session behind them is gone.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections import OrderedDict

ACTIVE_STATES = ("submitted", "working", "input-required")


class MemoryTaskStore:
    def __init__(self, limit: int = 1000):
        self.limit = limit
        self._tasks: OrderedDict[str, dict] = OrderedDict()

    def put(self, task: dict) -> None:
        self._tasks[task["id"]] = task
        self._tasks.move_to_end(task["id"])
        while len(self._tasks) > self.limit:
            oldest = next(iter(self._tasks))
            if self._tasks[oldest]["status"]["state"] in ACTIVE_STATES:
                break
            del self._tasks[oldest]

    def get(self, task_id: str) -> dict | None:
        return self._tasks.get(task_id)

    def list(self, context_id: str | None = None, state: str | None = None, limit: int = 50, offset: int = 0) -> list[dict]:
        tasks = [t for t in reversed(self._tasks.values())
                 if (not context_id or t["contextId"] == context_id) and (not state or t["status"]["state"] == state)]
        return tasks[offset:offset + limit]

    def __contains__(self, task_id) -> bool:
        return task_id in self._tasks


class SqliteTaskStore(MemoryTaskStore):
    """Memory store with every change also written to SQLite."""

    def __init__(self, path: str, limit: int = 1000):
        super().__init__(limit)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock, self._db:
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, context_id TEXT, state TEXT, "
                "updated REAL DEFAULT (julianday('now')), body TEXT NOT NULL)"
            )
            self._db.execute("CREATE INDEX IF NOT EXISTS tasks_context ON tasks (context_id, updated)")
            stale = self._db.execute(
                f"SELECT body FROM tasks WHERE state IN ({','.join('?' * len(ACTIVE_STATES))})", ACTIVE_STATES
            ).fetchall()
        for (body,) in stale:
            task = json.loads(body)
            task["status"] = {"state": "failed", "message": {
                "kind": "message", "role": "agent", "messageId": task["id"] + "-restart",
                "parts": [{"kind": "text", "text": "The server restarted while this task was running."}]}}
            self.save(task)

    def save(self, task: dict) -> None:
        """Write the task to disk (call after meaningful changes)."""
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO tasks (id, context_id, state, updated, body) VALUES (?, ?, ?, julianday('now'), ?)",
                (task["id"], task["contextId"], task["status"]["state"], json.dumps(task, ensure_ascii=False)),
            )

    def put(self, task: dict) -> None:
        super().put(task)
        self.save(task)

    def get(self, task_id: str) -> dict | None:
        task = super().get(task_id)
        if task is None:
            with self._lock:
                row = self._db.execute("SELECT body FROM tasks WHERE id = ?", (task_id,)).fetchone()
            task = json.loads(row[0]) if row else None
        return task

    def list(self, context_id: str | None = None, state: str | None = None, limit: int = 50, offset: int = 0) -> list[dict]:
        query, args = "SELECT body FROM tasks WHERE 1=1", []
        if context_id:
            query, args = query + " AND context_id = ?", [*args, context_id]
        if state:
            query, args = query + " AND state = ?", [*args, state]
        query += " ORDER BY updated DESC LIMIT ? OFFSET ?"
        with self._lock:
            rows = self._db.execute(query, [*args, limit, offset]).fetchall()
        return [self._tasks.get(t["id"], t) for t in (json.loads(r[0]) for r in rows)]

    def __contains__(self, task_id) -> bool:
        return self.get(task_id) is not None

    def close(self) -> None:
        self._db.close()
