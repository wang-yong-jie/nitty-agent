"""不联网测试：验证插件替换、协议转换、状态反馈和本机执行行为。"""

import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from io import BytesIO, StringIO
from pathlib import Path
from unittest.mock import Mock, patch

import adapters.local as environment_module
import cli as main
from tests.fakes import MemoryEnvironment, SequenceModel, call, sdk_response
from core.agent import Agent
from core.context import ContextBuilder
from contracts import Message, ModelReply, ModelResponseError, ToolCall
from adapters.local import LocalEnvironment
from adapters.models import ClaudeModel, OpenAICompatibleModel
from core.state import AgentState
from core.tooling import ToolRegistry
from tools.local import create_default_registry


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.environment = MemoryEnvironment()
        self.registry = ToolRegistry()
        self.registry.register(
            "double", "数字乘以二", {"number": {"type": "number"}}, ["number"],
            lambda environment, number: number * 2,
        )

    def agent(self, model, **kwargs):
        return Agent(model, self.registry, self.environment, verbose=False, **kwargs)

    def test_custom_model_tools_and_multiple_feedback(self):
        """单次多个工具调用与后续调用都保留正确 ID，状态包含最终消息。"""
        model = SequenceModel(
            ModelReply(tool_calls=[call("a", "double", {"number": 3}), call("b", "double", {"number": 4})]),
            ModelReply(tool_calls=[call("c", "double", {"number": 14})]),
            ModelReply("结果为 28。"),
        )
        agent = self.agent(model)
        self.assertEqual(agent.run("测试任务"), "结果为 28。")
        self.assertEqual([o.result for o in agent.last_state.observations], [6, 8, 28])
        self.assertEqual([m.tool_call_id for m in model.requests[1][0][-2:]], ["a", "b"])
        self.assertEqual(agent.last_state.status, "completed")
        self.assertEqual(agent.last_state.turn, 3)
        self.assertEqual(agent.last_state.messages[-1].content, "结果为 28。")
        # 自定义工具表不应携带未注册工具的使用说明。
        self.assertNotIn("exec_command", model.requests[0][0][0].content)

    def test_errors_then_corrected_call(self):
        """未知工具、非法 JSON、非对象参数和错误参数都反馈，而不是中断循环。"""
        model = SequenceModel(ModelReply(tool_calls=[
            call("unknown", "not_registered", {}),
            ToolCall("json", "double", "{bad json"),
            call("array", "double", []),
            call("args", "double", {"wrong": 1}),
        ]), ModelReply(tool_calls=[call("fixed", "double", {"number": 3})]), ModelReply("结果为 6。"))
        agent = self.agent(model)
        self.assertEqual(agent.run("修正错误"), "结果为 6。")
        observations = agent.last_state.observations
        self.assertTrue(all(o.error for o in observations[:4]))
        self.assertEqual(observations[-1].result, 6)
        self.assertTrue(all(m.is_error for m in model.requests[1][0][-4:]))

    def test_new_task_has_separate_history(self):
        """同一 Agent 连续运行任务时创建新状态，不共享 messages 列表。"""
        model = SequenceModel(ModelReply("回答一"), ModelReply("回答二"))
        agent = Agent(model, ToolRegistry(), self.environment, verbose=False)
        agent.run("任务一")
        previous = agent.last_state
        agent.run("任务二")
        self.assertIsNot(previous, agent.last_state)
        self.assertEqual(previous.answer, "回答一")
        self.assertEqual([m.content for m in agent.last_state.messages], ["任务二", "回答二"])
        self.assertEqual(model.requests[1][1], [])

    def test_limit_and_model_failure_keep_state(self):
        """轮次耗尽和模型通信失败有不同结束状态，均不会伪装成成功。"""
        repeated = ModelReply(tool_calls=[call("again", "double", {"number": 1})])
        agent = self.agent(SequenceModel(repeated, repeated), max_turns=2)
        with self.assertRaisesRegex(RuntimeError, "2 轮上限"):
            agent.run("循环")
        self.assertEqual(agent.last_state.status, "limit_reached")
        self.assertIsNone(agent.last_state.answer)
        agent = self.agent(SequenceModel(ConnectionError("模型通信失败")))
        with self.assertRaises(ConnectionError):
            agent.run("连接测试")
        self.assertEqual(agent.last_state.status, "failed")
        self.assertEqual(agent.last_state.observations, [])

    def test_empty_model_reply_is_failure(self):
        agent = self.agent(SequenceModel(ModelReply()))
        with self.assertRaisesRegex(RuntimeError, "有效回答"):
            agent.run("测试")
        self.assertEqual(agent.last_state.status, "failed")

    def test_default_tools_work_with_memory_environment(self):
        """同一套默认工具可在替换后的环境中读写、执行，且上下文随环境刷新。"""
        model = SequenceModel(
            ModelReply(tool_calls=[call("write", "write_file", {"path": "note.txt", "content": "你好"})]),
            ModelReply(tool_calls=[
                call("read", "read_file", {"path": "note.txt"}),
                call("exec", "exec_command", {"cmd": "test-command"}),
            ]),
            ModelReply("已完成"),
        )
        agent = Agent(model, create_default_registry(), self.environment, verbose=False)
        with patch.object(environment_module.subprocess, "Popen", side_effect=AssertionError("不应接触本机")):
            self.assertEqual(agent.run("测试替换环境"), "已完成")
        self.assertEqual(self.environment.files, {"note.txt": "你好"})
        self.assertEqual(self.environment.commands, ["test-command"])
        self.assertIn('"working_directory": null', model.requests[0][0][0].content)
        self.assertIn("memory:/work", model.requests[1][0][0].content)
        self.assertEqual(agent.last_state.observations[1].result["content"], "你好")

    def test_custom_context_and_unserializable_tool_result(self):
        """可替换上下文；错误工具返回值应反馈给模型。"""
        self.registry.register("bad", "返回不可序列化对象", {}, [], lambda environment: object())
        model = SequenceModel(ModelReply(tool_calls=[call("bad", "bad", {})]), ModelReply("返回值有误"))
        agent = self.agent(model, context=ContextBuilder("自定义指令"))
        self.assertEqual(agent.run("测试"), "返回值有误")
        self.assertTrue(model.requests[0][0][0].content.startswith("自定义指令"))
        self.assertIn("TypeError", agent.last_state.observations[0].error)


class ContextTests(unittest.TestCase):
    def test_registered_rules_are_ordered_deduplicated_and_scoped(self):
        registry = ToolRegistry()
        for name, instructions in (
            ("lookup", "查询前先确认数据来源。"),
            ("lookup_more", "查询前先确认数据来源。"),
            ("export", "导出时保留来源信息。"),
        ):
            registry.register(name, "测试工具", {}, [], lambda environment: None,
                              instructions=instructions)
        model = SequenceModel(ModelReply("已完成"))
        Agent(model, registry, MemoryEnvironment(), verbose=False).run("测试规则")
        system = model.requests[0][0][0].content
        self.assertEqual(system.count("查询前先确认数据来源。"), 1)
        self.assertEqual(system.count("导出时保留来源信息。"), 1)
        self.assertLess(system.index("查询前"), system.index("导出时"))
        scoped = ContextBuilder().build_messages(AgentState("task"), {}, registry.specs()[:2])
        self.assertNotIn("导出时保留来源信息。", scoped[0].content)

    def test_tool_names_do_not_select_instructions(self):
        registry = ToolRegistry()
        for name in ("exec_command", "read_file", "write_file", "desktop_screenshot"):
            registry.register(name, "没有附加规则的自定义实现", {}, [], lambda environment: None)
        context, state = ContextBuilder(), AgentState("task")
        self.assertEqual(
            context.build_messages(state, {}, registry.specs())[0].content,
            context.build_messages(state, {}, [])[0].content,
        )

    def test_builtin_rules_survive_renaming(self):
        specs = create_default_registry().specs()
        renamed = [replace(spec, name=f"renamed_{index}") for index, spec in enumerate(specs)]
        context, state = ContextBuilder(), AgentState("task")
        original = context.build_messages(state, {}, specs)[0].content
        self.assertEqual(original, context.build_messages(state, {}, renamed)[0].content)
        for spec in specs:
            self.assertTrue(spec.instructions)
            self.assertIn(spec.instructions, original)


class ModelAdapterTests(unittest.TestCase):
    def test_openai_response_metadata_and_truncation_diagnostics(self):
        response = sdk_response(content="完成")
        response = response.model_copy(update={"usage": None})
        # SDK 为真实响应附加请求 ID；在离线响应中模拟这个公开诊断属性。
        response._request_id = "request-123"
        from openai.types.completion_usage import CompletionUsage
        response.usage = CompletionUsage(prompt_tokens=12, completion_tokens=3, total_tokens=15)
        client = Mock()
        client.chat.completions.create.return_value = response
        adapter = OpenAICompatibleModel(client, "requested-model", provider="deepseek")
        reply = adapter.generate([Message("user", "任务")], [])
        self.assertEqual(reply.metadata.provider, "deepseek")
        self.assertEqual(reply.metadata.request_id, "request-123")
        self.assertEqual(reply.metadata.model, "test-model")
        self.assertEqual(reply.metadata.usage["total_tokens"], 15)
        response.choices[0].finish_reason = "length"
        with self.assertRaises(ModelResponseError) as caught:
            adapter.generate([Message("user", "任务")], [])
        self.assertEqual(caught.exception.code, "MODEL_OUTPUT_TRUNCATED")
        self.assertEqual(caught.exception.metadata.finish_reason, "length")

    def test_claude_response_metadata(self):
        body = {"id": "response-1", "model": "actual-model", "stop_reason": "end_turn",
                "usage": {"input_tokens": 8, "output_tokens": 2}, "content": [{"type": "text", "text": "完成"}]}
        response = BytesIO(json.dumps(body).encode())
        response.headers = {"request-id": "request-1"}
        with patch("adapters.models.urlopen", return_value=response):
            reply = ClaudeModel("key", "requested-model").generate([Message("user", "任务")], [])
        self.assertEqual(reply.metadata.request_id, "request-1")
        self.assertEqual(reply.metadata.response_id, "response-1")
        self.assertEqual(reply.metadata.model, "actual-model")
        self.assertEqual(reply.metadata.usage, {"input_tokens": 8, "output_tokens": 2})

    def test_openai_messages_calls_and_extensions(self):
        """统一消息和工具描述转成 SDK 格式，响应调用转成公共类型。"""
        client = Mock()
        client.chat.completions.create.return_value = sdk_response(
            calls=[call("new", "read_file", {"path": "note.txt"})],
        )
        adapter = OpenAICompatibleModel(client, "deepseek-flash", extra_body={"thinking": {"type": "disabled"}})
        specs = create_default_registry().specs()
        reply = adapter.generate([
            Message("system", "规则"), Message("user", "任务"),
            Message("assistant", tool_calls=[call("old", "write_file", {"path": "note.txt", "content": "你好"})]),
            Message("tool", '{"result": "已写入"}', tool_call_id="old"),
        ], specs)
        request = client.chat.completions.create.call_args.kwargs
        self.assertEqual(request["messages"][-1]["tool_call_id"], "old")
        self.assertEqual(request["tools"][0]["function"]["name"], "exec_command")
        self.assertEqual(set(request["tools"][0]["function"]), {"name", "description", "parameters"})
        self.assertEqual(reply.tool_calls[0].id, "new")
        self.assertEqual(json.loads(reply.tool_calls[0].arguments), {"path": "note.txt"})
        self.assertIn("extra_body", request)
        client.chat.completions.create.return_value = sdk_response(content="你好")
        OpenAICompatibleModel(client, "other-model").generate([Message("user", "你好")], [])
        request = client.chat.completions.create.call_args.kwargs
        self.assertNotIn("tools", request)
        self.assertNotIn("extra_body", request)

    def test_truncated_openai_response_raises(self):
        client = Mock()
        client.chat.completions.create.return_value = sdk_response(content="未完成", finish_reason="length")
        with self.assertRaisesRegex(RuntimeError, "截断"):
            OpenAICompatibleModel(client, "test-model").generate([Message("user", "任务")], [])

    def test_claude_tool_cycle_and_error_feedback(self):
        """Claude 原生 tool_use/tool_result 的多调用与错误标记不影响 Runtime。"""
        bodies = []
        responses = iter([
            {"stop_reason": "tool_use", "content": [
                {"type": "text", "text": "执行工具"},
                {"type": "tool_use", "id": "good", "name": "read_file", "input": {"path": "note"}},
                {"type": "tool_use", "id": "bad", "name": "read_file", "input": {"path": "missing"}},
            ]},
            {"stop_reason": "end_turn", "content": [{"type": "text", "text": "已检查"}]},
        ])

        def transport(request, timeout):
            bodies.append(json.loads(request.data))
            self.assertEqual(request.full_url, "https://api.anthropic.com/v1/messages")
            self.assertEqual(request.get_header("X-api-key"), "test-key")
            return BytesIO(json.dumps(next(responses)).encode("utf-8"))

        env = MemoryEnvironment()
        env.files["note"] = "内容"
        agent = Agent(ClaudeModel("test-key", "test-claude"), create_default_registry(), env, verbose=False)
        with patch("adapters.models.urlopen", side_effect=transport):
            self.assertEqual(agent.run("读取两个文件"), "已检查")
        self.assertTrue(bodies[0]["system"])
        self.assertIn("input_schema", bodies[0]["tools"][0])
        self.assertEqual(set(bodies[0]["tools"][0]), {"name", "description", "input_schema"})
        results = bodies[1]["messages"][-1]
        self.assertEqual(results["role"], "user")
        self.assertEqual([b["tool_use_id"] for b in results["content"]], ["good", "bad"])
        self.assertEqual([b["is_error"] for b in results["content"]], [False, True])
        self.assertEqual(bodies[1]["messages"][-2]["content"][0]["type"], "text")

    def test_claude_truncation_and_transport_failure(self):
        adapter = ClaudeModel("test-key", "test-model")
        with patch("adapters.models.urlopen", return_value=BytesIO(b'{"stop_reason": "max_tokens", "content": []}')):
            with self.assertRaisesRegex(RuntimeError, "截断"):
                adapter.generate([Message("user", "测试")], [])
        with patch("adapters.models.urlopen", side_effect=ConnectionError("网络失败")):
            with self.assertRaises(ConnectionError):
                adapter.generate([Message("user", "测试")], [])


class CliTests(unittest.TestCase):
    def run_main(self, arguments):
        """只测试入口装配，不运行网络客户端和模型循环。"""
        with (
            patch.object(sys, "argv", ["main.py", *arguments]),
            patch("builtins.input", return_value="测试任务"),
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test", "OPENAI_API_KEY": "test", "ANTHROPIC_API_KEY": "test"}),
            patch.object(main, "load_dotenv"),
            patch.object(main, "OpenAI"),
            patch.object(main, "Agent") as api,
            redirect_stdout(StringIO()),
        ):
            api.return_value.run.return_value = "已完成"
            main.main()
            api.return_value.run.assert_called_once_with("测试任务")
            return api.call_args.args

    def test_cli_working_directory_and_default_model(self):
        with tempfile.TemporaryDirectory() as root:
            work = Path(root) / "中文 work"
            model, registry, env = self.run_main(["--workdir", str(work)])
            self.assertEqual(env.working_directory, work)
            self.assertTrue(work.is_dir())
            self.assertEqual(model.model, "deepseek-flash")
            self.assertEqual(len(registry.specs()), 3)

    def test_cli_without_directory_is_lazy(self):
        with patch.object(environment_module.tempfile, "mkdtemp") as create:
            _, _, env = self.run_main([])
            self.assertIsNone(env.working_directory)
            create.assert_not_called()

    def test_other_providers_do_not_receive_deepseek_extensions(self):
        model, _, _ = self.run_main(["--provider", "openai", "--model", "test-model"])
        self.assertIsNone(model.extra_body)
        model, _, _ = self.run_main(["--provider", "claude", "--model", "test-claude"])
        self.assertIsInstance(model, ClaudeModel)


class WorkingDirectoryTests(unittest.TestCase):
    def setUp(self):
        # 所有文件测试在临时目录内完成，不修改项目文件。
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        self.env = LocalEnvironment(self.root)

    def tearDown(self):
        self.directory.cleanup()

    def run_python(self, code, **kwargs):
        """先写脚本再通过通用命令执行，避免不同 Shell 的内联引号规则。"""
        script = self.root / "check.py"
        self.env.write_file(str(script), code)
        return self.env.exec_command(f'python "{script}"', **kwargs)

    def test_files_and_absolute_paths(self):
        """验证中文文件、覆盖写入，以及工作目录外的绝对路径。"""
        self.env.write_file("data/notes.txt", "中文内容")
        self.assertEqual(self.env.read_file("data/notes.txt")["content"], "中文内容")
        self.env.write_file("data/notes.txt", "修改后的内容")
        self.assertEqual(self.env.read_file("data/notes.txt")["content"], "修改后的内容")
        # 另一个目录仍位于测试临时目录内，但不在当前工作目录里。
        work = self.root / "work"
        self.env = LocalEnvironment(work)
        external_file = self.root / "other" / "中文.txt"
        self.env.write_file(str(external_file), "目录外的内容")
        self.assertEqual(self.env.read_file(str(external_file))["content"], "目录外的内容")
        self.assertEqual(self.env.read_file("../other/中文.txt")["content"], "目录外的内容")

    def test_directory_creation_outside_working_directory(self):
        """同一个执行工具通过不同命令创建目录和列目录，支持工作目录外路径。"""
        work = self.root / "work"
        self.env = LocalEnvironment(work)
        target = self.root / "desktop" / "中文 目录"
        if os.name == "nt":
            create = f"New-Item -ItemType Directory -Force -Path '{target}'"
            listing = f"Get-ChildItem -LiteralPath '{target.parent}' -Name"
        else:
            create = f"mkdir -p '{target}'"
            listing = f"ls '{target.parent}'"
        created = self.env.exec_command(create)
        self.assertEqual(created["exit_code"], 0)
        self.assertEqual(created["stderr"], "")
        self.assertEqual(self.env.exec_command(create)["exit_code"], 0)
        result = self.env.exec_command(listing)
        self.assertEqual(result["exit_code"], 0)
        self.assertIn(target.name, result["stdout"])
        self.assertEqual(result["stderr"], "")
        self.assertTrue(target.is_dir())
        self.assertEqual(self.env.working_directory, work)

    def test_temporary_directory_is_lazy_and_shared(self):
        """未指定时，绝对文件路径不创建工作目录；相对文件和命令共用临时目录。"""
        self.env = LocalEnvironment()
        scratch = self.root / "scratch"
        scratch.mkdir()
        with patch.object(environment_module.tempfile, "mkdtemp", return_value=str(scratch)) as create:
            self.assertIsNone(self.env.get_info()["working_directory"])
            # 此操作模拟直接访问桌面之类的其他目录，不需要默认工作目录。
            self.env.write_file(str(self.root / "absolute.txt"), "外部文件")
            self.assertIsNone(self.env.working_directory)
            create.assert_not_called()

            with redirect_stdout(StringIO()):
                self.env.write_file("relative.txt", "临时目录中的文件")
                result = self.run_python("from pathlib import Path; print(Path.cwd())")
            self.assertEqual(self.env.read_file("relative.txt")["content"], "临时目录中的文件")
            self.assertEqual(Path(result["stdout"].strip()), scratch)
            self.assertEqual(result["exit_code"], 0)
            self.assertEqual(Path(self.env.get_info()["working_directory"]), scratch)
            create.assert_called_once()
        # 任务产物保留，不在工具返回时自动清理。
        self.assertTrue((scratch / "relative.txt").is_file())

    def test_specified_directory_is_created_and_used(self):
        """指定的目录可以尚不存在，文件写入和 Python 都以它为默认位置。"""
        work = self.root / "中文 工作目录"
        self.env = LocalEnvironment(work)
        self.assertEqual(self.env.working_directory, work)
        self.env.write_file("note.txt", "你好")
        result = self.run_python("from pathlib import Path; print(Path('note.txt').read_text(encoding='utf-8'))")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["stdout"].strip(), "你好")

    def test_environment_instances_do_not_share_working_directories(self):
        """两个环境独立创建临时目录，相同相对文件名不会互相覆盖。"""
        first, second = LocalEnvironment(), LocalEnvironment()
        first_dir, second_dir = self.root / "first", self.root / "second"
        first_dir.mkdir()
        second_dir.mkdir()
        with (
            patch.object(environment_module.tempfile, "mkdtemp", side_effect=[str(first_dir), str(second_dir)]),
            redirect_stdout(StringIO()),
        ):
            first.write_file("note.txt", "环境一")
            self.assertIsNone(second.working_directory)
            second.write_file("note.txt", "环境二")
        self.assertEqual(first.read_file("note.txt")["content"], "环境一")
        self.assertEqual(second.read_file("note.txt")["content"], "环境二")
        self.assertNotEqual(first.working_directory, second.working_directory)

    def test_environment_variables_in_paths(self):
        """路径中的环境变量可展开，方便访问用户目录。"""
        with patch.dict(os.environ, {"NITTY_TEST_ROOT": str(self.root)}):
            variable = "%NITTY_TEST_ROOT%" if os.name == "nt" else "$NITTY_TEST_ROOT"
            self.env.write_file(variable + "/expanded/note.txt", "变量路径")
        self.assertTrue((self.root / "expanded/note.txt").is_file())

    def test_python_uses_current_environment_and_working_directory(self):
        """真实执行子进程，确认解释器、工作目录和中文输出。"""
        # 模拟 PyCharm 未激活 Conda，PATH 里没有 agent 的情况。
        with patch.dict(os.environ, {"PATH": os.environ.get("SystemRoot", "/usr/bin")}):
            result = self.run_python(
                "import json, os, sys; print(json.dumps([sys.executable, os.getcwd(), '中文'], ensure_ascii=False))"
            )
        self.assertEqual(result["exit_code"], 0)
        executable, directory, text = json.loads(result["stdout"])
        self.assertEqual(Path(executable), Path(sys.executable))
        self.assertEqual(Path(directory), self.root)
        self.assertEqual(text, "中文")
        self.assertFalse(result["truncated"])
        self.assertFalse(result["timed_out"])

    def test_python_failure_returns_exit_code_and_stderr(self):
        """Python 运行失败仍返回真实退出码和报错，供模型修正。"""
        result = self.run_python("import sys; print('测试错误', file=sys.stderr); sys.exit(7)")
        self.assertEqual(result["exit_code"], 7)
        self.assertIn("测试错误", result["stderr"])

    def test_command_workdir_override_does_not_create_default(self):
        """单次命令指定绝对目录时不用临时目录，其他工具的默认目录不受影响。"""
        self.env = LocalEnvironment()
        work = self.root / "单次 执行"
        work.mkdir()
        with patch.object(environment_module.tempfile, "mkdtemp") as create:
            result = self.run_python("from pathlib import Path; print(Path.cwd())", workdir=str(work))
            create.assert_not_called()
        self.assertEqual(Path(result["stdout"].strip()), work)
        self.assertEqual(result["working_directory"], str(work))
        self.assertIsNone(self.env.working_directory)

    def test_shell_failure_and_stderr_without_failure(self):
        """Shell 自身错误应失败；程序仅输出 stderr 不应被误判为失败。"""
        result = self.env.exec_command("nitty_nonexistent_command_12345")
        self.assertNotEqual(result["exit_code"], 0)
        self.assertTrue(result["stderr"])
        result = self.run_python("import sys; print('提示信息', file=sys.stderr)")
        self.assertEqual(result["exit_code"], 0)
        self.assertIn("提示信息", result["stderr"])

    def test_timeout_stops_child_processes_and_preserves_output(self):
        """超时反馈保留已产生的输出，子进程不会继续写入文件。"""
        child = self.root / "child.py"
        marker = self.root / "should_not_exist.txt"
        child.write_text(
            f"import time\nfrom pathlib import Path\ntime.sleep(3)\nPath({str(marker)!r}).touch()\n",
            encoding="utf-8",
        )
        result = self.run_python(
            f"import subprocess, sys, time\nsubprocess.Popen([sys.executable, {str(child)!r}])\n"
            "print('开始执行', flush=True)\ntime.sleep(60)\n",
            timeout=2,
        )
        self.assertTrue(result["timed_out"])
        self.assertIn("开始执行", result["stdout"])
        time.sleep(3)
        self.assertFalse(marker.exists())

    def test_large_output_reports_truncation(self):
        """长输出明确标记截断，防止模型把部分结果当成完整结果。"""
        result = self.run_python("print('x' * 20001)")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(len(result["stdout"]), 20000)
        self.assertTrue(result["truncated"])


if __name__ == "__main__":
    unittest.main()
