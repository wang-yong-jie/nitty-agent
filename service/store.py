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
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, record TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
                task_id TEXT NOT NULL, id INTEGER NOT NULL, data TEXT NOT NULL,
                PRIMARY KEY (task_id, id)
            );
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY, record TEXT NOT NULL, history TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS checkpoints (
                task_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, data TEXT NOT NULL
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
                          omitted_messages=history["omitted_messages"], summary_mode=history["summary_mode"],
                          compaction_runs=history["compaction_runs"])
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
        return self._resume_projection([json.loads(row[0]) for row in rows])

    def delete_session(self, session_id: str) -> list[str]:
        """原子删除会话、关联任务和事件；返回任务 ID 供服务清理日志。"""
        with self.lock, self.db:
            tasks = self.session_tasks(session_id)
            if any(task["status"] in ACTIVE for task in tasks):
                raise ValueError("此会话仍有任务正在执行，请先停止并等待任务结束。")
            self.db.execute("DELETE FROM events WHERE task_id IN (SELECT id FROM tasks WHERE json_extract(record, '$.session_id') = ?)",
                            (session_id,))
            self.db.execute("DELETE FROM tasks WHERE json_extract(record, '$.session_id') = ?", (session_id,))
            self.db.executemany("DELETE FROM checkpoints WHERE task_id = ?", [(task["id"],) for task in tasks])
            self.db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        return [task["id"] for task in tasks]

    def save(self, record: dict) -> None:
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO tasks VALUES (?, ?)",
                            (record["id"], json.dumps(record, ensure_ascii=False, allow_nan=False)))

    def save_status(self, record: dict, timestamp: str) -> None:
        """任务终态和最后一个事件在同一事务中提交，避免 SSE 提前关闭。"""
        with self.lock, self.db:
            checkpoint = self.db.execute("SELECT revision, data FROM checkpoints WHERE task_id = ?", (record["id"],)).fetchone()
            if checkpoint:
                record["checkpoint_revision"] = checkpoint[0]
                record["resume_available"] = record["status"] not in ACTIVE | {"completed"} and json.loads(checkpoint[1])["status"] != "completed"
            self.db.execute("INSERT OR REPLACE INTO tasks VALUES (?, ?)",
                            (record["id"], json.dumps(record, ensure_ascii=False, allow_nan=False)))
            last = self.db.execute("SELECT COALESCE(MAX(id), 0) FROM events WHERE task_id = ?", (record["id"],)).fetchone()[0]
            data = {"event": "task_updated", "timestamp": timestamp, "task": record}
            self.db.execute("INSERT INTO events VALUES (?, ?, ?)",
                            (record["id"], last + 1, json.dumps(data, ensure_ascii=False, allow_nan=False)))

    def checkpoint(self, task_id):
        with self.lock:
            row = self.db.execute("SELECT revision, data FROM checkpoints WHERE task_id = ?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        return row[0], json.loads(row[1])

    def save_checkpoint(self, task_id, data, timestamp):
        from core.checkpoint import restore
        restore(data, resuming=False)
        with self.lock, self.db:
            record = self.get(task_id)
            if data.get("task_id") != task_id or data["task"] != record["task"] or data["session_id"] != record["session_id"]:
                raise ValueError("执行快照归属不匹配。")
            if record["status"] not in ACTIVE:
                raise ValueError("终态任务不能写入执行快照。")
            row = self.db.execute("SELECT revision FROM checkpoints WHERE task_id = ?", (task_id,)).fetchone()
            revision = row[0] + 1 if row else 1
            self.db.execute("INSERT OR REPLACE INTO checkpoints VALUES (?, ?, ?)",
                            (task_id, revision, json.dumps(data, ensure_ascii=False, allow_nan=False)))
            from core.task_memory import TaskMemory
            record.update(checkpoint_revision=revision, progress=TaskMemory.from_dict(data["memory"]).progress(),
                          attempt=data["memory"]["attempt"], turn=data["turn"], resume_available=False)
            self.db.execute("INSERT OR REPLACE INTO tasks VALUES (?, ?)",
                            (task_id, json.dumps(record, ensure_ascii=False, allow_nan=False)))
            last = self.db.execute("SELECT COALESCE(MAX(id), 0) FROM events WHERE task_id = ?", (task_id,)).fetchone()[0]
            self.db.execute("INSERT INTO events VALUES (?, ?, ?)", (task_id, last + 1,
                            json.dumps({"event": "task_updated", "timestamp": timestamp, "task": record}, ensure_ascii=False)))
        return revision

    def get(self, task_id: str) -> dict:
        with self.lock:
            row = self.db.execute("SELECT record FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        return self._resume_projection([json.loads(row[0])])[0]

    def _resume_projection(self, records):
        # 更早的快照不能覆盖同一会话中随后发生的用户问答。
        with self.lock:
            latest = dict(self.db.execute("SELECT json_extract(record, '$.session_id'), MAX(json_extract(record, '$.created_at')) "
                                          "FROM tasks GROUP BY json_extract(record, '$.session_id')").fetchall())
        for record in records:
            if record.get("resume_available") and record["created_at"] < latest.get(record.get("session_id"), record["created_at"]):
                record["resume_available"] = False
        return records

    def list(self, limit: int = 100) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT record FROM tasks ORDER BY rowid DESC LIMIT ?", (limit,)).fetchall()
        return self._resume_projection([json.loads(row[0]) for row in rows])

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
