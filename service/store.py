"""SQLite 任务和事件存储，跨浏览器刷新保留结果与事件游标。"""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import threading

from contracts import Message
from core.session import Session

ACTIVE = {"queued", "running", "stopping"}


class TaskStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, record TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
                task_id TEXT NOT NULL, id INTEGER NOT NULL, data TEXT NOT NULL,
                PRIMARY KEY (task_id, id)
            );
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY, record TEXT NOT NULL, history TEXT NOT NULL
            );
        """)
        self.db.commit()
        # 旧任务各自迁入一个会话，已有页面链接和任务结果继续可用。
        for task in self.list(limit=1000000):
            if not task.get("session_id"):
                session = self.create_session(task.get("task", "历史任务")[:80])
                task["session_id"] = session["id"]
                history = Session(id=session["id"], messages=[Message("user", task.get("task", ""))])
                if task.get("answer"):
                    history.messages.append(Message("assistant", task["answer"]))
                self.save_session_history(history.to_dict(), task.get("options"))
                self.save(task)

    def create_session(self, title: str = "新会话") -> dict:
        from datetime import datetime, timezone

        timestamp = datetime.now(timezone.utc).isoformat()
        session = Session()
        record = dict(id=session.id, title=title, created_at=timestamp, updated_at=timestamp,
                      options=None, working_directory=None, omitted_messages=0)
        with self.lock, self.db:
            self.db.execute("INSERT INTO sessions VALUES (?, ?, ?)",
                            (session.id, json.dumps(record, ensure_ascii=False), json.dumps(session.to_dict(), ensure_ascii=False)))
        return record

    def get_session(self, session_id: str) -> dict:
        with self.lock:
            row = self.db.execute("SELECT record FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if row is None:
            raise KeyError(session_id)
        return json.loads(row[0])

    def list_sessions(self, limit: int = 100) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT record FROM sessions ORDER BY json_extract(record, '$.updated_at') DESC LIMIT ?",
                                   (limit,)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def session_history(self, session_id: str) -> dict:
        with self.lock:
            row = self.db.execute("SELECT history FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if row is None:
            raise KeyError(session_id)
        return json.loads(row[0])

    def save_session_history(self, history: dict, options: dict | None = None, title: str | None = None) -> None:
        from datetime import datetime, timezone

        # 序列化边界统一裁剪并排除图片编码；会话原文不进入浏览器事件。
        history = Session.from_dict(history).to_dict()
        with self.lock, self.db:
            record = self.get_session(history["id"])
            record.update(updated_at=datetime.now(timezone.utc).isoformat(), working_directory=history["working_directory"],
                          omitted_messages=history["omitted_messages"])
            if options is not None:
                record["options"] = options
            if title is not None:
                record["title"] = title
            self.db.execute("UPDATE sessions SET record = ?, history = ? WHERE id = ?",
                            (json.dumps(record, ensure_ascii=False, allow_nan=False),
                             json.dumps(history, ensure_ascii=False, allow_nan=False), history["id"]))

    def session_tasks(self, session_id: str) -> list[dict]:
        self.get_session(session_id)
        with self.lock:
            rows = self.db.execute("SELECT record FROM tasks WHERE json_extract(record, '$.session_id') = ? ORDER BY rowid",
                                   (session_id,)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def save(self, record: dict) -> None:
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO tasks VALUES (?, ?)",
                            (record["id"], json.dumps(record, ensure_ascii=False, allow_nan=False)))

    def save_status(self, record: dict, timestamp: str) -> None:
        """任务终态和最后一个事件在同一事务中提交，避免 SSE 提前关闭。"""
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO tasks VALUES (?, ?)",
                            (record["id"], json.dumps(record, ensure_ascii=False, allow_nan=False)))
            last = self.db.execute("SELECT COALESCE(MAX(id), 0) FROM events WHERE task_id = ?", (record["id"],)).fetchone()[0]
            data = {"event": "task_updated", "timestamp": timestamp, "task": record}
            self.db.execute("INSERT INTO events VALUES (?, ?, ?)",
                            (record["id"], last + 1, json.dumps(data, ensure_ascii=False, allow_nan=False)))

    def get(self, task_id: str) -> dict:
        with self.lock:
            row = self.db.execute("SELECT record FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        return json.loads(row[0])

    def list(self, limit: int = 100) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT record FROM tasks ORDER BY rowid DESC LIMIT ?", (limit,)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def append(self, task_id: str, data: dict) -> dict:
        with self.lock, self.db:
            last = self.db.execute("SELECT COALESCE(MAX(id), 0) FROM events WHERE task_id = ?", (task_id,)).fetchone()[0]
            event = {"id": last + 1, "task_id": task_id, "data": data}
            self.db.execute("INSERT INTO events VALUES (?, ?, ?)",
                            (task_id, event["id"], json.dumps(data, ensure_ascii=False, allow_nan=False)))
        return event

    def events(self, task_id: str, after: int = 0, limit: int = 200) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT id, data FROM events WHERE task_id = ? AND id > ? ORDER BY id LIMIT ?",
                (task_id, after, limit),
            ).fetchall()
        return [{"id": row[0], "task_id": task_id, "data": json.loads(row[1])} for row in rows]

    def close(self) -> None:
        with self.lock:
            self.db.close()
