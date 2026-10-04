"""桌面闭环的离线测试；所有鼠标键盘输入都使用替身，不操作真实桌面。"""

import base64
import ctypes
import json
import os
import threading
import unittest
from contextlib import redirect_stdout
from io import BytesIO, StringIO
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image

from core.agent import Agent
from core.context import ContextBuilder
from contracts import AgentCancelled, ImageContent, Message, ModelReply, ToolResult
from adapters.windows_desktop import Frame, WindowsDesktop, _WindowsBackend
from tools.desktop import DESKTOP_INSTRUCTIONS, register_desktop_tools
from adapters.local import LocalEnvironment
from adapters.models import ClaudeModel, OpenAICompatibleModel
from core.state import AgentState
from tests.fakes import FakeBackend, MemoryEnvironment, SequenceModel, call, sdk_response
from core.tooling import ToolExecutor, ToolRegistry
from tools.local import create_default_registry


class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.backend = FakeBackend()
        self.desktop = WindowsDesktop(backend=self.backend, max_image_size=640, settle_seconds=0)
        self.addCleanup(self.desktop.close)

    def frame_id(self):
        return self.desktop.screenshot().data["frame_id"]

    def test_scaled_image_and_coordinate_mapping(self):
        result = self.desktop.screenshot()
        with Image.open(BytesIO(base64.b64decode(result.images[0].data))) as image:
            self.assertEqual(image.size, (640, 360))
        self.assertEqual(result.data["scale_x"], 2)
        newer = self.desktop.perform("click", frame_id=result.data["frame_id"], x=100, y=70)
        self.assertEqual(self.backend.sent, [("click", {"x": 200, "y": 140, "button": "left", "clicks": 1})])
        self.assertNotEqual(result.data["frame_id"], newer.data["frame_id"])
        self.assertTrue(newer.data["input_sent"])
        with self.assertRaisesRegex(ValueError, "过期"):
            self.desktop.perform("click", frame_id=result.data["frame_id"], x=100, y=70)
        self.assertEqual(len(self.backend.sent), 1)

    def test_bounds_origin_and_invalid_types(self):
        frame = Frame("test", (100, -100, 2560, 1440), (1280, 720), 1, 0)
        self.assertEqual(frame.point(100, 100), (300, 100))
        for x, y in ((-1, 0), (1280, 0), (0, 720), (True, 1), (0.5, 1), ("1", 0)):
            with self.subTest(point=(x, y)), self.assertRaises(ValueError):
                frame.point(x, y)

    def test_requires_observation_and_detects_external_changes(self):
        with self.assertRaisesRegex(ValueError, "尚未截图"):
            self.desktop.perform("hotkey", frame_id="guessed", keys=["ctrl", "s"])
        frame_id = self.frame_id()
        self.backend.window += 1
        with self.assertRaisesRegex(ValueError, "前台窗口"):
            self.desktop.perform("type_text", frame_id=frame_id, text="不能输入")
        frame_id = self.frame_id()
        self.backend.screen = (0, 0, 1920, 1080)
        with self.assertRaisesRegex(ValueError, "显示设置"):
            self.desktop.perform("click", frame_id=frame_id, x=5, y=5)
        self.assertEqual(self.backend.sent, [])

    def test_rejects_old_frame_even_when_focus_unchanged(self):
        frame_id = self.frame_id()
        with patch("adapters.windows_desktop.time.monotonic", return_value=self.desktop.frame.captured_at + 121):
            with self.assertRaisesRegex(ValueError, "过时"):
                self.desktop.perform("click", frame_id=frame_id, x=5, y=5)
        self.assertEqual(self.backend.sent, [])

    def test_drag_scroll_hotkey_and_unicode(self):
        self.desktop.perform("drag", frame_id=self.frame_id(), from_x=10, from_y=20, to_x=50, to_y=60)
        self.assertEqual(self.backend.sent[-1][1]["start"], (20, 40))
        self.assertEqual(self.backend.sent[-1][1]["end"], (100, 120))
        self.desktop.perform("scroll", frame_id=self.frame_id(), x=30, y=40, amount=-3)
        self.assertEqual(self.backend.sent[-1], ("scroll", {"x": 60, "y": 80, "amount": -3}))
        self.desktop.perform("hotkey", frame_id=self.frame_id(), keys=["CTRL", "s"])
        self.assertEqual(self.backend.sent[-1], ("keys", {"keys": ["ctrl", "s"]}))
        self.desktop.perform("type_text", frame_id=self.frame_id(), text="中文🙂\r\n第二行")
        self.assertEqual(self.backend.sent[-1], ("text", {"text": "中文🙂\n第二行"}))
        before = len(self.backend.sent)
        self.desktop.perform("wait", seconds=0)
        self.assertEqual(len(self.backend.sent), before)

    def test_bad_arguments_do_not_send_input(self):
        cases = [
            ("click", {"x": 0, "y": 0, "clicks": True}),
            ("click", {"x": 0, "y": 0, "button": "invalid"}),
            ("scroll", {"x": 0, "y": 0, "amount": 999}),
            ("scroll", {"x": 0, "y": 0, "amount": 0}),
            ("hotkey", {"keys": ["f8"]}), ("hotkey", {"keys": ["ctrl", "ctrl"]}),
            ("hotkey", {"keys": ["unknown"]}), ("hotkey", {"keys": "ctrl+s"}),
            ("type_text", {"text": "a" * 2001}), ("type_text", {"text": "\x00"}),
            ("type_text", {"text": "\ud800"}),
            ("drag", {"from_x": 0, "from_y": 0, "to_x": 1, "to_y": 1, "duration": float("nan")}),
        ]
        frame_id = self.frame_id()
        for name, arguments in cases:
            with self.subTest(action=name, arguments=arguments), self.assertRaises(ValueError):
                self.desktop.perform(name, frame_id=frame_id, **arguments)
        with self.assertRaises(TypeError):
            self.desktop.perform("click", frame_id=frame_id, x=5, y=5, extra=True)
        self.assertEqual(self.backend.sent, [])

    def test_post_action_capture_failure_consumes_frame(self):
        frame_id = self.frame_id()
        self.backend.fail_capture = True
        with self.assertRaisesRegex(RuntimeError, "动作已发送"):
            self.desktop.perform("click", frame_id=frame_id, x=50, y=50)
        with self.assertRaisesRegex(ValueError, "尚未截图"):
            self.desktop.perform("click", frame_id=frame_id, x=50, y=50)
        self.assertEqual(len(self.backend.sent), 1)

    def test_action_error_does_not_allow_blind_retry(self):
        frame_id = self.frame_id()
        with patch.object(self.backend, "send", side_effect=OSError("partial")):
            with self.assertRaisesRegex(RuntimeError, "部分执行"):
                self.desktop.perform("click", frame_id=frame_id, x=5, y=5)
        self.assertIsNone(self.desktop.frame)

    def test_cancel_before_action_and_during_wait(self):
        frame_id = self.frame_id()
        self.backend.cancelled = True
        with self.assertRaises(AgentCancelled):
            self.desktop.perform("click", frame_id=frame_id, x=5, y=5)
        with self.assertRaises(AgentCancelled):
            self.desktop.perform("wait", seconds=10)
        self.assertEqual(self.backend.sent, [])


class DesktopRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.backend = FakeBackend()
        self.desktop = WindowsDesktop(backend=self.backend, max_image_size=640, settle_seconds=0)
        self.addCleanup(self.desktop.close)
        self.environment = LocalEnvironment(desktop=self.desktop)
        self.registry = ToolRegistry()
        register_desktop_tools(self.registry, self.desktop)

    def test_model_observe_act_observe_cycle_and_no_base64_logs(self):
        requests = []

        def generate(messages, tools):
            requests.append(messages)
            if len(requests) == 1:
                return ModelReply(tool_calls=[call("screen", "desktop_screenshot", {})])
            if len(requests) == 2:
                metadata = json.loads(messages[-1].content)["result"]
                self.assertEqual(len(messages[-1].images), 1)
                return ModelReply(tool_calls=[call("click", "desktop_click", {
                    "frame_id": metadata["frame_id"], "x": 100, "y": 50,
                })])
            self.assertTrue(json.loads(messages[-1].content)["result"]["input_sent"])
            self.assertEqual(len(messages[-1].images), 1)
            return ModelReply("已确认界面变化")

        agent = Agent(SimpleNamespace(generate=generate), self.registry, self.environment)
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(agent.run("点击测试位置"), "已确认界面变化")
        self.assertEqual(agent.last_state.turn, 3)
        self.assertEqual(len(agent.last_state.observations), 2)
        self.assertNotIn(agent.last_state.observations[0].images[0].data, output.getvalue())
        self.assertIsNone(self.environment.working_directory)

    def test_rejects_entire_mixed_batch_without_side_effects(self):
        side_effect = Mock()
        self.registry.register("other", "另一个工具", {}, [], side_effect)
        model = SequenceModel(ModelReply(tool_calls=[
            call("one", "other", {}), call("two", "desktop_screenshot", {}),
        ]), ModelReply("改为逐次执行"))
        agent = Agent(model, self.registry, self.environment, verbose=False)
        agent.run("混合批次")
        side_effect.assert_not_called()
        self.assertIsNone(self.desktop.frame)
        self.assertTrue(all(o.error for o in agent.last_state.observations))
        self.assertEqual([m.tool_call_id for m in model.requests[1][0][-2:]], ["one", "two"])

    def test_cancellation_is_not_recoverable_tool_error(self):
        self.backend.cancelled = True
        model = SequenceModel(ModelReply(tool_calls=[call("s", "desktop_screenshot", {})]))
        agent = Agent(model, self.registry, self.environment, verbose=False)
        with self.assertRaises(AgentCancelled):
            agent.run("停止")
        self.assertEqual(agent.last_state.status, "cancelled")
        self.assertEqual(len(model.requests), 1)

    def test_stop_while_model_is_running_prevents_returned_action(self):
        def generate(messages, tools):
            self.backend.cancelled = True
            return ModelReply(tool_calls=[call("s", "desktop_screenshot", {})])

        agent = Agent(SimpleNamespace(generate=generate), self.registry, self.environment,
                      verbose=False, cancel_check=self.desktop.check_cancelled)
        with self.assertRaises(AgentCancelled):
            agent.run("停止")
        self.assertEqual(agent.last_state.status, "cancelled")
        self.assertEqual(agent.last_state.observations, [])

    def test_keyboard_interrupt_keeps_cancelled_state(self):
        model = Mock()
        model.generate.side_effect = KeyboardInterrupt
        agent = Agent(model, self.registry, self.environment, verbose=False)
        with self.assertRaises(KeyboardInterrupt):
            agent.run("中止")
        self.assertEqual(agent.last_state.status, "cancelled")

    def test_tools_execute_without_environment_desktop(self):
        """只有文件/Shell 能力的环境，也能使用已显式绑定的桌面工具。"""
        executor = ToolExecutor(self.registry, MemoryEnvironment())
        screenshot = executor.execute(call("s", "desktop_screenshot", {}))
        self.assertIsNone(screenshot.error)
        self.assertEqual(len(screenshot.images), 1)
        result = executor.execute(call("t", "desktop_type_text", {
            "frame_id": screenshot.result["frame_id"], "text": "显式绑定的桌面",
        }))
        self.assertIsNone(result.error)
        self.assertTrue(result.result["input_sent"])
        self.assertEqual(self.backend.sent, [("text", {"text": "显式绑定的桌面"})])

    def test_environment_cannot_redirect_bound_desktop_tools(self):
        other_desktop = Mock()
        self.environment.desktop = other_desktop
        executor = ToolExecutor(self.registry, self.environment)
        result = executor.execute(call("s", "desktop_screenshot", {}))
        self.assertIsNone(result.error)
        self.assertEqual(result.result["frame_id"], self.desktop.frame.id)
        other_desktop.screenshot.assert_not_called()

    def test_missing_controller_is_rejected_before_registration(self):
        registry = ToolRegistry()
        with self.assertRaisesRegex(ValueError, "DesktopController"):
            register_desktop_tools(registry, None)
        self.assertEqual(registry.specs(), [])

    def test_desktop_rules_are_deduplicated_and_mixed_with_local_rules(self):
        registry = create_default_registry()
        register_desktop_tools(registry, self.desktop)
        model = SequenceModel(ModelReply("已完成"))
        Agent(model, registry, self.environment, verbose=False).run("测试工具规则")
        system = model.requests[0][0][0].content
        self.assertEqual(system.count(DESKTOP_INSTRUCTIONS), 1)
        self.assertIn("每次调用都是新 Shell", system)
        self.assertIn("读取被截断", system)
        self.assertIn("编辑已有文件前先读取", system)
        self.assertIsNone(self.desktop.frame)  # 注册和组装规则不产生截图或输入。


class ImageProtocolTests(unittest.TestCase):
    def test_context_prunes_images_without_changing_state(self):
        messages = [Message("tool", str(i), tool_call_id=str(i), images=[ImageContent(str(i))]) for i in range(4)]
        state = AgentState("task", messages=messages)
        result = ContextBuilder(max_images=2).build_messages(state, {}, [])
        self.assertEqual([im.data for msg in result for im in msg.images], ["2", "3"])
        self.assertEqual([len(msg.images) for msg in state.messages], [1, 1, 1, 1])
        self.assertEqual([msg.tool_call_id for msg in result[1:]], ["0", "1", "2", "3"])
        self.assertIn("旧截图", result[1].content)

    def test_openai_keeps_all_tool_results_before_image_user_message(self):
        client = Mock()
        client.chat.completions.create.return_value = sdk_response(content="done")
        adapter = OpenAICompatibleModel(client, "vision-model")
        adapter.generate([
            Message("user", "task"),
            Message("assistant", tool_calls=[call("a", "screen", {}), call("b", "other", {})]),
            Message("tool", "metadata", tool_call_id="a", images=[ImageContent("image-a")]),
            Message("tool", "other-result", tool_call_id="b"),
            Message("assistant", "continue"),
            Message("user", "new image", images=[ImageContent("image-b", "image/jpeg")]),
        ], [])
        history = client.chat.completions.create.call_args.kwargs["messages"]
        self.assertEqual([m["role"] for m in history], ["user", "assistant", "tool", "tool", "user", "assistant", "user"])
        self.assertEqual(history[2]["tool_call_id"], "a")
        self.assertEqual(history[2]["content"], "metadata")
        self.assertIn("a", history[4]["content"][0]["text"])
        self.assertEqual(history[4]["content"][1]["image_url"]["url"], "data:image/png;base64,image-a")
        self.assertEqual(history[-1]["content"][1]["image_url"]["url"], "data:image/jpeg;base64,image-b")

    def test_openai_flushes_trailing_images(self):
        client = Mock()
        client.chat.completions.create.return_value = sdk_response(content="done")
        OpenAICompatibleModel(client, "vision").generate([
            Message("assistant", tool_calls=[call("a", "screen", {})]),
            Message("tool", "metadata", tool_call_id="a", images=[ImageContent("image")]),
        ], [])
        history = client.chat.completions.create.call_args.kwargs["messages"]
        self.assertEqual(history[-1]["role"], "user")
        self.assertEqual(history[-1]["content"][-1]["type"], "image_url")

    def test_claude_images_inside_paired_tool_result(self):
        response = {"stop_reason": "end_turn", "content": [{"type": "text", "text": "done"}]}
        with patch("adapters.models.urlopen", return_value=BytesIO(json.dumps(response).encode())) as transport:
            ClaudeModel("test-key", "vision").generate([
                Message("user", "task"),
                Message("assistant", tool_calls=[call("a", "screen", {}), call("b", "other", {})]),
                Message("tool", "metadata", tool_call_id="a", images=[ImageContent("image")]),
                Message("tool", "error", tool_call_id="b", is_error=True),
            ], [])
        payload = json.loads(transport.call_args.args[0].data)
        blocks = payload["messages"][-1]["content"]
        self.assertEqual([b["tool_use_id"] for b in blocks], ["a", "b"])
        self.assertEqual(blocks[0]["content"][1]["source"], {
            "type": "base64", "media_type": "image/png", "data": "image",
        })
        self.assertTrue(blocks[1]["is_error"])


class NativeInputTests(unittest.TestCase):
    """测试真实后端的清理路径和 Win32 ABI，但将输入 API 替换为 Mock。"""

    def backend(self):
        backend = object.__new__(_WindowsBackend)
        backend.gui = Mock()
        backend.gui.FAILSAFE = True
        backend.gui.FailSafeException = type("FakeFailSafe", (Exception,), {})
        backend.user32 = Mock()
        backend.user32.GetAsyncKeyState.return_value = 0
        backend._stop = threading.Event()
        backend._cancelled = threading.Event()
        return backend

    def test_hotkey_releases_held_keys_when_cancelled(self):
        backend = self.backend()
        backend.check_cancelled = Mock(side_effect=[None, AgentCancelled("stop")])
        with self.assertRaises(AgentCancelled):
            backend._keys(["ctrl", "s"])
        backend.gui.keyDown.assert_called_once_with("ctrl", _pause=False)
        backend.gui.keyUp.assert_called_once_with("ctrl", _pause=False)
        self.assertTrue(backend.gui.FAILSAFE)

    def test_drag_releases_mouse_after_failure(self):
        backend = self.backend()
        backend.check_cancelled = Mock()
        backend.gui.moveTo.side_effect = [None, RuntimeError("move failed")]
        with self.assertRaises(RuntimeError):
            backend.send("drag", {"start": (1, 1), "end": (10, 10), "button": "left", "duration": 0.5})
        backend.gui.mouseUp.assert_called_once_with(button="left", _pause=False)

    def test_corner_stop_latches_after_pointer_moves_away(self):
        backend = self.backend()
        backend.gui.failSafeCheck.side_effect = [backend.gui.FailSafeException(), None]
        with self.assertRaises(AgentCancelled):
            backend.check_cancelled()
        with self.assertRaises(AgentCancelled):
            backend.check_cancelled()

    def test_inaccessible_desktop_is_not_misreported_as_corner_stop(self):
        backend = self.backend()
        backend.user32.GetCursorPos.return_value = False
        with self.assertRaisesRegex(RuntimeError, "无法访问当前输入桌面"):
            backend.check_cancelled()
        backend.gui.failSafeCheck.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows ABI")
    def test_unicode_sendinput_layout_and_surrogate_pairs(self):
        backend = self.backend()
        backend._setup_unicode_input()
        seen = []

        def send(count, items, size):
            seen.extend((item.type, item.value.ki.scan, item.value.ki.flags) for item in items)
            self.assertEqual(size, 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)
            return count

        backend.user32.SendInput.side_effect = send
        backend._unicode("中")
        backend._unicode("🙂")
        self.assertEqual(seen[0:2], [(1, ord("中"), 4), (1, ord("中"), 6)])
        self.assertEqual([s[1] for s in seen[2:]], [0xD83D, 0xD83D, 0xDE42, 0xDE42])
        backend.user32.SendInput.side_effect = None
        backend.user32.SendInput.return_value = 0
        with self.assertRaisesRegex(RuntimeError, "Windows 未接受"):
            backend._unicode("a")


class DesktopCliTests(unittest.TestCase):
    def test_desktop_mode_configuration_and_cleanup(self):
        import cli as main
        import bootstrap

        with (
            patch("sys.argv", ["main.py", "--desktop", "--max-turns", "75"]),
            patch("builtins.input", return_value="桌面任务"),
            patch.dict("os.environ", {"DEEPSEEK_API_KEY": "test"}),
            patch.object(bootstrap, "load_dotenv"), patch.object(bootstrap, "OpenAI"),
            patch.object(bootstrap, "Agent") as agent,
            patch("adapters.windows_desktop.WindowsDesktop") as desktop,
            redirect_stdout(StringIO()),
        ):
            agent.return_value.run.return_value = "done"
            main.main()
        args, kwargs = agent.call_args
        names = {tool.name for tool in args[1].specs()}
        self.assertEqual(len(names), 10)
        self.assertIn("app_find", names)
        self.assertNotIn("exec_command", names)
        self.assertEqual(kwargs["max_turns"], 75)
        self.assertIs(args[2].desktop, desktop.return_value.__enter__.return_value)
        desktop.return_value.__exit__.assert_called_once()


if __name__ == "__main__":
    unittest.main()
