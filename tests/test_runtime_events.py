"""从事件还原调用结果，并验证日志故障不遮蔽原始任务错误。"""

import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import cli

from contracts import AgentCancelled, ModelMetadata, ModelReply, ModelResponseError, TraceWriteError
from core.agent import Agent
from core.tooling import ToolRegistry
from adapters.windows_desktop import WindowsDesktop
from tests.fakes import FakeBackend, MemoryEnvironment, MemoryTrace, SequenceModel, call
from tools.desktop import register_desktop_tools
from tracing import JsonlTrace


class RuntimeEventTests(unittest.TestCase):
    def test_cli_strict_mode_stops_before_file_write(self):
        trace = MemoryTrace(fail_on={"tool_execution_started"})
        model = SequenceModel(ModelReply(tool_calls=[call("write", "write_file", {"path": "note.txt", "content": "文本"})]))
        with (
            tempfile.TemporaryDirectory() as root,
            patch("sys.argv", ["main.py", "--workdir", root, "--trace-file", "unused.jsonl", "--trace-strict"]),
            patch("builtins.input", return_value="任务"),
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            patch.object(cli, "load_dotenv"),
            patch.object(cli, "_create_model", return_value=model),
            patch.object(cli, "JsonlTrace") as trace_file,
            redirect_stderr(StringIO()), redirect_stdout(StringIO()),
        ):
            trace_file.return_value.__enter__.return_value = trace
            with self.assertRaises(TraceWriteError):
                cli.main()
            self.assertFalse((Path(root) / "note.txt").exists())
        self.assertEqual(trace.events[0]["config"]["mode"], "local")

    def test_cli_rejects_strict_mode_without_trace_file(self):
        with patch("sys.argv", ["main.py", "--trace-strict"]), redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            cli.main()

    def test_model_truncation_logs_response_diagnostics(self):
        metadata = ModelMetadata("test", "model", "length", "request-1", "response-1", {"output_tokens": 4096})
        trace = MemoryTrace()
        agent = Agent(SequenceModel(ModelResponseError("MODEL_OUTPUT_TRUNCATED", "截断", metadata)),
                      ToolRegistry(), MemoryEnvironment(), verbose=False, trace=trace)
        with self.assertRaises(ModelResponseError):
            agent.run("任务")
        event = next(e for e in trace.events if e["event"] == "model_failed")
        self.assertEqual(event["error_info"]["code"], "MODEL_OUTPUT_TRUNCATED")
        self.assertEqual(event["metadata"]["finish_reason"], "length")
        self.assertEqual(event["metadata"]["usage"]["output_tokens"], 4096)

    def test_empty_or_whitespace_reply_is_logged_as_model_failure(self):
        for content in (None, "", "   "):
            with self.subTest(content=content):
                trace = MemoryTrace()
                agent = Agent(SequenceModel(ModelReply(content)), ToolRegistry(), MemoryEnvironment(), verbose=False, trace=trace)
                with self.assertRaises(RuntimeError):
                    agent.run("任务")
                self.assertTrue(any(e["event"] == "model_failed" for e in trace.events))

    def test_sink_recovery_on_next_run_and_cancellation_keep_independent_state(self):
        trace = MemoryTrace(fail_on={"run_started"})
        agent = Agent(SequenceModel(ModelReply("一"), ModelReply("二")), ToolRegistry(), MemoryEnvironment(),
                      verbose=False, trace=trace)
        with redirect_stderr(StringIO()):
            agent.run("一")
        previous = agent.last_state
        trace.fail_on.clear()
        agent.run("二")
        self.assertEqual(len(previous.trace_errors), 1)
        self.assertEqual(agent.last_state.trace_errors, [])
        self.assertEqual(trace.events[0]["sequence"], 1)

    def test_tool_cancellation_has_terminal_observation_and_event(self):
        registry, trace = ToolRegistry(), MemoryTrace()
        def cancel(_env):
            raise AgentCancelled("停止")
        registry.register("cancel", "cancel", {}, [], cancel)
        agent = Agent(SequenceModel(ModelReply(tool_calls=[call("stop", "cancel", {})])), registry,
                      MemoryEnvironment(), verbose=False, trace=trace)
        with self.assertRaises(AgentCancelled):
            agent.run("任务")
        observation = agent.last_state.observations[0]
        self.assertEqual(observation.status, "cancelled")
        self.assertTrue(observation.executed)
        self.assertEqual(observation.error_info.side_effects, "possible")
        self.assertEqual(trace.events[-2]["event"], "tool_cancelled")
        self.assertEqual(trace.events[-1]["stop_reason"], "user_cancelled")

    def test_jsonl_reconstructs_rejections_execution_failure_and_result_failure(self):
        effects = []
        registry = ToolRegistry()
        def fail(_environment):
            effects.append("attempted")
            raise OSError("操作失败")
        registry.register("fail", "失败", {}, [], fail)
        registry.register("bad_result", "结果无效", {}, [], lambda _env: object())
        registry.register("number", "数字", {"x": {"type": "integer"}}, ["x"], lambda _env, x: x)
        model = SequenceModel(ModelReply(tool_calls=[
            call("missing", "unknown", {}), call("args", "number", {"x": "wrong"}),
            call("execution", "fail", {}), call("result", "bad_result", {}),
        ]), ModelReply("已检查失败原因"))
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "events.jsonl"
            with JsonlTrace(path) as trace:
                agent = Agent(model, registry, MemoryEnvironment(), verbose=False, trace=trace,
                              run_config={"mode": "test"})
                agent.run("不应出现在日志中的完整用户任务")
            raw = path.read_text(encoding="utf-8")
            events = [json.loads(line) for line in raw.splitlines()]
        self.assertNotIn("不应出现在日志中的完整用户任务", raw)
        self.assertEqual(effects, ["attempted"])
        results = {e["call_id"]: e for e in events if e["event"] == "tool_finished"}
        self.assertEqual([(results[key]["status"], results[key]["executed"], results[key]["error_info"]["code"])
                          for key in ("missing", "args", "execution", "result")], [
            ("rejected", False, "UNKNOWN_TOOL"), ("rejected", False, "INVALID_ARGUMENTS"),
            ("failed", True, "EXECUTION_FAILED"), ("failed", True, "INVALID_RESULT"),
        ])
        self.assertEqual([e["call_id"] for e in events if e["event"] == "tool_execution_started"], ["execution", "result"])
        self.assertEqual([e["sequence"] for e in events], list(range(1, len(events) + 1)))
        self.assertEqual(len({e["run_id"] for e in events}), 1)
        self.assertTrue(all(e["schema_version"] == 2 for e in events))
        self.assertEqual(events[-1]["stop_reason"], "model_answer")
        # 模型反馈与日志保持相同错误分类；仍有旧字符串供调用方阅读。
        feedback = json.loads(model.requests[1][0][-1].content)
        self.assertEqual(feedback["error_info"]["phase"], "result")
        self.assertIsInstance(feedback["error"], str)

    def test_partial_desktop_action_is_visible_without_image_data(self):
        backend, trace, registry = FakeBackend(), MemoryTrace(), ToolRegistry()
        with WindowsDesktop(backend=backend, settle_seconds=0) as desktop:
            register_desktop_tools(registry, desktop)
            frame = desktop.screenshot().data["frame_id"]
            send = backend.send
            def send_and_break_capture(action, arguments):
                send(action, arguments)
                backend.fail_capture = True
            model = SequenceModel(ModelReply(tool_calls=[call("click", "desktop_click", {
                "frame_id": frame, "x": 10, "y": 10,
            })]), ModelReply("动作已发送，截图失败，需要重新观察"))
            with patch.object(backend, "send", side_effect=send_and_break_capture):
                Agent(model, registry, MemoryEnvironment(), verbose=False, trace=trace).run("点击")
            self.assertIsNone(desktop.frame)
        result = next(e for e in trace.events if e["event"] == "tool_finished")
        self.assertEqual(len(backend.sent), 1)
        self.assertEqual((result["status"], result["executed"]), ("failed", True))
        self.assertEqual(result["error_info"]["code"], "POST_ACTION_CAPTURE_FAILED")
        self.assertEqual(result["error_info"]["side_effects"], "possible")

    def test_model_failure_has_duration_phase_and_original_exception(self):
        trace = MemoryTrace()
        agent = Agent(SequenceModel(ConnectionError("服务不可达")), ToolRegistry(), MemoryEnvironment(),
                      verbose=False, trace=trace)
        with self.assertRaises(ConnectionError):
            agent.run("任务")
        event = next(e for e in trace.events if e["event"] == "model_failed")
        self.assertEqual(event["error_info"]["phase"], "model")
        self.assertEqual(event["error_info"]["exception_type"], "ConnectionError")
        self.assertGreaterEqual(event["duration_ms"], 0)
        self.assertFalse(any(e["event"] == "model_finished" for e in trace.events))
        self.assertEqual(agent.last_state.stop_reason, "run_failure")

    def test_provider_error_fields_do_not_break_error_reporting(self):
        error_type = type("ProviderError", (ConnectionError,), {})
        error = error_type("请求失败")
        error.code, error.metadata = None, {"provider_detail": "untrusted"}
        error.request_id, error.status_code = "failed-request", 429
        trace = MemoryTrace()
        agent = Agent(SequenceModel(error), ToolRegistry(), MemoryEnvironment(), verbose=False, trace=trace)
        with self.assertRaises(error_type) as caught:
            agent.run("任务")
        self.assertIs(caught.exception, error)
        event = next(e for e in trace.events if e["event"] == "model_failed")
        self.assertEqual(event["error_info"]["code"], "MODEL_FAILED")
        self.assertEqual(event["request_id"], "failed-request")
        self.assertEqual(event["status_code"], 429)
        self.assertIsNone(event["metadata"])

    def test_model_metadata_is_logged_and_token_counts_are_not_redacted(self):
        metadata = ModelMetadata("test", "model", "stop", "request-1", "response-1", {"input_tokens": 10, "output_tokens": 2})
        trace = MemoryTrace()
        Agent(SequenceModel(ModelReply("完成", metadata=metadata)), ToolRegistry(), MemoryEnvironment(),
              verbose=False, trace=trace).run("任务")
        event = next(e for e in trace.events if e["event"] == "model_finished")
        self.assertEqual(event["metadata"]["usage"], {"input_tokens": 10, "output_tokens": 2})
        self.assertEqual(event["metadata"]["request_id"], "request-1")

    def test_default_logging_failure_reports_once_and_task_continues(self):
        trace = MemoryTrace(fail_on={"tool_started"})
        env = MemoryEnvironment()
        registry = ToolRegistry()
        registry.register("write", "写入", {}, [], lambda _env: env.files.update(done=True))
        model = SequenceModel(ModelReply(tool_calls=[call("write", "write", {})]), ModelReply("完成"))
        agent = Agent(model, registry, env, verbose=False, trace=trace)
        stderr = StringIO()
        with redirect_stderr(stderr):
            self.assertEqual(agent.run("任务"), "完成")
        self.assertEqual(env.files, {"done": True})
        self.assertEqual(agent.last_state.status, "completed")
        self.assertEqual(len(agent.last_state.trace_errors), 1)
        self.assertEqual(stderr.getvalue().count("[日志故障]"), 1)

    def test_strict_logging_failure_before_execution_prevents_side_effects(self):
        trace = MemoryTrace(fail_on={"tool_execution_started"})
        env = MemoryEnvironment()
        registry = ToolRegistry()
        registry.register("write", "写入", {}, [], lambda _env: env.files.update(done=True))
        model = SequenceModel(ModelReply(tool_calls=[call("write", "write", {})]))
        agent = Agent(model, registry, env, verbose=False, trace=trace, trace_strict=True)
        with redirect_stderr(StringIO()), self.assertRaises(TraceWriteError):
            agent.run("任务")
        self.assertEqual(env.files, {})
        self.assertEqual(agent.last_state.status, "failed")
        self.assertEqual(agent.last_state.stop_reason, "trace_failure")
        self.assertEqual(agent.last_state.error_info.phase, "logging")
        self.assertEqual(len(model.requests), 1)

    def test_strict_failure_after_execution_keeps_observation_and_stops(self):
        trace = MemoryTrace(fail_on={"tool_finished"})
        registry = ToolRegistry()
        registry.register("effect", "effect", {}, [], lambda _env: "已执行")
        model = SequenceModel(ModelReply(tool_calls=[call("effect", "effect", {})]), ModelReply("不应再请求"))
        agent = Agent(model, registry, MemoryEnvironment(), verbose=False, trace=trace, trace_strict=True)
        with redirect_stderr(StringIO()), self.assertRaises(TraceWriteError):
            agent.run("任务")
        self.assertEqual(agent.last_state.observations[0].result, "已执行")
        self.assertTrue(agent.last_state.observations[0].executed)
        self.assertEqual(len(model.requests), 1)

    def test_final_logging_failure_never_masks_primary_model_error_or_cancellation(self):
        for error in (ConnectionError("原始网络错误"), AgentCancelled("停止"), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                trace = MemoryTrace(fail_on={"model_failed", "model_cancelled", "run_finished"})
                agent = Agent(SequenceModel(error), ToolRegistry(), MemoryEnvironment(), verbose=False,
                              trace=trace, trace_strict=True)
                with redirect_stderr(StringIO()), self.assertRaises(type(error)) as caught:
                    agent.run("任务")
                self.assertIs(caught.exception, error)
                self.assertEqual(len(agent.last_state.trace_errors), 1)

    def test_strict_failure_on_successful_final_event_marks_run_failed(self):
        agent = Agent(SequenceModel(ModelReply("回答")), ToolRegistry(), MemoryEnvironment(), verbose=False,
                      trace=MemoryTrace(fail_on={"run_finished"}), trace_strict=True)
        with redirect_stderr(StringIO()), self.assertRaises(TraceWriteError):
            agent.run("任务")
        self.assertEqual(agent.last_state.status, "failed")
        self.assertEqual(agent.last_state.error_info.code, "TRACE_WRITE_FAILED")

    def test_context_failure_and_turn_limit_have_specific_stop_information(self):
        env = MemoryEnvironment()
        with patch.object(env, "get_info", side_effect=OSError("环境不可用")):
            agent = Agent(SequenceModel(), ToolRegistry(), env, verbose=False, trace=MemoryTrace())
            with self.assertRaises(OSError):
                agent.run("任务")
        self.assertEqual(agent.last_state.error_info.phase, "context")
        registry = ToolRegistry()
        registry.register("noop", "noop", {}, [], lambda _env: None)
        agent = Agent(SequenceModel(ModelReply(tool_calls=[call("again", "noop", {})])), registry, env,
                      max_turns=1, verbose=False, trace=MemoryTrace())
        with self.assertRaises(RuntimeError):
            agent.run("任务")
        self.assertEqual(agent.last_state.stop_reason, "turn_limit")
        self.assertEqual(agent.last_state.error_info.code, "TURN_LIMIT_REACHED")

    def test_short_sensitive_text_does_not_corrupt_status_and_error_codes(self):
        registry, trace = ToolRegistry(), MemoryTrace()
        registry.register("input", "input", {"text": {"type": "string"}}, ["text"],
                          lambda _env, text: (_ for _ in ()).throw(ValueError(text)), sensitive_parameters=("text",))
        model = SequenceModel(ModelReply(tool_calls=[call("in", "input", {"text": "a"})]), ModelReply("done"))
        with redirect_stdout(StringIO()):
            Agent(model, registry, MemoryEnvironment(), trace=trace).run("input")
        finished = next(e for e in trace.events if e["event"] == "tool_finished")
        self.assertEqual(finished["status"], "failed")
        self.assertEqual(finished["error_info"]["code"], "EXECUTION_FAILED")
        self.assertEqual(finished["error_info"]["message"], "[REDACTED]")
        self.assertEqual(trace.events[-1]["stop_reason"], "model_answer")


if __name__ == "__main__":
    unittest.main()
