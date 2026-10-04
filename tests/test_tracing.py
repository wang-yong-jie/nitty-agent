"""日志必须可追溯，且不改变真实工具输入或把图片/凭证写到日志。"""

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import cli as main
from core.agent import Agent
from contracts import AgentCancelled, ImageContent, ModelReply, ToolCall, ToolResult
from tests.fakes import MemoryEnvironment, SequenceModel, call
from core.tooling import ToolRegistry
from tools.local import create_default_registry
from tracing import JsonlTrace, LogRedactor


class MemoryTrace:
    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event)


class TracingTests(unittest.TestCase):
    def test_command_arguments_duration_and_timeout_are_logged_without_changing_feedback(self):
        env, trace = MemoryEnvironment(), MemoryTrace()
        command = "Write-Output '中文'; tool --password 'inline-secret'"
        result = {"exit_code": 0, "timed_out": True, "truncated": False, "stdout": "inline-secret env-secret"}
        model = SequenceModel(ModelReply(tool_calls=[call("cmd", "exec_command", {"cmd": command})]), ModelReply("未完成"))
        with patch.dict("os.environ", {"TEST_API_KEY": "env-secret"}), patch.object(env, "exec_command", return_value=result) as run:
            agent = Agent(model, create_default_registry(), env, trace=trace)
            output = StringIO()
            with redirect_stdout(output):
                agent.run("查询应用")
        run.assert_called_once_with(cmd=command)
        self.assertIn("inline-secret", model.requests[1][0][-1].content)
        logged = output.getvalue() + json.dumps(trace.events)
        self.assertNotIn("inline-secret", logged)
        self.assertNotIn("env-secret", logged)
        self.assertIn("Write-Output", logged)
        started = next(e for e in trace.events if e["event"] == "tool_started")
        finished = next(e for e in trace.events if e["event"] == "tool_finished")
        self.assertEqual(started["call_id"], finished["call_id"])
        self.assertTrue(finished["result"]["timed_out"])
        self.assertGreaterEqual(finished["duration_ms"], 0)
        self.assertEqual(trace.events[-1]["status"], "completed")

    def test_registered_sensitive_fields_and_images_are_omitted(self):
        trace, registry, received = MemoryTrace(), ToolRegistry(), []
        def input_text(_env, text):
            received.append(text)
            return ToolResult({"input_sent": True, "echo": text}, [ImageContent("private-base64")])
        registry.register("custom_input", "input", {}, [], input_text, sensitive_parameters=("text",))
        model = SequenceModel(ModelReply(tool_calls=[call("input", "custom_input", {"text": "private-message"})]), ModelReply("done"))
        agent = Agent(model, registry, MemoryEnvironment(), verbose=False, trace=trace)
        agent.run("input")
        logged = json.dumps(trace.events)
        self.assertNotIn("private-message", logged)
        self.assertNotIn("private-base64", logged)
        self.assertEqual(received, ["private-message"])
        finished = next(e for e in trace.events if e["event"] == "tool_finished")
        self.assertEqual(finished["images"], [{"mime_type": "image/png", "encoded_length": 14}])
        self.assertEqual(agent.last_state.observations[0].images[0].data, "private-base64")

    def test_redactor_covers_nested_fields_assignments_and_authorization(self):
        redactor = LogRedactor()
        payload = {"nested": [{"api_key": "field-secret", "password": "another-secret"}],
                   "cmd": '$env:API_KEY="ps-secret"; tool --token=cli-secret',
                   "stdout": "Authorization: Bearer bearer-secret", "normal": "Get-StartApps"}
        cleaned = redactor.clean(payload)
        logged = json.dumps(cleaned)
        for value in ("field-secret", "another-secret", "ps-secret", "cli-secret", "bearer-secret"):
            self.assertNotIn(value, logged)
        self.assertEqual(cleaned["normal"], "Get-StartApps")
        self.assertEqual(payload["nested"][0]["api_key"], "field-secret")

    def test_malformed_sensitive_call_is_logged_without_raw_content(self):
        trace, registry = MemoryTrace(), ToolRegistry()
        registry.register("custom_input", "input", {}, [], lambda *_: None, sensitive_parameters=("text",))
        model = SequenceModel(ModelReply(tool_calls=[ToolCall("bad", "custom_input", '{"text":"private-message"')]), ModelReply("bad input"))
        Agent(model, registry, MemoryEnvironment(), verbose=False, trace=trace).run("input")
        self.assertNotIn("private-message", json.dumps(trace.events))
        self.assertTrue(next(e for e in trace.events if e["event"] == "tool_started")["arguments"]["invalid_json"])

    def test_rejected_batch_and_cancelled_calls_have_distinct_events(self):
        trace, registry = MemoryTrace(), ToolRegistry()
        registry.register("exclusive", "exclusive", {}, [], lambda _: self.fail("must not execute"), requires_single_call=True)
        model = SequenceModel(ModelReply(tool_calls=[call("a", "exclusive", {}), call("b", "exclusive", {})]), ModelReply("retry"))
        Agent(model, registry, MemoryEnvironment(), verbose=False, trace=trace).run("batch")
        self.assertTrue(all(not e["executed"] for e in trace.events if e["event"] == "tool_finished"))
        def cancel(_env):
            raise AgentCancelled("stop")
        registry.register("cancel", "cancel", {}, [], cancel)
        model = SequenceModel(ModelReply(tool_calls=[call("stop", "cancel", {})]))
        with self.assertRaises(AgentCancelled):
            Agent(model, registry, MemoryEnvironment(), verbose=False, trace=trace).run("cancel")
        self.assertEqual(trace.events[-1]["status"], "cancelled")
        self.assertEqual(trace.events[-2]["event"], "tool_cancelled")

    def test_jsonl_flushes_final_failure_and_does_not_overwrite_existing_file(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "logs" / "trace.jsonl"
            with JsonlTrace(path) as trace:
                agent = Agent(SequenceModel(ConnectionError("连接失败")), ToolRegistry(), MemoryEnvironment(), verbose=False, trace=trace)
                with self.assertRaises(ConnectionError):
                    agent.run("test")
                events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
                self.assertEqual(events[-1]["status"], "failed")
                self.assertEqual(events[-1]["error"], "连接失败")
            original = path.read_bytes()
            with self.assertRaises(FileExistsError), JsonlTrace(path):
                pass
            self.assertEqual(path.read_bytes(), original)

    def test_cli_trace_option_records_real_tool_execution(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "cli.jsonl"
            model = SequenceModel(ModelReply(tool_calls=[call("write", "write_file", {
                "path": "note.txt", "content": "private-cli-content",
            })]), ModelReply("done"))
            with (
                patch("sys.argv", ["main.py", "--workdir", root, "--trace-file", str(path)]),
                patch.dict("os.environ", {"DEEPSEEK_API_KEY": "test-key"}),
                patch("builtins.input", return_value="写文件"),
                patch.object(main, "load_dotenv"),
                patch.object(main, "_create_model", return_value=model),
                redirect_stdout(StringIO()),
            ):
                main.main()
            events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(events[-1]["status"], "completed")
            self.assertEqual(next(e for e in events if e["event"] == "tool_started")["arguments"]["content"], "[REDACTED]")
            self.assertEqual((Path(root) / "note.txt").read_text(encoding="utf-8"), "private-cli-content")


if __name__ == "__main__":
    unittest.main()
