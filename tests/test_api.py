"""本地 API、请求隔离、SSE 续传及 OpenAPI 契约验证。"""

from dataclasses import fields
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from api.app import create_app
from api.schemas import RunOptions
from bootstrap import AgentOptions
from tests.run_web_fixture import create_fixture_app
from tests.service_workers import wait_terminal


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.app = create_fixture_app(self.root)
        self.context = TestClient(self.app, base_url="http://127.0.0.1")
        self.client = self.context.__enter__()

    def tearDown(self):
        self.context.__exit__(None, None, None)
        self.directory.cleanup()

    def submit(self, task="读取说明"):
        response = self.client.post("/api/tasks", json={"task": task})
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()

    def test_submit_result_history_and_stop_completed_is_idempotent(self):
        task = self.submit()
        self.assertEqual(task["status"], "running")
        self.assertIn("id", task)
        wait_terminal(self.app.state.manager, task["id"])
        result = self.client.get(f"/api/tasks/{task['id']}").json()
        self.assertEqual(result["status"], "completed")
        self.assertIn("离线验证完成", result["answer"])
        self.assertEqual(self.client.get("/api/tasks").json()[0]["id"], task["id"])
        stopped = self.client.post(f"/api/tasks/{task['id']}/stop", json={}).json()
        self.assertEqual(stopped["status"], "completed")

    def test_sessions_followup_options_and_history_visibility(self):
        session = self.client.post("/api/sessions", json={}).json()
        first = self.client.post("/api/tasks", json={"task": "项目叫海蓝", "session_id": session["id"],
                                                   "options": {"max_turns": 8}}).json()
        wait_terminal(self.app.state.manager, first["id"])
        followup_response = self.client.post("/api/tasks", json={"task": "项目名称 [history]", "session_id": session["id"]})
        self.assertEqual(followup_response.status_code, 202, followup_response.text)
        followup = followup_response.json()
        self.assertEqual(followup["options"]["max_turns"], 8)
        result = wait_terminal(self.app.state.manager, followup["id"])
        self.assertIn("项目叫海蓝", result["answer"])
        tasks = self.client.get(f"/api/sessions/{session['id']}/tasks").json()
        self.assertEqual([task["id"] for task in tasks], [first["id"], followup["id"]])
        detail = self.client.get(f"/api/sessions/{session['id']}").json()
        self.assertNotIn("messages", detail)
        self.assertEqual(self.client.get("/api/sessions").json()[0]["id"], session["id"])
        for path in ("/api/sessions/missing", "/api/sessions/missing/tasks"):
            self.assertEqual(self.client.get(path).status_code, 404)
        self.assertEqual(self.client.post("/api/tasks", json={"task": "追问", "session_id": "missing"}).status_code, 404)

    def test_busy_and_cancellation(self):
        task = self.submit("等待 [slow]")
        self.assertEqual(self.client.post("/api/tasks", json={"task": "第二个"}).status_code, 409)
        stop = self.client.post(f"/api/tasks/{task['id']}/stop", json={})
        self.assertEqual(stop.json()["status"], "stopping")
        result = wait_terminal(self.app.state.manager, task["id"])
        self.assertEqual(result["status"], "cancelled")

    def test_event_replay_respects_last_event_id_and_closes_at_terminal(self):
        task = self.submit()
        wait_terminal(self.app.state.manager, task["id"])
        path = f"/api/tasks/{task['id']}/events"
        events = self.client.get(path).json()
        cursor = events[-3]["id"]
        replay = self.client.get(path + "/stream?after=1", headers={"Last-Event-ID": str(cursor)})
        self.assertIn("text/event-stream", replay.headers["content-type"])
        self.assertIn("event: stream_end", replay.text)
        payloads = [json.loads(line[6:]) for line in replay.text.splitlines() if line.startswith("data: ")]
        self.assertEqual([item["id"] for item in payloads if "id" in item], [e["id"] for e in events if e["id"] > cursor])
        self.assertEqual(self.client.get(path + "/stream", headers={"Last-Event-ID": "bad"}).status_code, 400)

    def test_invalid_requests_and_unknown_tasks(self):
        for body in ({"task": " "}, {"task": "test", "api_key": "hidden"},
                     {"task": "test", "options": {"api_key": "hidden"}},
                     {"task": "test", "options": {"max_turns": True}},
                     {"task": "test", "options": {"vision_mode": "separate"}}):
            with self.subTest(body=body):
                self.assertEqual(self.client.post("/api/tasks", json=body).status_code, 422)
        self.assertEqual(self.client.get("/api/tasks/missing").status_code, 404)
        self.assertEqual(self.client.get("/api/tasks/missing/events/stream").status_code, 404)
        self.assertEqual(self.client.post("/api/tasks/missing/stop", json={}).status_code, 404)

    def test_foreign_origin_and_host_cannot_trigger_local_actions(self):
        response = self.client.post("/api/tasks", json={"task": "test"}, headers={"Origin": "https://evil.example"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.client.get("/api/tasks", headers={"Host": "evil.example"}).status_code, 403)
        self.assertEqual(self.client.post("/api/tasks", content='{"task":"test"}',
                                          headers={"Content-Type": "text/plain"}).status_code, 415)
        preflight = self.client.options("/api/tasks", headers={
            "Origin": "http://127.0.0.1:5173", "Access-Control-Request-Method": "POST",
        })
        self.assertEqual(preflight.status_code, 200)
        self.assertEqual(self.client.get("/api/tasks").json(), [])

    def test_capabilities_return_only_key_presence_and_api_does_not_accept_keys(self):
        with tempfile.TemporaryDirectory() as other, patch.dict(os.environ, {"OPENAI_API_KEY": "private-unit-key"}), patch("bootstrap.load_dotenv"):
            app = create_app(data_dir=Path(other), trace_dir=Path(other) / "logs")
            with TestClient(app, base_url="http://127.0.0.1") as client:
                response = client.get("/api/capabilities")
                self.assertTrue(response.json()["providers"]["openai"])
                self.assertNotIn("private-unit-key", response.text)
        self.assertEqual(set(RunOptions.model_fields), {field.name for field in fields(AgentOptions)})

    def test_openapi_snapshot_matches_current_api(self):
        snapshot = Path(__file__).resolve().parents[1] / "frontend" / "openapi.json"
        self.assertEqual(json.loads(snapshot.read_text(encoding="utf-8")), self.app.openapi())
