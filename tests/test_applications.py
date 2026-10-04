"""应用发现的失败边界与工具装配测试；不依赖本机安装了哪些应用。"""

import base64
import json
import unittest
from unittest.mock import Mock

from core.agent import Agent
from tools.applications import register_application_tools
from adapters.windows_apps import WindowsApplicationCatalog
from contracts import AgentCancelled, ModelReply
from adapters.windows_desktop import WindowsDesktop
from tools.desktop import register_desktop_tools
from adapters.models import TextOnlyModel
from tests.fakes import FakeBackend, MemoryEnvironment, SequenceModel, call
from core.tooling import ToolExecutor, ToolRegistry


def response(matches=None, errors=None, **changes):
    result = {
        "exit_code": 0, "timed_out": False, "truncated": False, "stderr": "",
        "stdout": json.dumps({"matches": matches or [], "errors": errors or [], "truncated": False}),
    }
    return {**result, **changes}


class ApplicationTests(unittest.TestCase):
    def test_store_app_is_found_without_conventional_install_directory(self):
        runner = Mock(side_effect=[response(), response([{
            "name": "TelegramMessengerLLP.TelegramDesktop", "package_family_name": "example_family",
            "install_location": r"C:\Program Files\WindowsApps\example",
        }]), response(), response()])
        result = WindowsApplicationCatalog(runner).find("Telegram Desktop")
        self.assertEqual(result["status"], "found")
        self.assertEqual(result["matches"][0]["source"], "appx")
        self.assertTrue(result["complete"])
        self.assertEqual(result["checked_sources"], result["attempted_sources"])
        self.assertTrue(all(c.kwargs["timeout"] == 10 for c in runner.call_args_list))

    def test_shortcut_and_distinct_same_name_candidates_are_preserved(self):
        rows = [{"name": "Editor", "shortcut_path": "C:/A.lnk"},
                {"name": "Editor", "shortcut_path": "C:/B.lnk"}]
        runner = Mock(side_effect=[response(), response(), response([*rows, rows[0]]), response()])
        result = WindowsApplicationCatalog(runner).find("Editor")
        self.assertEqual(len(result["matches"]), 2)
        self.assertEqual({r["shortcut_path"] for r in result["matches"]}, {"C:/A.lnk", "C:/B.lnk"})

    def test_empty_complete_result_is_scoped_not_found(self):
        result = WindowsApplicationCatalog(Mock(return_value=response())).find("Missing app")
        self.assertEqual(result["status"], "not_found")
        self.assertTrue(result["complete"])
        self.assertIn("便携应用", result["scope"])

    def test_timeout_even_with_exit_zero_is_incomplete_and_other_sources_continue(self):
        runner = Mock(side_effect=[response(timed_out=True), response(), response(), response()])
        result = WindowsApplicationCatalog(runner).find("Missing app")
        self.assertEqual(result["status"], "incomplete")
        self.assertNotIn("start_apps", result["checked_sources"])
        self.assertIn("超时", result["errors"][0]["message"])
        self.assertEqual(runner.call_count, 4)

    def test_positive_evidence_survives_other_source_failure(self):
        runner = Mock(side_effect=[response([{"name": "Editor", "app_id": "editor.id"}]),
                                   PermissionError("denied"), response(), response()])
        result = WindowsApplicationCatalog(runner).find("Editor")
        self.assertEqual(result["status"], "found")
        self.assertFalse(result["complete"])
        self.assertEqual(result["errors"][0]["source"], "appx")

    def test_partial_source_preserves_matches_and_reports_incomplete_coverage(self):
        runner = Mock(side_effect=[response(), response(), response(
            [{"name": "Editor", "shortcut_path": "C:/Editor.lnk"}], errors=["Public folder denied"]
        ), response()])
        result = WindowsApplicationCatalog(runner).find("Editor")
        self.assertEqual(result["status"], "found")
        self.assertNotIn("shortcuts", result["checked_sources"])
        self.assertFalse(result["complete"])

    def test_corrupt_truncated_failed_or_stderr_output_is_never_absence(self):
        for failed in (
            response(stdout=""), response(stdout="null"), response(stdout="{}"),
            response(stdout='{"matches":[null],"errors":[],"truncated":false}'),
            response(truncated=True), response(exit_code=1), response(stderr="query warning"),
            response(stdout='{"matches":[],"errors":[],"truncated":true}'),
        ):
            with self.subTest(failed=failed):
                runner = Mock(side_effect=[failed, response(), response(), response()])
                result = WindowsApplicationCatalog(runner).find("Editor")
                self.assertEqual(result["status"], "incomplete")
                self.assertTrue(result["errors"])

    def test_user_name_is_encoded_as_data_and_invalid_name_never_executes(self):
        runner = Mock(return_value=response())
        catalog = WindowsApplicationCatalog(runner)
        name = "编辑器'; throw 'injected'; #"
        catalog.find(name)
        for invocation in runner.call_args_list:
            script = invocation.args[0]
            self.assertNotIn(name, script)
            self.assertIn(base64.b64encode(name.encode()).decode(), script)
        runner.reset_mock()
        for name in ("", "  ", "*?", "x" * 201, None, 123):
            with self.subTest(name=name), self.assertRaises(ValueError):
                catalog.find(name)
        runner.assert_not_called()

    def test_cancellation_is_not_reported_as_a_query_failure(self):
        runner = Mock(side_effect=AgentCancelled("stop"))
        with self.assertRaises(AgentCancelled):
            WindowsApplicationCatalog(runner).find("Editor")
        self.assertEqual(runner.call_count, 1)
        runner.reset_mock()
        check = Mock(side_effect=[None, AgentCancelled("stop")])
        runner.side_effect = None
        runner.return_value = response()
        with self.assertRaises(AgentCancelled):
            WindowsApplicationCatalog(runner, cancel_check=check).find("Editor")
        self.assertEqual(runner.call_count, 1)

    def test_bound_catalog_does_not_use_executor_environment(self):
        catalog = Mock()
        catalog.find.return_value = {"status": "found", "matches": [{"name": "Editor"}]}
        registry = ToolRegistry()
        register_application_tools(registry, catalog)
        other_environment = MemoryEnvironment()
        observation = ToolExecutor(registry, other_environment).execute(call("find", "app_find", {"name": "Editor"}))
        self.assertIsNone(observation.error)
        catalog.find.assert_called_once_with("Editor")
        self.assertEqual(other_environment.commands, [])
        with self.assertRaises(ValueError):
            register_application_tools(ToolRegistry(), None)

    def test_catalog_feedback_is_available_in_both_vision_modes_without_calling_vlm(self):
        for separate in (False, True):
            with self.subTest(separate=separate), WindowsDesktop(backend=FakeBackend(), settle_seconds=0) as desktop:
                registry, vision = ToolRegistry(), Mock()
                register_desktop_tools(registry, desktop, vision=vision if separate else None)
                register_application_tools(registry, Mock(find=Mock(return_value={
                    "status": "incomplete", "matches": [], "errors": [{"source": "appx", "message": "timeout"}],
                })))
                model = SequenceModel(
                    ModelReply(tool_calls=[call("find", "app_find", {"name": "Editor"})]),
                    ModelReply(tool_calls=[call("screen", "desktop_screenshot", {})]),
                    ModelReply("查询未完成，需要结合界面继续核实。"),
                )
                Agent(TextOnlyModel(model) if separate else model, registry, MemoryEnvironment(), verbose=False).run("检查应用")
                self.assertIn('"incomplete"', model.requests[1][0][-1].content)
                self.assertIn("不能断言未安装", model.requests[0][0][0].content)
                self.assertEqual(bool(model.requests[2][0][-1].images), not separate)
                vision.answer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
