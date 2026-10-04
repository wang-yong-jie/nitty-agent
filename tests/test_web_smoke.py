"""通过真实本地 HTTP 连接验证静态页面、SSE、断线后运行和续传；不操作浏览器。"""

import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.error import URLError
from urllib.request import Request, urlopen


class WebSmokeTests(unittest.TestCase):
    def test_http_stream_disconnect_resume_and_stop(self):
        root = Path(__file__).resolve().parents[1]
        if not (root / "frontend" / "dist" / "index.html").exists():
            self.skipTest("先执行前端 npm run build，才能验证静态页面。")
        with tempfile.TemporaryDirectory() as data, socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
            reservation.close()
            process = subprocess.Popen(
                [sys.executable, "-m", "tests.run_web_fixture", "--port", str(port), "--data-dir", data],
                cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            base = f"http://127.0.0.1:{port}"

            def request(path, body=None):
                req = Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                              headers={"Content-Type": "application/json"} if body is not None else {})
                with urlopen(req, timeout=5) as response:
                    return response.read()

            try:
                deadline = time.monotonic() + 15
                while True:
                    try:
                        request("/api/capabilities")
                        break
                    except URLError:
                        if time.monotonic() > deadline:
                            self.fail("HTTP 服务未能启动。")
                        time.sleep(0.1)
                html = request("/").decode()
                self.assertIn('lang="zh-CN"', html)
                self.assertIn('/assets/', html)
                # 长任务保证首次事件在终态之前发出。
                task = json.loads(request("/api/tasks", {"task": "HTTP 验证 [slow]"}))
                path = f"/api/tasks/{task['id']}"
                with urlopen(base + path + "/events/stream", timeout=5) as stream:
                    first_id = None
                    for raw in stream:
                        line = raw.decode().strip()
                        if line.startswith("id:"):
                            first_id = int(line[3:].strip())
                            break
                    self.assertIsNotNone(first_id)
                # 客户端断线不能取消工作进程。
                self.assertEqual(json.loads(request(path))["status"], "running")
                self.assertEqual(json.loads(request(path + "/stop", {}))["status"], "stopping")
                deadline = time.monotonic() + 15
                while json.loads(request(path))["status"] in ("running", "stopping"):
                    if time.monotonic() > deadline:
                        self.fail("停止后未收到终态。")
                    time.sleep(0.05)
                self.assertEqual(json.loads(request(path))["status"], "cancelled")
                with urlopen(Request(base + path + "/events/stream", headers={"Last-Event-ID": str(first_id)}), timeout=5) as stream:
                    replay = stream.read().decode()
                self.assertIn("event: stream_end", replay)
                ids = [int(line[3:].strip()) for line in replay.splitlines() if line.startswith("id:")]
                self.assertTrue(all(event_id > first_id for event_id in ids))
            finally:
                process.terminate()
                process.wait(timeout=10)
