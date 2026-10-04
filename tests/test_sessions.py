"""连续追问、历史预算、工具协议和跨进程持久化的离线验证。"""

import json
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch

import bootstrap
import cli

from bootstrap import AgentOptions
from adapters.local import LocalEnvironment
from contracts import AgentCancelled, ImageContent, Message, ModelReply
from core.agent import Agent
from core.context import ContextBuilder
from core.session import Session, trim_history
from core.state import AgentState
from core.tooling import ToolRegistry
from service.manager import TaskManager
from service.store import TaskStore
from tests.fakes import MemoryEnvironment, SequenceModel, call
from tests.service_workers import fixture_worker, wait_terminal
from tools.local import create_default_registry


class SessionTests(unittest.TestCase):
    def test_cli_chat_resume_and_new_session(self):
        with tempfile.TemporaryDirectory() as directory:
            history_file = Path(directory) / "session.json"
            model = SequenceModel(ModelReply("记住了海蓝。"), ModelReply("刚才是海蓝。"), ModelReply("新话题。"))
            def run(inputs, extra=()):
                with (patch("sys.argv", ["main.py", "--chat", "--session-file", str(history_file), *extra]),
                      patch("builtins.input", side_effect=inputs), patch.dict("os.environ", {"DEEPSEEK_API_KEY": "test-key"}),
                      patch.object(bootstrap, "load_dotenv"), patch.object(bootstrap, "create_model", return_value=model),
                      redirect_stdout(StringIO())):
                    cli.main()
            run(["项目叫海蓝", "/exit"])
            first_id = json.loads(history_file.read_text(encoding="utf-8"))["id"]
            run(["刚才叫什么", "/new", "新话题", "/exit"])
            self.assertIn("项目叫海蓝", [m.content for m in model.requests[1][0]])
            self.assertEqual([m.role for m in model.requests[2][0]], ["system", "user"])
            saved = Session.from_dict(json.loads(history_file.read_text(encoding="utf-8")))
            self.assertNotEqual(saved.id, first_id)
            self.assertEqual([m.content for m in saved.messages], ["新话题", "新话题。"])

    def test_new_run_reuses_persisted_working_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            environment = LocalEnvironment(Path(directory) / "work")
            model = SequenceModel(ModelReply(tool_calls=[call("write", "write_file", {"path": "note", "content": "蓝色"})]),
                                  ModelReply("已写入"))
            agent = Agent(model, create_default_registry(), environment, verbose=False)
            agent.run("保存选择")
            restored = Session.from_dict(agent.session.to_dict())
            self.assertEqual(restored.working_directory, str(environment.working_directory))
            model = SequenceModel(ModelReply(tool_calls=[call("read", "read_file", {"path": "note"})]), ModelReply("追问完成"))
            followup = Agent(model, create_default_registry(), LocalEnvironment(restored.working_directory), session=restored, verbose=False)
            followup.run("刚才保存了什么")
            self.assertEqual(followup.last_state.observations[0].result["content"], "蓝色")
            self.assertIn("蓝色", " ".join(c.arguments for m in model.requests[0][0] for c in m.tool_calls))

    def test_followup_receives_previous_question_answer_and_tool_result(self):
        model = SequenceModel(ModelReply(tool_calls=[call("read", "read_file", {"path": "note"})]),
                              ModelReply("读到了蓝色。"), ModelReply("你刚才选择了蓝色。"), ModelReply("新开始。"))
        environment = MemoryEnvironment()
        environment.files["note"] = "蓝色"
        agent = Agent(model, create_default_registry(), environment, verbose=False)
        agent.run("读取我的选择")
        previous = agent.last_state
        session_id = agent.session.id
        agent.run("我刚才选择了什么？")
        history = model.requests[2][0]
        self.assertEqual([message.role for message in history], ["system", "user", "assistant", "tool", "assistant", "user"])
        self.assertIn("蓝色", history[3].content)
        self.assertEqual(agent.last_state.turn, 1)
        self.assertEqual(agent.last_state.observations, [])
        self.assertEqual(previous.task, "读取我的选择")
        self.assertEqual(agent.last_state.session_id, session_id)
        agent.new_session()
        agent.run("新任务")
        self.assertNotEqual(agent.session.id, session_id)
        self.assertEqual([message.role for message in model.requests[3][0]], ["system", "user"])

    def test_cancelled_batch_has_paired_history_and_can_be_followed_up(self):
        registry = ToolRegistry()
        def stop(environment):
            raise AgentCancelled("用户停止")
        registry.register("stop", "stop", {}, [], stop)
        model = SequenceModel(ModelReply(tool_calls=[call("a", "stop", {}), call("b", "stop", {})]), ModelReply("已核实。"))
        agent = Agent(model, registry, MemoryEnvironment(), verbose=False)
        with self.assertRaises(AgentCancelled):
            agent.run("执行")
        self.assertEqual([m.tool_call_id for m in agent.session.messages if m.role == "tool"], ["a", "b"])
        self.assertTrue(all(m.is_error for m in agent.session.messages if m.role == "tool"))
        # 原始状态没有伪造未执行工具的 Observation。
        self.assertEqual(len(agent.last_state.observations), 1)
        agent.run("先核实状态")
        self.assertEqual(agent.last_state.status, "completed")

    def test_budget_preserves_latest_question_and_atomic_tool_groups(self):
        messages = [Message("user", "早期" * 200), Message("assistant", "旧答案" * 200), Message("user", "最新问题")]
        for index in range(12):
            messages.extend([Message("assistant", tool_calls=[call(str(index), "read", {})]),
                             Message("tool", "结果" * 200, tool_call_id=str(index))])
        original = list(messages)
        bounded, omitted = trim_history(messages, 1500, 8)
        self.assertGreater(omitted, 0)
        self.assertIn("最新问题", [m.content for m in bounded])
        self.assertLessEqual(len(bounded), 8)
        self.assertLessEqual(sum(len(m.content or "") + sum(len(c.arguments) + len(c.name) + len(c.id)
                                                          for c in m.tool_calls) + 64 for m in bounded), 1500)
        self.assertEqual([c.id for m in bounded for c in m.tool_calls], [m.tool_call_id for m in bounded if m.role == "tool"])
        built = ContextBuilder(max_history_chars=1500, max_history_messages=8).build_messages(AgentState("最新问题", messages=messages), {}, [])
        self.assertIn("裁剪", built[0].content)
        self.assertEqual(messages, original)

    def test_serialization_omits_screenshots_and_roundtrips_tool_calls(self):
        session = Session()
        session.update([Message("user", "看截图"), Message("assistant", tool_calls=[call("a", "screen", {})]),
                        Message("tool", "尺寸信息", tool_call_id="a", images=[ImageContent("private-base64")])])
        serialized = json.dumps(session.to_dict(), ensure_ascii=False)
        self.assertNotIn("private-base64", serialized)
        restored = Session.from_dict(json.loads(serialized))
        self.assertEqual(restored.messages[1].tool_calls[0].id, "a")
        self.assertIn("重新截图", restored.messages[2].content)
        self.assertEqual(restored.id, session.id)

    def test_service_restart_followup_and_new_session_isolation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = TaskManager(root, root / "logs", worker_target=fixture_worker)
            try:
                first = manager.submit("我的项目叫海蓝", AgentOptions())
                wait_terminal(manager, first["id"])
                session_id = first["session_id"]
            finally:
                manager.close()
            manager = TaskManager(root, root / "logs", worker_target=fixture_worker)
            try:
                followup = manager.submit("刚才的项目叫什么 [history]", AgentOptions(), session_id)
                result = wait_terminal(manager, followup["id"])
                self.assertIn("我的项目叫海蓝", result["answer"])
                self.assertNotEqual(first["id"], followup["id"])
                self.assertEqual(len(manager.store.session_tasks(session_id)), 2)
                isolated = manager.submit("新会话 [history]", AgentOptions())
                result = wait_terminal(manager, isolated["id"])
                self.assertNotIn("海蓝", result["answer"])
                self.assertNotEqual(isolated["session_id"], session_id)
            finally:
                manager.close()

    def test_migration_preserves_legacy_answers(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tasks.sqlite3"
            store = TaskStore(path)
            store.save(dict(id="old", task="旧问题", answer="旧答案", status="completed"))
            store.close()
            store = TaskStore(path)
            try:
                session_id = store.get("old")["session_id"]
                history = Session.from_dict(store.session_history(session_id))
                self.assertEqual([m.content for m in history.messages], ["旧问题", "旧答案"])
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
