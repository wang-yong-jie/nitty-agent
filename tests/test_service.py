"""真实 spawn 与 SQLite 验证；模型与文件/Shell 工具均使用离线替身。"""

import json
from pathlib import Path
import queue
import tempfile
import unittest
from unittest.mock import Mock
from unittest.mock import patch

from bootstrap import AgentOptions
from service.manager import TaskBusy, TaskManager
from service.store import TaskStore
from service.worker import TaskTrace
from tests.service_workers import blocking_worker, crash_worker, fixture_worker, wait_terminal


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.managers = []

    def tearDown(self):
        for manager in self.managers:
            manager.close()
        self.directory.cleanup()

    def manager(self, target=fixture_worker):
        manager = TaskManager(self.root / "data", self.root / "logs", worker_target=target)
        self.managers.append(manager)
        return manager

    def test_spawn_completion_events_logs_and_restart_history(self):
        manager = self.manager()
        task = manager.submit("读取说明", AgentOptions())
        result = wait_terminal(manager, task["id"])
        self.assertEqual(result["status"], "completed")
        self.assertIn("离线验证完成", result["answer"])
        events = manager.store.events(task["id"])
        self.assertEqual([e["id"] for e in events], list(range(1, len(events) + 1)))
        self.assertEqual(events[-1]["data"]["task"]["status"], "completed")
        self.assertIn("tool_finished", [e["data"]["event"] for e in events])
        self.assertEqual(manager.store.events(task["id"], events[-2]["id"]), events[-1:])
        self.assertTrue((self.root / "logs" / f"{task['id']}.trace.jsonl").exists())
        manager.close()
        self.managers.remove(manager)
        reopened = self.manager()
        self.assertEqual(reopened.store.get(task["id"])["answer"], result["answer"])

    def test_stop_is_pending_until_worker_exits_and_single_task_is_enforced(self):
        manager = self.manager(blocking_worker)
        task = manager.submit("等待停止 [slow]", AgentOptions())
        with self.assertRaises(TaskBusy):
            manager.submit("第二个任务", AgentOptions())
        self.assertEqual(manager.stop(task["id"])["status"], "stopping")
        self.assertEqual(manager.stop(task["id"])["status"], "stopping")
        self.assertEqual(manager.store.get(task["id"])["status"], "stopping")
        result = wait_terminal(manager, task["id"])
        self.assertEqual(result["status"], "cancelled")
        self.assertIsNone(result["answer"])
        self.assertEqual(manager.stop(task["id"])["status"], "cancelled")

    def test_model_failure_preserves_framework_diagnostics(self):
        manager = self.manager()
        task = manager.submit("失败 [fail]", AgentOptions())
        result = wait_terminal(manager, task["id"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_info"]["phase"], "model")
        self.assertEqual(result["error_info"]["code"], "MODEL_FAILED")
        self.assertIsNone(result["answer"])

    def test_process_crash_has_terminal_state_and_releases_slot(self):
        manager = self.manager(crash_worker)
        task = manager.submit("异常退出", AgentOptions())
        result = wait_terminal(manager, task["id"])
        self.assertEqual(result["error_info"]["code"], "WORKER_EXIT")
        self.assertEqual(result["error_info"]["side_effects"], "possible")
        self.assertIsNone(manager.active_id)

    def test_restart_marks_stale_activity_interrupted(self):
        store = TaskStore(self.root / "data" / "tasks.sqlite3")
        store.save(dict(id="stale", task="未完成", status="running"))
        store.close()
        manager = self.manager()
        record = manager.store.get("stale")
        self.assertEqual(record["status"], "interrupted")
        self.assertEqual(record["stop_reason"], "service_restart")

    def test_second_service_cannot_own_same_data_directory(self):
        self.manager()
        with self.assertRaisesRegex(RuntimeError, "另一个"):
            TaskManager(self.root / "data", self.root / "logs")

    def test_delete_session_cleans_history_events_logs_and_survives_restart(self):
        manager = self.manager()
        output_dir = self.root / "outputs"
        output_dir.mkdir()
        output = output_dir / "note.txt"
        output.write_text("保留产物", encoding="utf-8")
        first = manager.submit("读取说明", AgentOptions(workdir=str(output_dir)))
        wait_terminal(manager, first["id"])
        second = manager.submit("追问 [history]", AgentOptions(), first["session_id"])
        wait_terminal(manager, second["id"])
        other = manager.store.create_session("另一个会话")
        deleted = manager.delete_session(first["session_id"])
        self.assertEqual(deleted, {"id": first["session_id"], "deleted_tasks": 2, "retained_logs": 0})
        for task_id in (first["id"], second["id"]):
            with self.assertRaises(KeyError):
                manager.store.get(task_id)
            self.assertEqual(manager.store.events(task_id), [])
            self.assertFalse((self.root / "logs" / f"{task_id}.trace.jsonl").exists())
        with self.assertRaises(KeyError):
            manager.store.session_history(first["session_id"])
        self.assertEqual(output.read_text(encoding="utf-8"), "保留产物")
        manager.close()
        self.managers.remove(manager)
        reopened = self.manager()
        self.assertEqual([session["id"] for session in reopened.store.list_sessions()], [other["id"]])
        self.assertEqual(reopened.store.list(), [])

    def test_delete_log_failure_reports_partial_cleanup(self):
        manager = self.manager()
        session = manager.store.create_session()
        manager.store.save(dict(id="fixture", session_id=session["id"], task="test", status="completed"))
        with patch.object(Path, "unlink", side_effect=PermissionError("日志被占用")):
            deleted = manager.delete_session(session["id"])
        self.assertEqual(deleted["retained_logs"], 1)
        with self.assertRaises(KeyError):
            manager.store.get_session(session["id"])

    def test_delete_never_removes_log_outside_trace_directory(self):
        manager = self.manager()
        session = manager.store.create_session()
        outside = self.root / "outside.trace.jsonl"
        outside.write_text("保留", encoding="utf-8")
        manager.store.save(dict(id="../outside", session_id=session["id"], task="legacy", status="completed"))
        self.assertEqual(manager.delete_session(session["id"])["retained_logs"], 1)
        self.assertTrue(outside.exists())

    def test_file_trace_failure_does_not_disable_ui_events(self):
        channel = queue.Queue()
        file_sink = Mock()
        file_sink.emit.side_effect = OSError("disk unavailable")
        trace = TaskTrace(channel, file_sink)
        trace.emit({"event": "model_started"})
        trace.emit({"event": "model_finished"})
        values = [channel.get_nowait()["data"]["event"] for _ in range(3)]
        self.assertEqual(values, ["trace_warning", "model_started", "model_finished"])
        file_sink.emit.assert_called_once()

    def test_trace_creation_failure_still_returns_result(self):
        manager = self.manager()
        self.root.joinpath("logs").write_text("a file, not a directory", encoding="utf-8")
        task = manager.submit("读取说明", AgentOptions())
        result = wait_terminal(manager, task["id"])
        self.assertEqual(result["status"], "completed")
        encoded = json.dumps(manager.store.events(task["id"]), ensure_ascii=False)
        self.assertIn("trace_warning", encoded)

    def test_event_storage_failure_stops_worker_and_records_failure(self):
        manager = self.manager()
        with patch.object(manager.store, "append", side_effect=OSError("event write failed")):
            task = manager.submit("监控失败 [slow]", AgentOptions())
            result = wait_terminal(manager, task["id"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_info"]["code"], "MONITOR_FAILED")
        self.assertIsNone(manager.active_id)
