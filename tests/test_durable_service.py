"""真实 spawn、SQLite 保存确认及服务重启后的原任务恢复。"""

from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
from api.app import create_app
from bootstrap import AgentOptions
from service.manager import TaskManager
from tests.service_workers import durable_crash_worker, wait_terminal


class DurableServiceTests(unittest.TestCase):
    def test_old_checkpoint_cannot_replace_later_conversation(self):
        from contracts import Message
        from core.session import Session
        from service.manager import TaskBusy
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manager = TaskManager(root / "data", root / "logs", worker_target=durable_crash_worker)
            try:
                record = manager.submit("保存评测文件", AgentOptions(workdir=str(root), long_horizon=True))
                failed = wait_terminal(manager, record["id"])
                later = {**failed, "id": "later-task", "task": "后续问题", "status": "completed", "answer": "后续答案",
                         "created_at": "2099-01-01T00:00:00Z", "resume_available": False}
                manager.store.save(later)
                session = Session.from_dict(manager.store.session_history(record["session_id"]))
                session.update([*session.messages, Message("user", "后续问题"), Message("assistant", "后续答案")])
                manager.store.save_session_history(session.to_dict())
                before = manager.store.session_history(record["session_id"])
                with self.assertRaisesRegex(TaskBusy, "已有后续任务"):
                    manager.resume(record["id"], failed["checkpoint_revision"])
                self.assertEqual(manager.store.session_history(record["session_id"]), before)
                self.assertFalse(manager.store.get(record["id"])["resume_available"])
                self.assertFalse(next(x for x in manager.store.session_tasks(record["session_id"]) if x["id"] == record["id"])["resume_available"])
                self.assertEqual((root / "write-count.txt").read_text(), "1")
            finally:
                manager.close()

    def test_process_death_after_write_and_restart_resume_do_not_duplicate_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manager = TaskManager(root / "data", root / "logs", worker_target=durable_crash_worker)
            try:
                record = manager.submit("保存评测文件并验收", AgentOptions(workdir=str(root), long_horizon=True))
                failed = wait_terminal(manager, record["id"])
                self.assertEqual(failed["status"], "failed")
                self.assertTrue(failed["resume_available"])
                revision, snapshot = manager.store.checkpoint(record["id"])
                self.assertEqual(snapshot["phase"], "executing")
                self.assertEqual(snapshot["memory"]["actions"][-1]["status"], "started")
                self.assertEqual((root / "write-count.txt").read_text(), "1")
            finally:
                manager.close()

            manager = TaskManager(root / "data", root / "logs", worker_target=durable_crash_worker)
            try:
                with self.assertRaisesRegex(ValueError, "已更新"):
                    manager.resume(record["id"], revision - 1)
                running = manager.resume(record["id"], revision)
                self.assertEqual(running["id"], record["id"])
                self.assertEqual(running["attempt"], 2)
                completed = wait_terminal(manager, record["id"])
                self.assertEqual(completed["status"], "completed", completed.get("error"))
                self.assertFalse(completed["resume_available"])
                self.assertEqual(completed["progress"]["milestones"][0]["status"], "completed")
                self.assertEqual((root / "write-count.txt").read_text(), "1")
                self.assertTrue((root / "logs" / f"{record['id']}.attempt-2.trace.jsonl").is_file())
                manager.delete_session(record["session_id"])
                with self.assertRaises(KeyError):
                    manager.store.checkpoint(record["id"])
                self.assertFalse((root / "logs" / f"{record['id']}.attempt-2.trace.jsonl").exists())
            finally:
                manager.close()

    def test_resume_api_checks_revision_origin_and_completion(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            def factory(data_dir, trace_dir):
                return TaskManager(data_dir, trace_dir, worker_target=durable_crash_worker)
            app = create_app(data_dir=root / "data", trace_dir=root / "logs", manager_factory=factory,
                             validate_options=lambda options: None)
            with TestClient(app, base_url="http://127.0.0.1") as client:
                response = client.post("/api/tasks", json={"task": "保存评测文件", "options": {
                    "workdir": str(root), "long_horizon": True}})
                self.assertEqual(response.status_code, 202, response.text)
                record = response.json()
                failed = wait_terminal(app.state.manager, record["id"])
                path = f"/api/tasks/{record['id']}/resume"
                body = {"expected_revision": failed["checkpoint_revision"]}
                self.assertEqual(client.post(path, json=body, headers={"Origin": "https://evil.example"}).status_code, 403)
                self.assertEqual(client.post(path, json={"expected_revision": 0}).status_code, 422)
                self.assertEqual(client.post(path, json={"expected_revision": body["expected_revision"] + 10}).status_code, 409)
                response = client.post(path, json=body)
                self.assertEqual(response.status_code, 202, response.text)
                completed = wait_terminal(app.state.manager, record["id"])
                self.assertEqual(completed["status"], "completed", completed.get("error"))
                self.assertEqual(client.post(path, json=body).status_code, 422)
                self.assertEqual(client.post("/api/tasks/missing/resume", json=body).status_code, 404)
