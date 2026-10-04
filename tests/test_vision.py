"""双视觉模式的离线测试；网络模型和 Windows 输入均使用替身。"""

import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from io import StringIO
from types import SimpleNamespace
from unittest.mock import Mock, patch

import cli as main
from core.agent import Agent
from contracts import AgentCancelled, ImageContent, Message, ModelReply, VisionAdapter, VisionAnswer, VisualTarget
from adapters.windows_desktop import WindowsDesktop
from tools.desktop import register_desktop_tools
from adapters.models import TextOnlyModel
from tests.fakes import FakeBackend, MemoryEnvironment, SequenceModel, call
from core.tooling import ToolExecutor, ToolRegistry
from adapters.vision import ModelVisionAdapter


def visual_reply(answer="保存按钮位于窗口右下方。", targets=None, status="answered"):
    return ModelReply(json.dumps({
        "status": status, "answer": answer, "targets": targets or [],
    }, ensure_ascii=False))


class VisionAdapterTests(unittest.TestCase):
    def test_question_and_image_are_sent_without_tools_or_shared_history(self):
        model = SequenceModel(
            visual_reply(targets=[{"label": "保存", "x": 500, "y": 300}]),
            visual_reply("仍显示未保存标记。"),
        )
        vision, image = ModelVisionAdapter(model), ImageContent("test-image")
        result = vision.answer("找到保存按钮", image, width=640, height=360)
        self.assertEqual(result.targets, [VisualTarget("保存", 500, 300)])
        vision.answer("检查是否保存成功", image, width=640, height=360)
        first, second = model.requests
        self.assertEqual(first[1], [])
        self.assertEqual(len(first[0]), 2)
        self.assertEqual(first[0][1].images, [image])
        self.assertIn("找到保存按钮", first[0][1].content)
        self.assertIn("640", first[0][1].content)
        self.assertIn("检查是否保存成功", second[0][1].content)
        self.assertNotIn("找到保存按钮", second[0][1].content)
        self.assertEqual(len(second[0]), 2)

    def test_uncertain_and_not_found_answers_are_explicit(self):
        for status in ("not_found", "ambiguous", "uncertain"):
            with self.subTest(status=status):
                model = SequenceModel(visual_reply("无法确定目标。", status=status))
                answer = ModelVisionAdapter(model).answer("找保存按钮", ImageContent("image"), width=640, height=360)
                self.assertEqual(answer.status, status)
                self.assertEqual(answer.targets, [])

    def test_complete_json_fence_is_supported(self):
        reply = visual_reply("文件名仍为未命名。")
        reply.content = "```json\n" + reply.content + "\n```"
        result = ModelVisionAdapter(SequenceModel(reply)).answer("查看文件名", ImageContent("image"), width=640, height=360)
        self.assertEqual(result.answer, "文件名仍为未命名。")

    def test_malformed_or_unsafe_results_are_rejected(self):
        valid = {"status": "answered", "answer": "已找到。", "targets": []}
        cases = ["not json", "null", "[]", "{}", json.dumps({**valid, "frame_id": "forged"})]
        cases += [json.dumps({**valid, **update}) for update in (
            {"status": "success"}, {"answer": " "}, {"answer": "x" * 4001}, {"targets": None},
            {"targets": [{"label": "保存", "x": x, "y": 10} for x in range(21)]},
            {"targets": [{"label": "", "x": 1, "y": 2}]},
            {"targets": [{"label": "保存", "x": True, "y": 2}]},
            {"targets": [{"label": "保存", "x": 640, "y": 2}]},
            {"targets": [{"label": "保存", "x": 1, "y": -1}]},
            {"targets": [{"label": "保存", "x": 1.5, "y": 2}]},
            {"targets": [{"label": "保存", "x": 1}]},
            {"status": "ambiguous", "targets": [{"label": "保存", "x": 1, "y": 2}]},
        )]
        for content in cases:
            with self.subTest(content=content[:80]), self.assertRaises(ValueError):
                ModelVisionAdapter(SequenceModel(ModelReply(content))).answer(
                    "找保存按钮", ImageContent("image"), width=640, height=360,
                )

    def test_vlm_cannot_issue_tool_calls(self):
        model = SequenceModel(ModelReply(tool_calls=[call("click", "desktop_click", {"x": 1, "y": 2})]))
        with self.assertRaisesRegex(ValueError, "不能发出工具调用"):
            ModelVisionAdapter(model).answer("找保存按钮", ImageContent("image"), width=640, height=360)

    def test_text_only_boundary_preserves_original_messages_and_pairing(self):
        image = ImageContent("private-image")
        messages = [
            Message("user", "任务", images=[image]),
            Message("assistant", tool_calls=[call("a", "desktop_ask", {"question": "找按钮"})]),
            Message("tool", "文字观察", tool_call_id="a", images=[image]),
        ]
        delegate = SequenceModel(ModelReply("已完成"))
        TextOnlyModel(delegate).generate(messages, [])
        sent = delegate.requests[0][0]
        self.assertTrue(all(not message.images for message in sent))
        self.assertEqual(sent[-1].tool_call_id, "a")
        self.assertEqual(sent[-1].content, "文字观察")
        self.assertEqual(messages[0].images, [image])
        self.assertEqual(messages[-1].images, [image])


class DesktopVisionTests(unittest.TestCase):
    def setUp(self):
        self.backend = FakeBackend()
        self.desktop = WindowsDesktop(backend=self.backend, max_image_size=640, settle_seconds=0)
        self.addCleanup(self.desktop.close)
        self.vision = Mock(spec=VisionAdapter)
        self.vision.answer.return_value = VisionAnswer("answered", "找到保存按钮。", [VisualTarget("保存", 100, 50)])
        self.registry = ToolRegistry()
        register_desktop_tools(self.registry, self.desktop, vision=self.vision)
        self.executor = ToolExecutor(self.registry, MemoryEnvironment())

    def ask(self, question="找到保存按钮"):
        return self.executor.execute(call("ask", "desktop_ask", {"question": question}))

    def test_question_result_is_bound_to_a_fresh_usable_frame(self):
        previous = self.desktop.screenshot().data["frame_id"]
        result = self.ask()
        self.assertIsNone(result.error)
        self.assertNotEqual(result.result["frame_id"], previous)
        self.assertEqual(result.result["frame_id"], self.desktop.frame.id)
        self.assertEqual(result.result["question"], "找到保存按钮")
        self.assertEqual(self.vision.answer.call_args.kwargs, {"width": 640, "height": 360})
        target = result.result["vision"]["targets"][0]
        self.desktop.perform("click", frame_id=result.result["frame_id"], x=target["x"], y=target["y"])
        self.assertEqual(self.backend.sent, [("click", {"x": 200, "y": 100, "button": "left", "clicks": 1})])

    def test_screenshots_and_actions_do_not_automatically_call_vision(self):
        shot = self.executor.execute(call("s", "desktop_screenshot", {}))
        result = self.executor.execute(call("k", "desktop_hotkey", {
            "frame_id": shot.result["frame_id"], "keys": ["ctrl", "s"],
        }))
        self.assertIsNone(result.error)
        self.vision.answer.assert_not_called()

    def test_fresh_question_invalidates_previous_coordinates(self):
        first, second = self.ask(), self.ask("检查保存状态")
        self.assertNotEqual(first.result["frame_id"], second.result["frame_id"])
        result = self.executor.execute(call("click", "desktop_click", {
            "frame_id": first.result["frame_id"], "x": 100, "y": 50,
        }))
        self.assertIn("过期", result.error)
        self.assertEqual(self.backend.sent, [])

    def test_window_change_during_vision_discards_the_answer(self):
        def changed(*args, **kwargs):
            self.backend.window += 1
            return VisionAnswer("answered", "旧窗口中的按钮。", [VisualTarget("保存", 100, 50)])
        self.vision.answer.side_effect = changed
        result = self.ask()
        self.assertIn("前台窗口", result.error)
        self.assertIsNone(result.result)
        self.assertEqual(result.images, [])
        self.assertIsNone(self.desktop.frame)

    def test_expired_frame_during_vision_is_rejected(self):
        def expired(*args, **kwargs):
            self.desktop.frame = replace(self.desktop.frame, captured_at=self.desktop.frame.captured_at - 121)
            return VisionAnswer("answered", "旧画面。")
        self.vision.answer.side_effect = expired
        self.assertIn("过时", self.ask().error)

    def test_stop_during_vision_cancels_even_if_transport_also_fails(self):
        for fail in (False, True):
            self.backend.cancelled = False
            def stopped(*args, **kwargs):
                self.backend.cancelled = True
                if fail:
                    raise ConnectionError("VLM unavailable")
                return VisionAnswer("answered", "完成。")
            self.vision.answer.side_effect = stopped
            planner = SequenceModel(ModelReply(tool_calls=[call("a", "desktop_ask", {"question": "检查状态"})]))
            agent = Agent(TextOnlyModel(planner), self.registry, MemoryEnvironment(), verbose=False,
                          cancel_check=self.desktop.check_cancelled)
            with self.subTest(fail=fail), self.assertRaises(AgentCancelled):
                agent.run("检查")
            self.assertEqual(agent.last_state.status, "cancelled")
            self.assertEqual(self.backend.sent, [])

    def test_invalid_questions_do_not_capture_or_call_vision(self):
        for question in (None, 42, "", "  ", "x" * 2001):
            with self.subTest(question=str(question)[:20]):
                result = self.ask(question)
                self.assertEqual(result.status, "rejected")
                self.assertEqual(result.error_info.code, "INVALID_ARGUMENTS")
                self.assertEqual(result.error_info.side_effects, "none")
        self.assertIsNone(self.desktop.frame)
        self.vision.answer.assert_not_called()

    def test_capture_and_vision_errors_are_tool_errors(self):
        self.backend.fail_capture = True
        self.assertIsNotNone(self.ask().error)
        self.vision.answer.assert_not_called()
        self.backend.fail_capture = False
        self.vision.answer.side_effect = ConnectionError("VLM unavailable")
        self.assertIn("ConnectionError", self.ask().error)
        self.assertEqual(self.backend.sent, [])

    def test_custom_vision_adapter_cannot_return_out_of_bounds_coordinates(self):
        self.vision.answer.return_value = VisionAnswer("answered", "找到按钮。", [VisualTarget("保存", 9999, 0)])
        self.assertIn("截图范围", self.ask().error)

    def test_ask_and_action_batch_is_rejected_before_any_side_effect(self):
        planner = SequenceModel(ModelReply(tool_calls=[
            call("a", "desktop_ask", {"question": "找到保存按钮"}),
            call("b", "desktop_click", {"frame_id": "guessed", "x": 1, "y": 2}),
        ]), ModelReply("改为逐步执行"))
        agent = Agent(TextOnlyModel(planner), self.registry, MemoryEnvironment(), verbose=False)
        agent.run("保存")
        self.assertTrue(all(o.error for o in agent.last_state.observations))
        self.assertIsNone(self.desktop.frame)
        self.vision.answer.assert_not_called()

    def test_full_text_planner_locate_act_verify_loop(self):
        vlm = SequenceModel(
            visual_reply(targets=[{"label": "保存", "x": 100, "y": 50}]),
            visual_reply("未保存标记已消失，画面与已保存状态一致。"),
        )
        registry = ToolRegistry()
        register_desktop_tools(registry, self.desktop, vision=ModelVisionAdapter(vlm))
        requests = []

        def plan(messages, tools):
            requests.append(messages)
            self.assertTrue(all(not message.images for message in messages))
            step = len(requests)
            self.assertEqual(len(vlm.requests), {1: 0, 2: 0, 3: 1, 4: 1, 5: 2}[step])
            if step == 1:
                return ModelReply(tool_calls=[call("screen", "desktop_screenshot", {})])
            if step == 2:
                return ModelReply(tool_calls=[call("locate", "desktop_ask", {"question": "找到保存按钮"})])
            if step == 3:
                result = json.loads(messages[-1].content)["result"]
                target = result["vision"]["targets"][0]
                return ModelReply(tool_calls=[call("click", "desktop_click", {
                    "frame_id": result["frame_id"], "x": target["x"], "y": target["y"],
                })])
            if step == 4:
                return ModelReply(tool_calls=[call("verify", "desktop_ask", {"question": "检查是否保存成功"})])
            self.assertIn("未保存标记已消失", messages[-1].content)
            return ModelReply("已根据画面确认保存状态。")

        agent = Agent(TextOnlyModel(SimpleNamespace(generate=plan)), registry, MemoryEnvironment(), max_turns=5)
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(agent.run("保存文件并检查"), "已根据画面确认保存状态。")
        self.assertEqual(len(self.backend.sent), 1)
        self.assertEqual(len(vlm.requests), 2)
        self.assertTrue(all(r[0][1].images for r in vlm.requests))
        self.assertIn("找到保存按钮", vlm.requests[0][0][1].content)
        self.assertIn("检查是否保存成功", vlm.requests[1][0][1].content)
        for observation in agent.last_state.observations:
            self.assertEqual(len(observation.images), 1)
            self.assertNotIn(observation.images[0].data, output.getvalue())


class VisionCliTests(unittest.TestCase):
    def run_cli(self, arguments, env=None):
        with (
            patch("sys.argv", ["main.py", *arguments]),
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "planner-key", **(env or {})}, clear=True),
            patch("builtins.input", return_value="检查界面"),
            patch.object(main, "load_dotenv"), patch.object(main, "OpenAI") as clients,
            patch.object(main, "ModelVisionAdapter", wraps=ModelVisionAdapter) as vision,
            patch.object(main, "Agent") as agent,
            patch("adapters.windows_desktop.WindowsDesktop") as desktop,
            redirect_stdout(StringIO()), redirect_stderr(StringIO()),
        ):
            agent.return_value.run.return_value = "done"
            main.main()
        return SimpleNamespace(clients=clients, vision=vision, agent=agent, desktop=desktop)

    def test_separate_models_have_independent_providers_keys_urls_and_cleanup(self):
        result = self.run_cli([
            "--desktop", "--vision-mode", "separate", "--with-local-tools",
            "--vision-provider", "openai", "--vision-model", "test-vlm",
            "--vision-base-url", "https://vision.example/v1",
        ], {"VISION_API_KEY": "vision-key"})
        args = result.agent.call_args.args
        self.assertIsInstance(args[0], TextOnlyModel)
        self.assertEqual(args[0].delegate.model, "deepseek-flash")
        self.assertEqual(len(args[1].specs()), 14)
        self.assertIn("app_find", {s.name for s in args[1].specs()})
        self.assertIn("desktop_ask", {s.name for s in args[1].specs()})
        vision_model = result.vision.call_args.args[0]
        self.assertEqual(vision_model.model, "test-vlm")
        self.assertIsNone(vision_model.extra_body)
        self.assertIsNotNone(args[0].delegate.extra_body)
        planner_config, vision_config = [c.kwargs for c in result.clients.call_args_list]
        self.assertEqual(planner_config["api_key"], "planner-key")
        self.assertEqual(vision_config["api_key"], "vision-key")
        self.assertEqual(vision_config["base_url"], "https://vision.example/v1")
        result.desktop.return_value.__exit__.assert_called_once()
        self.assertEqual(result.clients.return_value.__exit__.call_count, 2)

    def test_env_vision_settings_support_claude_and_provider_key_fallback(self):
        result = self.run_cli(["--desktop", "--vision-mode", "separate"], {
            "VISION_PROVIDER": "claude", "VISION_MODEL": "test-claude-vlm", "ANTHROPIC_API_KEY": "claude-key",
        })
        vision_model = result.vision.call_args.args[0]
        self.assertIsInstance(vision_model, main.ClaudeModel)
        self.assertEqual(vision_model.api_key, "claude-key")
        self.assertEqual(vision_model.model, "test-claude-vlm")
        self.assertEqual(result.clients.call_count, 1)

    def test_cli_overrides_env_vision_config(self):
        result = self.run_cli([
            "--desktop", "--vision-mode", "separate", "--vision-provider", "deepseek",
            "--vision-model", "cli-vlm", "--vision-base-url", "https://cli.example",
        ], {"VISION_PROVIDER": "openai", "VISION_MODEL": "env-vlm", "VISION_BASE_URL": "https://env.example"})
        self.assertEqual(result.vision.call_args.args[0].model, "cli-vlm")
        self.assertEqual(result.clients.call_args.kwargs["base_url"], "https://cli.example")
        self.assertEqual(result.clients.call_args.kwargs["api_key"], "planner-key")

    def test_direct_default_ignores_optional_vision_env(self):
        result = self.run_cli(["--desktop"], {"VISION_MODEL": "unused-vlm", "VISION_PROVIDER": "invalid"})
        self.assertNotIsInstance(result.agent.call_args.args[0], TextOnlyModel)
        self.assertEqual(len(result.agent.call_args.args[1].specs()), 10)
        result.vision.assert_not_called()
        self.assertEqual(result.clients.call_count, 1)

    def test_invalid_mode_configuration_is_rejected_before_model_or_desktop_creation(self):
        for arguments, env in (
            (["--vision-mode", "separate", "--vision-model", "vlm"], {}),
            (["--desktop", "--vision-model", "vlm"], {}),
            (["--desktop", "--vision-mode", "separate"], {}),
            (["--desktop", "--vision-mode", "separate"], {"VISION_MODEL": "vlm", "VISION_PROVIDER": "invalid"}),
        ):
            with self.subTest(arguments=arguments, env=env), patch.object(main, "_create_model") as create:
                with self.assertRaises(SystemExit):
                    self.run_cli(arguments, env)
                create.assert_not_called()

    def test_missing_vision_key_does_not_reuse_unrelated_planner_key(self):
        with self.assertRaisesRegex(RuntimeError, "VISION_API_KEY.*OPENAI_API_KEY"):
            self.run_cli(["--desktop", "--vision-mode", "separate", "--vision-provider", "openai", "--vision-model", "vlm"])


if __name__ == "__main__":
    unittest.main()
