"""SQLite outbox and command journal. All mutations commit before network IO."""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path


class NodeStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE, body TEXT NOT NULL,
                acknowledged INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS ix_node_events_pending
                ON events(seq) WHERE acknowledged=0;
            CREATE TABLE IF NOT EXISTS commands (
                id TEXT PRIMARY KEY, body TEXT NOT NULL,
                status TEXT NOT NULL, response TEXT
            );
            CREATE TABLE IF NOT EXISTS threads (id TEXT PRIMARY KEY, cwd TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS asset_jobs (
                id TEXT PRIMARY KEY, body TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending'
            );
            CREATE TABLE IF NOT EXISTS uploaded_assets (
                cache_key TEXT PRIMARY KEY, asset TEXT NOT NULL
            );
        """)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def recover_uncertain(self) -> None:
        rows = self.db.execute("SELECT id FROM commands WHERE status='started'").fetchall()
        with self.db:
            for row in rows:
                response = {
                    "type": "command_result",
                    "id": row["id"],
                    "unknown": True,
                    "error": "Codex stopped before the command outcome was recorded; "
                    "inspect thread history before submitting a new command",
                }
                self.db.execute(
                    "UPDATE commands SET status='unknown',response=? WHERE id=?",
                    (json.dumps(response), row["id"]),
                )

    def claim(self, command: dict) -> tuple[bool, dict | None]:
        existing = self.db.execute(
            "SELECT body,status,response FROM commands WHERE id=?", (command["id"],)
        ).fetchone()
        if existing:
            if json.loads(existing["body"]) != command:
                raise ValueError("Command ID was reused with different contents")
            return False, json.loads(existing["response"]) if existing["response"] else None
        with self.db:
            self.db.execute(
                "INSERT INTO commands(id,body,status) VALUES(?,?,'started')",
                (command["id"], json.dumps(command)),
            )
        return True, None

    def finish(
        self, command_id: str, *, result=None, error: str | None = None, unknown: bool = False
    ) -> dict:
        response = {"type": "command_result", "id": command_id}
        if error is not None:
            response["error"] = error
        else:
            response["result"] = result
        if unknown:
            response["unknown"] = True
        with self.db:
            self.db.execute(
                "UPDATE commands SET status=?,response=? WHERE id=?",
                ("unknown" if unknown else "completed", json.dumps(response), command_id),
            )
        return response

    def completed(self) -> list[dict]:
        return [
            json.loads(row[0])
            for row in self.db.execute(
                "SELECT response FROM commands WHERE response IS NOT NULL ORDER BY rowid"
            )
        ]

    def record_event(
        self, runtime_id: str, method: str, params: dict, rpc_id=None, *, asset_job: bool = False
    ) -> dict:
        event_id = str(uuid.uuid4())
        with self.db:
            cursor = self.db.execute(
                "INSERT INTO events(event_id,body) VALUES(?,'{}')", (event_id,)
            )
            event = {
                "type": "event",
                "event_id": event_id,
                "runtime_id": runtime_id,
                "seq": cursor.lastrowid,
                "method": method,
                "params": params,
            }
            if rpc_id is not None:
                event["rpc_id"] = rpc_id
            self.db.execute(
                "UPDATE events SET body=? WHERE event_id=?",
                (json.dumps(event, ensure_ascii=False), event_id),
            )
            if asset_job:
                self.db.execute(
                    "INSERT INTO asset_jobs(id,body) VALUES(?,?)",
                    (event_id, json.dumps(event, ensure_ascii=False)),
                )
        return event

    def pending_events(self, limit: int = 100) -> list[dict]:
        return [
            json.loads(row[0])
            for row in self.db.execute(
                "SELECT body FROM events WHERE acknowledged=0 ORDER BY seq LIMIT ?", (limit,)
            )
        ]

    def acknowledge(self, event_id: str) -> bool:
        with self.db:
            # Retain the monotonic sequence while bounding the journal's content size.
            changed = self.db.execute(
                "UPDATE events SET acknowledged=1,body='{}' WHERE event_id=? AND acknowledged=0",
                (event_id,),
            )
        return changed.rowcount == 1

    def save_thread(self, thread_id: str, cwd: str) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO threads(id,cwd) VALUES(?,?)", (thread_id, cwd))

    def thread_cwd(self, thread_id: str) -> str | None:
        row = self.db.execute("SELECT cwd FROM threads WHERE id=?", (thread_id,)).fetchone()
        return row[0] if row else None

    def asset_jobs(self) -> list[dict]:
        return [
            json.loads(row[0])
            for row in self.db.execute(
                "SELECT body FROM asset_jobs WHERE status='pending' ORDER BY rowid LIMIT 20"
            )
        ]

    def finish_asset_job(self, event_id: str) -> None:
        with self.db:
            self.db.execute("DELETE FROM asset_jobs WHERE id=?", (event_id,))

    def cached_asset(self, key: str) -> dict | None:
        row = self.db.execute(
            "SELECT asset FROM uploaded_assets WHERE cache_key=?", (key,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def save_asset(self, key: str, asset: dict) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO uploaded_assets(cache_key,asset) VALUES(?,?)",
                (key, json.dumps(asset)),
            )
