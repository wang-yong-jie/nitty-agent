"""长任务的独立验收、摘要、保存屏障与恢复行为。"""

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from dataclasses import asdict
from contextlib import redirect_stdout
from io import StringIO

from contracts import AgentCancelled, ModelReply, Observation
from core.agent import Agent
from core.checkpoint import CheckpointWriteError, restore, save_file
from core.context import ContextBuilder
from core.session import Session
from core.state import AgentState
from core.task_memory import TaskMemory
from core.verification import verify_checks
from tests.fakes import MemoryEnvironment, SequenceModel, call
from tools.local import create_default_registry


def step():
    return {"id": "save", "title": "保存文件", "success_criteria": "out.txt 内容为 完成", "checks": [
        {"kind": "file_equals", "path": "out.txt", "expected": "完成"}]}


def reply(name, arguments, call_id=None):
    return ModelReply(tool_calls=[call(call_id or name, name, arguments)])


class LongHorizonTests(unittest.TestCase):
    def agent(self, model, environment=None, **options):
        return Agent(model, create_default_registry(), environment or MemoryEnvironment(), verbose=False,
                     max_turns=20, long_horizon=True, **options)

    def test_steps_only_complete_after_actual_file_verification(self):
        environment = MemoryEnvironment()
        agent = self.agent(SequenceModel(reply("task_plan", {"steps": [step()]}),
            reply("write_file", {"path": "out.txt", "content": "完成"}),
            reply("task_verify", {"step_id": "save"}), ModelReply("文件已保存。")), environment)
        self.assertEqual(agent.run("保存文件并核实内容"), "文件已保存。")
        self.assertEqual(agent.last_state.memory.milestones[0].status, "completed")
        self.assertEqual(agent.last_state.verification["status"], "achieved")
        self.assertEqual(environment.files["out.txt"], "完成")

    def test_unverified_step_cannot_be_completed_by_final_answer(self):
        agent = self.agent(SequenceModel(reply("task_plan", {"steps": [step()]}), ModelReply("完成"), ModelReply("完成")))
        with self.assertRaisesRegex(RuntimeError, "未验收步骤"):
            agent.run("保存文件")
        self.assertIsNone(agent.last_state.answer)
        self.assertEqual(agent.last_state.memory.milestones[0].status, "pending")

    def test_forged_completion_field_is_rejected(self):
        agent = self.agent(SequenceModel(reply("task_plan", {"steps": [{**step(), "status": "completed"}]}),
                                        ModelReply("好了"), ModelReply("好了")))
        with self.assertRaises(RuntimeError):
            agent.run("保存文件")
        self.assertEqual(agent.last_state.observations[0].status, "rejected")

    def test_completed_step_is_reverified_against_current_artifact(self):
        environment = MemoryEnvironment()
        class ChangedModel(SequenceModel):
            def generate(self, messages, tools):
                result = super().generate(messages, tools)
                if not result.tool_calls:
                    environment.files["out.txt"] = "被修改"
                return result
        agent = self.agent(ChangedModel(reply("task_plan", {"steps": [step()]}),
            reply("write_file", {"path": "out.txt", "content": "完成"}), reply("task_verify", {"step_id": "save"}),
            ModelReply("已完成"), ModelReply("已完成")), environment)
        with self.assertRaisesRegex(RuntimeError, "不符合验收条件"):
            agent.run("保存文件")
        self.assertEqual(agent.last_state.verification["status"], "unmet")

    def test_file_and_command_checks_do_not_accept_timeout_or_truncation(self):
        environment = MemoryEnvironment()
        environment.files["out.txt"] = "完成"
        self.assertEqual(verify_checks(step()["checks"], environment, [])["status"], "achieved")
        environment.read_file = Mock(return_value={"content": "完成", "truncated": True})
        self.assertEqual(verify_checks(step()["checks"], environment, [])["status"], "uncertain")
        environment.read_file = Mock(side_effect=AgentCancelled("stop"))
        with self.assertRaises(AgentCancelled):
            verify_checks(step()["checks"], environment, [])
        observation = Observation("cmd", "exec_command", result={"exit_code": 0, "timed_out": True})
        check = [{"kind": "command_succeeded", "call_id": "cmd"}]
        self.assertEqual(verify_checks(check, environment, [observation])["status"], "unmet")
        self.assertEqual(verify_checks(check, environment, [])["status"], "uncertain")

    def test_semantic_summary_and_task_state_survive_serialization(self):
        from contracts import Message
        compactor = Mock(return_value="用户要求中文；已核实 60 个文件；剩余 40 个。")
        session = Session(max_history_chars=1000, max_history_messages=4, compactor=compactor)
        messages = [Message("user", "只使用中文。"), Message("tool", "x" * 1200), Message("user", "继续处理")]
        session.update(messages)
        # 孤立工具结果会被排除；用正常长回答触发摘要。
        session.update([Message("user", "只使用中文。"), Message("assistant", "x" * 1200), Message("user", "继续处理")])
        restored = Session.from_dict(json.loads(json.dumps(session.to_dict(), ensure_ascii=False)))
        self.assertEqual(restored.summary_mode, "semantic")
        self.assertIn("60", restored.summary)
        state = AgentState("处理 100 个文件", memory=TaskMemory("处理 100 个文件", constraints=["只使用中文"]),
                           context_summary=restored.summary, messages=restored.messages)
        context = ContextBuilder().build_messages(state, {}, [])
        self.assertIn("只使用中文", context[0].content)
        self.assertTrue(any("剩余 40" in (item.content or "") for item in context))

    def test_summary_failure_falls_back_without_inventing_success(self):
        from contracts import Message
        session = Session(max_history_chars=1000, max_history_messages=3, compactor=Mock(side_effect=ValueError("bad JSON")))
        session.update([Message("user", "必须核实输出"), Message("assistant", "a" * 1300), Message("user", "继续")])
        self.assertEqual(session.summary_mode, "extractive")
        self.assertIn("可能不完整", session.summary)
        session.compactor = Mock(side_effect=AgentCancelled("stop"))
        with self.assertRaises(AgentCancelled):
            session.update([Message("user", "另一个约束"), Message("assistant", "b" * 1300), Message("user", "继续")])

    def test_checkpoint_save_failure_stops_before_write(self):
        environment = MemoryEnvironment()
        def persist(data):
            if data["phase"] == "executing":
                raise OSError("disk full")
        agent = self.agent(SequenceModel(reply("write_file", {"path": "out.txt", "content": "完成"})),
                           environment, on_checkpoint=persist)
        with self.assertRaises(CheckpointWriteError):
            agent.run("保存文件")
        self.assertNotIn("out.txt", environment.files)
        self.assertEqual(agent.last_state.error_info.code, "CHECKPOINT_WRITE_FAILED")

    def test_cancellation_while_saving_stops_before_tool_effect(self):
        environment = MemoryEnvironment()
        stopping = False
        def persist(data):
            nonlocal stopping
            if data["phase"] == "executing":
                stopping = True
        def check_cancelled():
            if stopping:
                raise AgentCancelled("stop after checkpoint commit")
        agent = self.agent(SequenceModel(reply("write_file", {"path": "out.txt", "content": "完成"})),
                           environment, on_checkpoint=persist, cancel_check=check_cancelled)
        with self.assertRaises(AgentCancelled):
            agent.run("保存文件")
        self.assertNotIn("out.txt", environment.files)
        self.assertEqual(agent.last_state.status, "cancelled")

    def test_final_checkpoint_failure_is_not_reported_as_completed(self):
        def persist(data):
            if data["phase"] == "finished":
                raise OSError("disk full")
        agent = Agent(SequenceModel(ModelReply("回答")), create_default_registry(), MemoryEnvironment(),
                      verbose=False, on_checkpoint=persist)
        with self.assertRaises(CheckpointWriteError):
            agent.run("问候")
        self.assertEqual(agent.last_state.status, "failed")
        self.assertIsNone(agent.last_state.answer)
        self.assertEqual(agent.last_state.error_info.code, "CHECKPOINT_WRITE_FAILED")
        self.assertEqual(agent.last_checkpoint["status"], "failed")

    def test_invalid_progress_update_is_atomic(self):
        agent = self.agent(SequenceModel(reply("task_progress", {"facts": ["新事实"], "current_step": "missing"}),
            ModelReply("完成")), task_verifier=lambda *_: {"status": "achieved", "evidence": "回答满足要求"})
        agent.run("回答问题")
        self.assertEqual(agent.last_state.observations[0].status, "rejected")
        self.assertEqual(agent.last_state.memory.facts, [])

    def test_repeated_observations_stop_without_claiming_progress(self):
        environment = MemoryEnvironment()
        environment.files["out.txt"] = "仍未完成"
        repeated = reply("read_file", {"path": "out.txt"}, "read")
        agent = self.agent(SequenceModel(*[repeated] * 15), environment)
        with self.assertRaisesRegex(RuntimeError, "八次操作"):
            agent.run("处理文件")
        self.assertEqual(agent.last_state.error_info.code, "NO_PROGRESS")
        self.assertEqual(agent.last_state.memory.no_progress_turns, 8)
        self.assertEqual(len(agent.last_state.observations), 9)

    def test_paged_state_preserves_constraints_and_labels_truncated_checks(self):
        memory = TaskMemory("处理文件", constraints=[f"约束-{i}: " + "x" * 400 for i in range(100)])
        large_step = {**step(), "checks": [{"kind": "file_equals", "path": "out.txt", "expected": "x" * 1500}]}
        memory.set_plan([large_step])
        agent = self.agent(SequenceModel())
        agent.runtime.state = AgentState("处理文件", memory=memory)
        first = agent.runtime.executor.execute(call("page", "task_state", {"section": "milestones"}))
        check = first.result["items"][0]["checks"][0]
        self.assertTrue(check["expected_truncated"])
        self.assertEqual(len(memory.milestones[0].checks[0]["expected"]), 1500)
        page = agent.runtime.executor.execute(call("page2", "task_state", {"section": "constraints", "limit": 10}))
        self.assertEqual(page.result["items"], memory.constraints[:10])
        self.assertEqual(page.result["next_offset"], 10)
        self.assertGreater(memory.context()["omitted"]["constraints"], 0)
        self.assertEqual(len(memory.to_dict()["constraints"]), 100)
        self.assertNotIn("expected", memory.progress()["milestones"][0]["checks"][0])

    def test_cli_resume_reuses_goal_provider_and_persisted_workdir(self):
        import bootstrap
        import cli
        from bootstrap import AgentOptions
        environment, checkpoint = self.interrupted_write(applied=True)
        with tempfile.TemporaryDirectory() as temporary:
            environment.working_directory = Path(temporary)
            checkpoint["session"]["working_directory"] = temporary
            checkpoint["options"] = asdict(AgentOptions(model="saved-model", long_horizon=True))
            checkpoint_path = Path(temporary) / "checkpoint.json"
            save_file(checkpoint_path, checkpoint)
            model = SequenceModel(reply("task_reconcile", {"action_id": "write", "expected": "核实文件"}),
                reply("task_verify", {"step_id": "save"}), ModelReply("已完成"),
                ModelReply('{"status":"achieved","evidence":"实际文件及里程碑已验收"}'))
            with (patch("sys.argv", ["main.py", "--resume", "--checkpoint-file", str(checkpoint_path)]),
                  patch("builtins.input", side_effect=AssertionError("恢复不应输入新目标")),
                  patch.dict("os.environ", {"DEEPSEEK_API_KEY": "test-key"}), patch.object(bootstrap, "load_dotenv"),
                  patch.object(bootstrap, "create_model", return_value=model) as create,
                  patch.object(bootstrap, "LocalEnvironment", return_value=environment), redirect_stdout(StringIO())):
                cli.main()
            self.assertEqual(create.call_args.kwargs["model_name"], "saved-model")
            saved = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["task"], checkpoint["task"])
            self.assertEqual(saved["status"], "completed")
            self.assertEqual(saved["options"]["workdir"], temporary)
            self.assertEqual(saved["memory"]["attempt"], 2)

    def test_auxiliary_models_validate_json_and_have_no_tools(self):
        from adapters.task_reasoning import ModelCompactor, ModelTaskVerifier
        model = SequenceModel(ModelReply('```json\n{"summary":"已核实文件，剩余验收"}\n```'),
            ModelReply('{"status":"achieved","evidence":"候选回答满足请求"}'),
            reply("write_file", {"path": "bad", "content": "bad"}),
            ModelReply('{"status":"success","evidence":"无证据"}'))
        self.assertIn("剩余", ModelCompactor(model)("", []))
        verifier = ModelTaskVerifier(model)
        state = AgentState("问候")
        self.assertEqual(verifier("问候", state, "你好")["status"], "achieved")
        with self.assertRaisesRegex(ValueError, "不得调用工具"):
            ModelCompactor(model)("", [])
        with self.assertRaisesRegex(ValueError, "格式无效"):
            verifier("问候", state, "你好")
        self.assertTrue(all(not tools for _, tools in model.requests))

    def test_verifier_observations_are_bounded_and_truncation_is_explicit(self):
        from adapters.task_reasoning import ModelTaskVerifier
        model = SequenceModel(ModelReply('{"status":"uncertain","evidence":"长内容证据不足"}'))
        state = AgentState("读取长文件", observations=[Observation(str(i), "read_file", result={"content": "x" * 20000})
                                                     for i in range(40)])
        self.assertEqual(ModelTaskVerifier(model)("验收", state)["status"], "uncertain")
        payload = json.loads(model.requests[0][0][1].content)
        self.assertLess(len(json.dumps(payload, ensure_ascii=False)), 25000)
        self.assertGreater(payload["omitted_observations"], 0)
        self.assertTrue(payload["observations"][-1]["result"]["truncated"])
        self.assertEqual(payload["observations"][-1]["call_id"], "39")

    def interrupted_write(self, *, applied):
        environment = MemoryEnvironment()
        environment.files["out.txt"] = "旧内容"
        saved = []
        def persist(data):
            if data["phase"] == "observed" and any(action["id"] == "write" for action in data["memory"]["actions"]):
                raise OSError("post-action save failed")
            if data["phase"] == "finished":
                raise OSError("post-action save failed")
            saved.append(copy.deepcopy(data))
            if not applied and data["phase"] == "executing" and data["memory"]["actions"][-1]["id"] == "write":
                raise AgentCancelled("stop before effect")
        agent = self.agent(SequenceModel(reply("task_plan", {"steps": [step()]}),
            reply("write_file", {"path": "out.txt", "content": "完成"}, "write")), environment, on_checkpoint=persist)
        with self.assertRaises((CheckpointWriteError, AgentCancelled)):
            agent.run("保存文件")
        return environment, saved[-1]

    def test_resume_reconciles_applied_write_without_replaying_it(self):
        environment, checkpoint = self.interrupted_write(applied=True)
        writes = Mock(wraps=environment.write_file)
        environment.write_file = writes
        agent = self.agent(SequenceModel(reply("task_reconcile", {"action_id": "write", "expected": "文件已写入"}),
            reply("task_verify", {"step_id": "save"}), ModelReply("完成")), environment)
        self.assertEqual(agent.resume(checkpoint), "完成")
        writes.assert_not_called()
        self.assertEqual(agent.last_state.memory.attempt, 2)
        self.assertGreater(agent.last_state.turn, checkpoint["turn"])

    def test_resume_checks_preimage_before_retrying_unapplied_write(self):
        environment, checkpoint = self.interrupted_write(applied=False)
        writes = Mock(wraps=environment.write_file)
        environment.write_file = writes
        agent = self.agent(SequenceModel(reply("task_reconcile", {"action_id": "write", "expected": "核实写入"}),
            reply("write_file", {"path": "out.txt", "content": "完成"}, "retry"),
            reply("task_verify", {"step_id": "save"}), ModelReply("完成")), environment)
        self.assertEqual(agent.resume(checkpoint), "完成")
        writes.assert_called_once()
        self.assertEqual(agent.last_state.memory.actions[1].status, "not_executed")

    def test_unknown_side_effect_blocks_all_further_writes(self):
        memory = TaskMemory("任务")
        old = memory.plan_action(call("old", "exec_command", {"cmd": "run"}), "unknown")
        old.status = "started"
        memory.restore_actions()
        new = memory.plan_action(call("new", "write_file", {"path": "other", "content": "x"}), "write")
        with self.assertRaisesRegex(ValueError, "结果未知"):
            memory.guard(new)
        memory.guard(memory.plan_action(call("read", "read_file", {"path": "out"}), "read"))

    def test_checkpoint_file_roundtrip_and_version_validation(self):
        agent = Agent(SequenceModel(ModelReply("回答")), create_default_registry(), MemoryEnvironment(), verbose=False)
        agent.run("问候")
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "checkpoint.json"
            save_file(path, agent.last_checkpoint)
            data = json.loads(path.read_text(encoding="utf-8"))
        with self.assertRaisesRegex(ValueError, "已完成"):
            restore(data)
        with self.assertRaisesRegex(ValueError, "版本"):
            restore({**data, "schema_version": 99})

    def test_checkpoint_rejects_invalid_counter_and_session_identity(self):
        agent = Agent(SequenceModel(RuntimeError("stop")), create_default_registry(), MemoryEnvironment(), verbose=False)
        with self.assertRaises(RuntimeError):
            agent.run("问候")
        checkpoint = agent.last_checkpoint
        with self.assertRaisesRegex(ValueError, "轮次"):
            restore({**checkpoint, "turn": -1})
        with self.assertRaisesRegex(ValueError, "会话 ID"):
            restore({**checkpoint, "session_id": "other"})
