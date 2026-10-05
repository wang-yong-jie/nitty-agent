"""桌面优化的几何、稳定、恢复、验证与录制边界。"""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from adapters.desktop_workflow import DesktopWorkflow
from adapters.vision import ModelVisionAdapter
from adapters.windows_desktop import WindowsDesktop
from contracts import ModelReply, ImageContent, ToolRejected, ToolExecutionError
from core.agent import Agent
from evaluation.desktop_benchmark import summarize
from tests.fakes import FakeBackend, MemoryEnvironment, SequenceModel
from tools.desktop import register_desktop_tools
from core.tooling import ToolRegistry, ToolExecutor
from tests.fakes import call


class DesktopWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.backend = FakeBackend()
        self.desktop = WindowsDesktop(backend=self.backend, max_image_size=640, settle_seconds=0,
                                     stability_timeout=0.2, stability_interval=0.01)
        self.addCleanup(self.desktop.close)

    def test_crop_uses_native_pixels_maps_input_and_consumes_old_frame(self):
        original = self.desktop.screenshot()
        crop = self.desktop.crop(original.data["frame_id"], 100, 80, 120, 60)
        self.assertEqual((crop.data["image_width"], crop.data["image_height"]), (240, 120))
        self.assertEqual(crop.data["screen_left"], 200)
        self.assertEqual(crop.data["screen_top"], 160)
        self.assertEqual(crop.data["parent_frame_id"], original.data["frame_id"])
        with self.assertRaises(ToolRejected):
            self.desktop.perform("click", frame_id=original.data["frame_id"], x=1, y=1)
        self.desktop.perform("click", frame_id=crop.data["frame_id"], x=50, y=20)
        self.assertEqual(self.backend.sent[-1][1]["x"], 250)
        self.assertEqual(self.backend.sent[-1][1]["y"], 180)

    def test_crop_bounds_and_inherited_capture_time(self):
        shot = self.desktop.screenshot()
        captured_at = self.desktop.frame.captured_at
        with self.assertRaises(ToolRejected):
            self.desktop.crop(shot.data["frame_id"], 630, 0, 20, 20)
        self.desktop.crop(shot.data["frame_id"], 0, 0, 100, 100)
        self.assertEqual(self.desktop.frame.captured_at, captured_at)

    def test_unstable_screen_is_bounded_and_rejects_input(self):
        self.desktop.stability_timeout = 0.04
        count = 0
        def capture(geometry):
            nonlocal count
            count += 1
            return Image.new("RGB", geometry[2:], "white" if count % 2 else "black")
        self.backend.capture = capture
        result = self.desktop.screenshot()
        self.assertFalse(result.data["stable"])
        self.assertLess(count, 12)
        with self.assertRaisesRegex(ToolRejected, "尚未稳定"):
            self.desktop.perform("click", frame_id=result.data["frame_id"], x=20, y=20)
        self.assertEqual(self.backend.sent, [])

    def test_content_change_same_window_invalidates_frame(self):
        shot = self.desktop.screenshot()
        self.backend.capture = lambda geometry: Image.new("RGB", geometry[2:], "black")
        with self.assertRaisesRegex(ToolRejected, "内容已变化"):
            self.desktop.validate_frame(shot.data["frame_id"])

    def test_repeated_input_is_rejected_and_recovery_is_bounded(self):
        shot = self.desktop.screenshot()
        for _ in range(3):
            shot = self.desktop.perform("click", frame_id=shot.data["frame_id"], x=20, y=20)
        self.assertEqual(shot.data["recovery"]["suggestion"], "reground")
        with self.assertRaisesRegex(ToolRejected, "相同动作"):
            self.desktop.perform("click", frame_id=shot.data["frame_id"], x=20, y=20)
        self.assertEqual(len(self.backend.sent), 3)
        for x in (30, 40, 50):
            shot = self.desktop.perform("click", frame_id=shot.data["frame_id"], x=x, y=20)
        with self.assertRaisesRegex(ToolRejected, "预算"):
            self.desktop.perform("click", frame_id=shot.data["frame_id"], x=60, y=20)
        self.assertEqual(len(self.backend.sent), 6)

    def test_frame_reuse_does_not_publish_a_new_frame(self):
        vision = Mock()
        from contracts import VisionAnswer
        vision.answer.return_value = VisionAnswer("answered", "按钮可见。")
        registry = ToolRegistry()
        register_desktop_tools(registry, self.desktop, vision=vision)
        shot = self.desktop.screenshot()
        result = ToolExecutor(registry, MemoryEnvironment()).execute(call("ask", "desktop_ask", {
            "question": "读取按钮", "frame_id": shot.data["frame_id"],
        }))
        self.assertIsNone(result.error)
        self.assertEqual(result.result["frame_id"], shot.data["frame_id"])

    def test_recording_contains_images_timing_coordinates_and_no_input_text(self):
        with tempfile.TemporaryDirectory() as temporary:
            from adapters.desktop_support import DesktopRecorder
            self.desktop.recorder = DesktopRecorder(Path(temporary))
            shot = self.desktop.screenshot()
            result = self.desktop.perform("click", frame_id=shot.data["frame_id"], x=40, y=20)
            trace = result.data["trajectory"]
            self.assertEqual(trace["position"]["x_px"], 80)
            for name in ("before", "after", "target"):
                self.assertTrue((Path(temporary) / trace[name]).is_file())
            self.desktop.perform("type_text", frame_id=result.data["frame_id"], text="secret-test")
            data = (Path(temporary) / "trajectory.jsonl").read_text(encoding="utf-8")
            self.assertNotIn("secret-test", data)
            self.assertIn("settle_capture", data)

    def test_verification_reports_evidence_and_checks_frame_after_request(self):
        verifier = Mock()
        verifier.verify.return_value = {"status": "unmet", "evidence": "仍有未保存标记。"}
        workflow = DesktopWorkflow(self.desktop, verifier)
        result = workflow.verify("文件已保存")
        self.assertEqual(result.data["verification"]["status"], "unmet")
        self.assertEqual(result.data["verification"]["frame_id"], self.desktop.frame.id)
        self.assertEqual(result.data["verification"]["scope"], "step")
        def changed(*args, **kwargs):
            self.backend.window += 1
            return {"status": "achieved", "evidence": "旧画面。"}
        verifier.verify.side_effect = changed
        with self.assertRaises(ToolRejected):
            workflow.verify("文件已保存")

    def test_bad_verification_json_does_not_claim_success(self):
        for data in ({"status": "answered", "evidence": "好了"}, {"status": "achieved", "evidence": ""},
                     {"status": "achieved", "evidence": "好了", "frame_id": "forged"}):
            verifier = ModelVisionAdapter(SequenceModel(ModelReply(json.dumps(data))))
            with self.assertRaises(ValueError):
                verifier.verify("保存", ImageContent("image"))

    def test_verification_staleness_after_input_preserves_input_sent(self):
        verifier = Mock()
        def changed(*args, **kwargs):
            self.backend.window += 1
            return {"status": "achieved", "evidence": "旧画面"}
        verifier.verify.side_effect = changed
        workflow = DesktopWorkflow(self.desktop, verifier)
        registry = ToolRegistry()
        register_desktop_tools(registry, self.desktop, workflow=workflow)
        shot = self.desktop.screenshot()
        observation = ToolExecutor(registry, MemoryEnvironment()).execute(call("c", "desktop_click", {
            "frame_id": shot.data["frame_id"], "x": 20, "y": 20, "expected": "显示保存成功",
        }))
        self.assertIsNone(observation.error)
        self.assertTrue(observation.result["input_sent"])
        self.assertEqual(observation.result["verification"]["status"], "uncertain")
        self.assertEqual(len(self.backend.sent), 1)

    def test_partial_input_failure_still_requires_final_verification(self):
        verifier = Mock()
        verifier.verify.return_value = {"status": "unmet", "evidence": "尚未保存"}
        workflow = DesktopWorkflow(self.desktop, verifier)
        workflow.start()
        shot = self.desktop.screenshot()
        self.backend.send = Mock(side_effect=RuntimeError("partial input"))
        with self.assertRaises(ToolExecutionError):
            self.desktop.perform("click", frame_id=shot.data["frame_id"], x=20, y=20)
        self.assertEqual(workflow.finish("保存文件")["status"], "unmet")
        verifier.verify.assert_called_once()

    def test_final_verification_staleness_returns_uncertain_for_replanning(self):
        verifier = Mock()
        def changed(*args, **kwargs):
            self.backend.window += 1
            return {"status": "achieved", "evidence": "旧画面"}
        verifier.verify.side_effect = changed
        workflow = DesktopWorkflow(self.desktop, verifier)
        workflow.start()
        shot = self.desktop.screenshot()
        self.desktop.perform("click", frame_id=shot.data["frame_id"], x=20, y=20)
        result = workflow.finish("保存文件")
        self.assertEqual(result["status"], "uncertain")
        self.assertEqual(result["scope"], "task")
        self.assertTrue(result["frame_id"])
        self.assertIsNone(self.desktop.frame)

    def test_recovery_exhaustion_stops_run_before_another_model_request(self):
        registry = ToolRegistry()
        def exhausted(_environment):
            raise ToolRejected("RECOVERY_EXHAUSTED", "预算已耗尽")
        registry.register("blocked_input", "测试输入预算", {}, [], exhausted)
        workflow = DesktopWorkflow(self.desktop, Mock())
        model = SequenceModel(ModelReply(tool_calls=[call("c", "blocked_input", {})]), ModelReply("完成"))
        agent = Agent(model, registry, MemoryEnvironment(), verbose=False, on_observation=workflow.observe)
        with self.assertRaisesRegex(ToolExecutionError, "恢复预算"):
            agent.run("完成桌面任务")
        self.assertEqual(len(model.requests), 1)
        self.assertEqual(agent.last_state.error_info.code, "RECOVERY_EXHAUSTED")
        self.assertEqual(agent.last_state.error_info.phase, "recovery")
        self.assertEqual(agent.last_state.status, "failed")

    def test_completion_check_requires_success_and_replans_once(self):
        check = Mock(side_effect=[{"status": "unmet", "evidence": "未保存"},
                                  {"status": "achieved", "evidence": "已保存"}])
        agent = Agent(SequenceModel(ModelReply("完成"), ModelReply("已保存")), ToolRegistry(), MemoryEnvironment(),
                      completion_check=check, verbose=False)
        self.assertEqual(agent.run("保存"), "已保存")
        self.assertEqual(check.call_count, 2)
        self.assertEqual(agent.last_state.verification["status"], "achieved")

    def test_uncertain_completion_stops_instead_of_claiming_success(self):
        check = Mock(return_value={"status": "uncertain", "evidence": "画面不足"})
        agent = Agent(SequenceModel(ModelReply("好了"), ModelReply("好了")), ToolRegistry(), MemoryEnvironment(),
                      completion_check=check, verbose=False)
        with self.assertRaisesRegex(RuntimeError, "尚未验证"):
            agent.run("保存")
        self.assertEqual(agent.last_state.status, "failed")
        self.assertIsNone(agent.last_state.answer)
        self.assertEqual(agent.last_state.error_info.code, "TARGET_UNVERIFIED")
        self.assertEqual(agent.last_state.error_info.side_effects, "possible")

    def test_independent_evaluation_does_not_treat_completion_as_success(self):
        result = summarize([{"success": False, "agent_completed": True, "seconds": 10,
                             "steps": 3, "no_progress_steps": 1}])
        self.assertEqual(result["task_success_rate"], 0)
        self.assertEqual(result["completion_precision"], 0)
