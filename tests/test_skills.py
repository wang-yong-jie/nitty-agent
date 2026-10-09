"""Skill 生命周期的离线集成测试：真实文件、Agent Loop、裁剪和恢复。"""

from copy import deepcopy
from contextlib import redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from contracts import ModelReply, ToolRejected
from core.agent import Agent
from core.checkpoint import restore
from core.context import ContextBuilder
from core.skills import SkillCatalog, SkillManager, configured_catalog, load_skill
from core.state import AgentState
from core.tooling import ToolRegistry
from tests.fakes import MemoryEnvironment, MemoryTrace, SequenceModel, call
from tools.local import create_default_registry


def replace_text(path, content):
    """模拟编辑器的原子保存；仅重试 Windows 测试文件的短暂共享锁。"""
    staging = path.with_name(path.name + ".pending")
    staging.write_text(content, encoding="utf-8")
    deadline = time.monotonic() + 2
    while True:
        try:
            staging.replace(path)
            return
        except PermissionError:
            if os.name != "nt" or time.monotonic() >= deadline:
                raise
            time.sleep(0.02)


def write_skill(root, name="report", *, body="逐项核对数据，不得遗漏。", required="", version="1.0.0"):
    directory = Path(root) / name
    directory.mkdir(parents=True, exist_ok=True)
    replace_text(directory / "SKILL.md",
        f'---\nname: {name}\ndescription: 生成并核对报告。\nmetadata:\n  version: "{version}"\n'
        f'  nitty-required-tools: "{required}"\n---\n{body}\n')
    return directory


class SkillCatalogTests(unittest.TestCase):
    def test_discovery_precedence_disabled_and_bad_package_diagnostics(self):
        with tempfile.TemporaryDirectory() as tmp:
            user, project = Path(tmp) / "user", Path(tmp) / "project"
            write_skill(user, body="用户版")
            write_skill(project, body="项目版")
            write_skill(project, "disabled")
            bad = write_skill(project, "broken")
            replace_text(bad / "SKILL.md", "不是有效格式")
            catalog = SkillCatalog([user, project], ["disabled"])
            catalog.discover()
            self.assertEqual(list(catalog.skills), ["report"])
            self.assertEqual(catalog.skills["report"].instructions, "项目版")
            self.assertEqual({d["code"] for d in catalog.diagnostics}, {"SKILL_SHADOWED", "SKILL_INVALID"})
            replace_text(project / "report" / "SKILL.md", "损坏的项目版")
            catalog.discover()
            self.assertNotIn("report", catalog.skills)

    def test_invalid_yaml_duplicates_names_and_sizes_are_isolated(self):
        cases = [
            "---\nname: report\nname: report\ndescription: x\n---\n正文",
            "---\nname: another\ndescription: x\n---\n正文",
            "---\nname: report\ndescription: x\nmetadata:\n  version: 1\n---\n正文",
            "---\nname: report\ndescription: !!python/object/apply:builtins.str ['invalid']\n---\n正文",
            "---\nname: report\ndescription: " + "[" * 1200 + "x" + "]" * 1200 + "\n---\n正文",
            "x" * 32001,
        ]
        for content in cases:
            with self.subTest(content=content[:100]), tempfile.TemporaryDirectory() as tmp:
                directory = write_skill(tmp)
                replace_text(directory / "SKILL.md", content)
                catalog = SkillCatalog([tmp])
                catalog.discover()
                self.assertFalse(catalog.skills)
                self.assertEqual(catalog.diagnostics[0]["code"], "SKILL_INVALID")

    def test_configuration_requires_explicit_absolute_roots(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
            "NITTY_SKILL_DIRS": json.dumps([tmp]), "NITTY_DISABLED_SKILLS": '["report"]',
        }):
            catalog = configured_catalog(Path(tmp) / "project")
            self.assertEqual(catalog.roots[-1], Path(tmp).resolve())
            self.assertEqual(catalog.disabled, {"report"})
            for value in ('"not-array"', '[42]', '["relative/path"]', 'invalid'):
                os.environ["NITTY_SKILL_DIRS"] = value
                with self.assertRaises(ValueError):
                    configured_catalog(Path(tmp))

    def test_manifest_covers_resources_and_discovery_does_not_execute_script(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = write_skill(tmp)
            script = directory / "danger.py"
            replace_text(script, "raise RuntimeError('MUST NOT EXECUTE')")
            before = load_skill(directory)
            replace_text(script, "raise RuntimeError('CHANGED')")
            after = load_skill(directory)
            self.assertNotEqual(before.sha256, after.sha256)
            self.assertIn("danger.py", dict(before.files))

    def test_no_home_environment_still_has_project_skills(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            with patch("core.skills.Path.home", side_effect=RuntimeError("no home")):
                catalog = configured_catalog(Path(tmp))
            self.assertEqual(catalog.roots, ((Path(tmp) / ".agents" / "skills").resolve(),))


class SkillRuntimeTests(unittest.TestCase):
    def make_agent(self, root, model, **kwargs):
        return Agent(model, create_default_registry(), MemoryEnvironment(), verbose=False,
                     skills=SkillManager(SkillCatalog([root])), **kwargs)

    def test_shared_bootstrap_loads_bundled_skill_with_long_horizon_tools(self):
        import bootstrap

        model = SequenceModel(
            ModelReply(tool_calls=[call("reference", "read_skill_resource", {
                "name": "complex-task", "path": "references/acceptance.md",
            })]),
            ModelReply("验收应检查真实产物、数量和完整性。"),
            ModelReply('{"status":"achieved","evidence":"回答覆盖了所读取的验收方法。"}'),
        )
        trace = MemoryTrace()
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
            "DEEPSEEK_API_KEY": "offline-key", "NITTY_SKILL_DIRS": "[]", "NITTY_DISABLED_SKILLS": "[]",
            "USERPROFILE": tmp,
        }, clear=True), patch.object(bootstrap, "load_settings"), patch.object(bootstrap, "create_model", return_value=model):
            with bootstrap.agent_session(bootstrap.AgentOptions(long_horizon=True, workdir=tmp), trace=trace, verbose=False) as session:
                answer = session.agent.run("/skill complex-task\n解释复杂任务应该怎样验收。")
                self.assertIn("真实产物", answer)
                self.assertEqual(session.agent.last_state.observations[0].tool_name, "read_skill_resource")
                self.assertIn("验收条件应对应用户要求", session.agent.last_state.observations[0].result["content"])
        self.assertIn("先明确用户目标", model.requests[0][0][0].content)
        self.assertTrue(any(e["event"] == "skill_activated" and e["reason"] == "user" for e in trace.events))

    def test_cli_explicit_skill_option_becomes_run_directive(self):
        import cli

        with tempfile.TemporaryDirectory() as tmp, patch("sys.argv", [
            "main.py", "--long-horizon", "--skill", "complex-task", "--workdir", tmp,
        ]), patch("builtins.input", return_value="整理资料"), patch.object(cli, "model_settings"), \
                patch.object(cli, "agent_session") as session_factory, redirect_stdout(StringIO()):
            session = session_factory.return_value.__enter__.return_value
            session.agent.run.return_value = "完成"
            cli.main()
            session.agent.run.assert_called_once_with("/skill complex-task\n整理资料")
            self.assertTrue(session_factory.call_args.args[0].long_horizon)

    def test_model_activation_context_resource_paging_dedup_and_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = write_skill(tmp)
            replace_text(directory / "reference.txt", "甲乙丙丁")
            model = SequenceModel(
                ModelReply(tool_calls=[call("activate", "activate_skill", {"name": "report"})]),
                ModelReply(tool_calls=[call("read1", "read_skill_resource", {"name": "report", "path": "reference.txt", "limit": 2})]),
                ModelReply(tool_calls=[call("read2", "read_skill_resource", {"name": "report", "path": "reference.txt", "offset": 2})]),
                ModelReply(tool_calls=[call("again", "activate_skill", {"name": "report"})]),
                ModelReply("核对完成"),
            )
            trace = MemoryTrace()
            agent = self.make_agent(tmp, model, trace=trace)
            self.assertEqual(agent.run("整理报告"), "核对完成")
            self.assertNotIn("逐项核对数据", model.requests[0][0][0].content)
            for messages, _ in model.requests[1:]:
                self.assertEqual(messages[0].content.count("逐项核对数据"), 1)
            observations = agent.last_state.observations
            self.assertEqual(observations[1].result["content"], "甲乙")
            self.assertTrue(observations[1].result["truncated"])
            self.assertEqual(observations[2].result["content"], "丙丁")
            self.assertTrue(observations[3].result["already_active"])
            self.assertEqual(len(agent.last_state.skill_resources), 1)
            self.assertEqual(sum(e["event"] == "skill_activated" for e in trace.events), 1)
            self.assertEqual(agent.last_checkpoint["skills"]["active"][0]["name"], "report")

    def test_explicit_activation_survives_history_budget_and_new_run_resets(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_skill(tmp, body="关键规则：核对订单总金额。")
            model = SequenceModel(ModelReply("第一轮完成"), ModelReply("另一任务完成"))
            agent = self.make_agent(tmp, model, context=ContextBuilder(max_history_chars=1000, max_history_messages=2))
            agent.run("/skill report\n整理订单。" + "数据" * 1200)
            self.assertIn("关键规则：核对订单总金额", model.requests[0][0][0].content)
            self.assertTrue(agent.last_checkpoint["skills"]["active"])
            agent.run("解释一句话")
            self.assertNotIn("关键规则：核对订单总金额", model.requests[1][0][0].content)
            self.assertEqual(agent.last_state.active_skills, [])

    def test_guidance_survives_mid_run_compaction_of_activation_feedback(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_skill(tmp, body="必须核对真实条数。")
            model = SequenceModel(
                ModelReply(tool_calls=[call("activate", "activate_skill", {"name": "report"})]),
                ModelReply(tool_calls=[call("read", "read_file", {"path": "large.txt"})]),
                ModelReply("完成"),
            )
            agent = self.make_agent(tmp, model, context=ContextBuilder(max_history_chars=1000, max_history_messages=2))
            agent.runtime.environment.files["large.txt"] = "大量数据" * 2000
            agent.run("整理报告")
            final_messages = model.requests[-1][0]
            self.assertIn("必须核对真实条数", final_messages[0].content)
            self.assertFalse(any(m.tool_call_id == "activate" for m in final_messages))

    def test_external_tool_conflict_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_skill(tmp)
            registry = ToolRegistry()
            original = lambda _env: "caller"
            registry.register("activate_skill", "用户工具", {}, [], original)
            manager = SkillManager(SkillCatalog([tmp]))
            with self.assertRaisesRegex(ValueError, "冲突"):
                manager.prepare(AgentState("task"), registry)
            self.assertIs(registry.get("activate_skill").handler, original)

    def test_activation_is_not_allowed_with_other_calls_in_same_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_skill(tmp)
            model = SequenceModel(ModelReply(tool_calls=[
                call("activate", "activate_skill", {"name": "report"}),
                call("write", "write_file", {"path": "out", "content": "bad"}),
            ]), ModelReply("需先激活"))
            agent = self.make_agent(tmp, model)
            agent.run("任务")
            self.assertFalse(agent.runtime.environment.files)
            self.assertFalse(agent.last_state.active_skills)
            self.assertTrue(all(o.status == "rejected" for o in agent.last_state.observations))

    def test_dependencies_and_empty_catalog_do_not_expose_unusable_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_skill(tmp, required="browser_execute")
            model = SequenceModel(ModelReply("需要浏览器"))
            agent = self.make_agent(tmp, model)
            agent.run("任务")
            self.assertEqual([s.name for s in model.requests[0][1]], ["exec_command", "read_file", "write_file"])
            self.assertNotIn("可用 Skill", model.requests[0][0][0].content)
            with self.assertRaisesRegex(ToolRejected, "依赖不可用"):
                agent.run("/skill report\n任务")

    def test_refresh_and_disable_take_effect_next_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = SequenceModel(ModelReply("无技能"), ModelReply("有技能"), ModelReply("已禁用"))
            agent = self.make_agent(tmp, model)
            agent.run("一")
            write_skill(tmp)
            agent.run("二")
            agent.runtime.skills.catalog.disabled = frozenset({"report"})
            agent.run("三")
            self.assertEqual([any(s.name == "activate_skill" for s in specs) for _, specs in model.requests], [False, True, False])

    def test_budget_rejection_does_not_partially_activate_or_truncate(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_skill(tmp, "one", body="a" * 25000)
            write_skill(tmp, "two", body="b" * 25000)
            manager = SkillManager(SkillCatalog([tmp]))
            state = AgentState("task")
            manager.prepare(state, ToolRegistry())
            manager.activate("one")
            with self.assertRaisesRegex(ToolRejected, "预算"):
                manager.activate("two")
            self.assertEqual([r["name"] for r in state.active_skills], ["one"])
            self.assertIn("a" * 25000, manager.context())

    def test_deactivation_removes_guidance_and_resource_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = write_skill(tmp)
            replace_text(directory / "ref.txt", "资料")
            manager = SkillManager(SkillCatalog([tmp]))
            state = AgentState("task")
            manager.prepare(state, ToolRegistry())
            manager.activate("report")
            manager.read_resource("report", "ref.txt")
            manager.deactivate("report")
            self.assertEqual(state.active_skills, [])
            self.assertEqual(state.skill_resources, [])
            self.assertNotIn("逐项核对数据", manager.context())

    def test_changed_and_outside_resources_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = write_skill(tmp)
            reference = directory / "ref.txt"
            replace_text(reference, "原版")
            manager = SkillManager(SkillCatalog([tmp]))
            manager.prepare(AgentState("task"), ToolRegistry())
            with self.assertRaises(ToolRejected):
                manager.read_resource("report", "ref.txt")
            manager.activate("report")
            for path in ("../outside.txt", str(reference.resolve()), "missing.txt", "SKILL.md"):
                with self.subTest(path=path), self.assertRaises(ToolRejected):
                    manager.read_resource("report", path)
            replace_text(reference, "修改版")
            with self.assertRaisesRegex(ToolRejected, "变化"):
                manager.read_resource("report", "ref.txt")

    def test_binary_resource_is_not_decoded_or_executed(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = write_skill(tmp)
            (directory / "template.bin").write_bytes(b"\xff\xfe\x00")
            manager = SkillManager(SkillCatalog([tmp]))
            manager.prepare(AgentState("task"), ToolRegistry())
            manager.activate("report")
            with self.assertRaises(ToolRejected) as failure:
                manager.read_resource("report", "template.bin")
            self.assertEqual(failure.exception.info.code, "SKILL_RESOURCE_BINARY")

    def test_modified_package_between_discovery_and_activation_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = write_skill(tmp)
            manager = SkillManager(SkillCatalog([tmp]))
            state = AgentState("task")
            manager.prepare(state, ToolRegistry())
            replace_text(directory / "SKILL.md", "changed")
            with self.assertRaises(ToolRejected):
                manager.activate("report")
            self.assertFalse(state.active_skills)

    def test_read_resource_rejects_symlink_introduced_after_activation(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = write_skill(tmp)
            reference = directory / "ref.txt"
            other = directory / "other.txt"
            replace_text(reference, "相同内容")
            replace_text(other, "相同内容")
            manager = SkillManager(SkillCatalog([tmp]))
            manager.prepare(AgentState("task"), ToolRegistry())
            manager.activate("report")
            reference.unlink()
            try:
                reference.symlink_to(other)
            except OSError as error:
                self.skipTest(f"操作系统不允许创建测试符号链接：{error}")
            with self.assertRaises(ToolRejected) as failure:
                manager.read_resource("report", "ref.txt")
            self.assertEqual(failure.exception.info.code, "SKILL_RESOURCE_PATH")

    def test_restore_pins_package_and_rejects_changes_before_model_or_actions(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = write_skill(tmp)
            replace_text(directory / "ref.txt", "原始资料")
            interrupted = self.make_agent(tmp, SequenceModel(RuntimeError("中断")))
            with self.assertRaisesRegex(RuntimeError, "中断"):
                interrupted.run("/skill report\n整理任务")
            checkpoint = deepcopy(interrupted.last_checkpoint)
            # 恢复走长任务验收；固定测试验收器只隔离与 Skill 无关的模型调用。
            resumed = self.make_agent(tmp, SequenceModel(ModelReply("继续完成")))
            resumed.runtime.controller.finish = lambda _answer: {"status": "achieved", "evidence": "测试完成"}
            self.assertEqual(resumed.resume(checkpoint), "继续完成")
            self.assertIn("逐项核对数据", resumed.runtime.model.requests[0][0][0].content)
            replace_text(directory / "ref.txt", "变化的资料")
            model = SequenceModel(ModelReply("不应调用"))
            changed = self.make_agent(tmp, model)
            with self.assertRaisesRegex(ToolRejected, "原始 Skill"):
                changed.resume(checkpoint)
            self.assertFalse(model.requests)
            self.assertFalse(changed.runtime.environment.commands)
            self.assertEqual(changed.last_state.error_info.code, "SKILL_VERSION_MISMATCH")

    def test_checkpoint_snapshot_is_independent_and_old_checkpoint_supported(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_skill(tmp)
            agent = self.make_agent(tmp, SequenceModel(RuntimeError("stop")))
            with self.assertRaises(RuntimeError):
                agent.run("/skill report\n任务")
            checkpoint = agent.last_checkpoint
            agent.last_state.active_skills.clear()
            self.assertEqual(len(checkpoint["skills"]["active"]), 1)
            old = {k: v for k, v in checkpoint.items() if k != "skills"}
            self.assertEqual(restore(old).active_skills, [])
            bad = deepcopy(checkpoint)
            bad["skills"]["schema_version"] = True
            with self.assertRaises(ValueError):
                restore(bad)

    def test_unknown_explicit_skill_fails_before_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = SequenceModel(ModelReply("不应调用"))
            agent = self.make_agent(tmp, model)
            with self.assertRaisesRegex(ToolRejected, "不存在"):
                agent.run("/skill missing\n任务")
            self.assertEqual(model.requests, [])

    def test_checkpoint_with_skills_requires_manager_and_checks_resource_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = write_skill(tmp)
            replace_text(directory / "ref.txt", "资料")
            interrupted = self.make_agent(tmp, SequenceModel(RuntimeError("stop")))
            with self.assertRaises(RuntimeError):
                interrupted.run("/skill report\n任务")
            checkpoint = deepcopy(interrupted.last_checkpoint)
            no_manager = Agent(SequenceModel(ModelReply("不应调用")), ToolRegistry(), MemoryEnvironment(), verbose=False)
            with self.assertRaisesRegex(ValueError, "没有配置"):
                no_manager.resume(checkpoint)
            checkpoint["skills"]["resources"] = [{"skill": "report", "path": "ref.txt", "sha256": "wrong"}]
            model = SequenceModel(ModelReply("不应调用"))
            with self.assertRaises(ToolRejected) as failure:
                self.make_agent(tmp, model).resume(checkpoint)
            self.assertEqual(failure.exception.info.code, "SKILL_CHECKPOINT_INVALID")
            self.assertFalse(model.requests)


if __name__ == "__main__":
    unittest.main()
