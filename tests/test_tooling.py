"""验证执行前拒绝、业务拒绝、部分执行与结果失败的真实边界。"""

import unittest
from unittest.mock import Mock

from jsonschema import SchemaError

from contracts import ToolExecutionError, ToolRejected, ToolResult, ToolCall
from core.tooling import ToolExecutor, ToolRegistry
from tests.fakes import MemoryEnvironment, call


class ToolContractTests(unittest.TestCase):
    def setUp(self):
        self.handler = Mock(return_value={"ok": True})
        self.registry = ToolRegistry()
        self.registry.register("update", "更新", {
            "count": {"type": "integer", "minimum": 1, "maximum": 3},
            "mode": {"type": "string", "enum": ["new", "edit"]},
            "names": {"type": "array", "minItems": 1, "maxItems": 2, "items": {
                "type": "string", "minLength": 1, "maxLength": 4,
            }},
            "options": {"type": "object", "properties": {"enabled": {"type": "boolean"}},
                        "required": ["enabled"], "additionalProperties": False},
        }, ["count"], self.handler)
        self.executor = ToolExecutor(self.registry, MemoryEnvironment())

    def test_invalid_arguments_never_enter_handler(self):
        for arguments in (
            {}, {"count": "2"}, {"count": True}, {"count": 2.0}, {"count": 0}, {"count": 4},
            {"count": 1, "unknown": 1}, {"count": 1, "mode": "other"},
            {"count": 1, "names": []}, {"count": 1, "names": ["a", "b", "c"]},
            {"count": 1, "names": ["too long"]}, {"count": 1, "names": [42]},
            {"count": 1, "options": {}}, {"count": 1, "options": {"enabled": 1}},
            {"count": 1, "options": {"enabled": True, "other": 1}}, [],
        ):
            with self.subTest(arguments=arguments):
                result = self.executor.execute(call("bad", "update", arguments))
                self.assertEqual(result.status, "rejected")
                self.assertFalse(result.executed)
                self.assertEqual(result.error_info.code, "INVALID_ARGUMENTS")
                self.assertEqual(result.error_info.phase, "validation")
                self.assertEqual(result.error_info.side_effects, "none")
        self.handler.assert_not_called()

    def test_unknown_tool_and_invalid_json_are_distinct(self):
        result = self.executor.execute(call("unknown", "missing", {}))
        self.assertEqual(result.error_info.code, "UNKNOWN_TOOL")
        for arguments in ('{bad', '{"count":NaN}', '{"count":Infinity}', '{"count":1e999}'):
            with self.subTest(arguments=arguments):
                result = self.executor.execute(ToolCall("bad", "update", arguments))
                self.assertEqual(result.error_info.code, "INVALID_JSON")
                self.assertFalse(result.executed)
        self.handler.assert_not_called()

    def test_nested_valid_arguments_preserve_types_and_execution_boundary(self):
        order = []
        self.handler.side_effect = lambda *_args, **kwargs: order.append(("handler", kwargs)) or 0
        args = {"count": 2, "mode": "edit", "names": ["中文"], "options": {"enabled": True}}
        result = self.executor.execute(call("ok", "update", args), on_execution_start=lambda: order.append("validated"))
        self.assertEqual(order, ["validated", ("handler", args)])
        self.assertTrue(result.executed)
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.result, 0)

    def test_business_rejection_is_explicit_even_after_handler_entry(self):
        self.handler.side_effect = ToolRejected("STALE_FRAME", "重新截图")
        result = self.executor.execute(call("frame", "update", {"count": 1}))
        self.assertTrue(result.executed)
        self.assertEqual(result.status, "rejected")
        self.assertEqual(result.error_info.side_effects, "none")

    def test_handler_failure_preserves_possible_side_effects(self):
        effects = []
        def fail(_environment, count):
            effects.append(count)
            raise OSError("写入后失败")
        self.registry.register("fail", "失败", {"count": {"type": "integer"}}, ["count"], fail)
        result = self.executor.execute(call("failed", "fail", {"count": 2}))
        self.assertEqual(effects, [2])
        self.assertEqual(result.error_info.code, "EXECUTION_FAILED")
        self.assertEqual(result.error_info.phase, "execution")
        self.assertEqual(result.error_info.side_effects, "possible")
        self.assertEqual(result.status, "failed")
        self.assertTrue(result.executed)

    def test_result_failures_do_not_claim_no_execution(self):
        for value in (object(), float("nan"), ToolResult({}, images=["invalid"])):
            with self.subTest(value=type(value).__name__):
                self.handler.return_value = value
                result = self.executor.execute(call("result", "update", {"count": 1}))
                self.assertEqual(result.error_info.code, "INVALID_RESULT")
                self.assertEqual(result.error_info.phase, "result")
                self.assertEqual(result.error_info.side_effects, "possible")
                self.assertTrue(result.executed)

    def test_explicit_partial_execution_error_is_preserved(self):
        self.handler.side_effect = ToolExecutionError("POST_ACTION_CAPTURE_FAILED", "动作已发送", phase="result")
        result = self.executor.execute(call("partial", "update", {"count": 1}))
        self.assertEqual(result.error_info.code, "POST_ACTION_CAPTURE_FAILED")
        self.assertEqual(result.status, "failed")
        self.assertTrue(result.executed)

    def test_invalid_registration_fails_before_use(self):
        with self.assertRaises(SchemaError):
            self.registry.register("bad", "bad", {"x": {"type": "unknown"}}, [], self.handler)
        with self.assertRaises(ValueError):
            self.registry.register("bad", "bad", {}, ["missing"], self.handler)
        self.assertEqual(len(self.registry.specs()), 1)

    def test_schema_cannot_be_changed_by_input_or_model_context(self):
        properties = {"x": {"type": "integer"}}
        self.registry.register("immutable", "test", properties, ["x"], self.handler)
        properties["x"]["type"] = "string"
        specs = self.registry.specs()
        specs[-1].parameters["properties"]["x"]["type"] = "string"
        result = self.executor.execute(call("bad", "immutable", {"x": "text"}))
        self.assertEqual(result.status, "rejected")
        self.assertEqual(self.registry.specs()[-1].parameters["properties"]["x"]["type"], "integer")

    def test_remote_references_cannot_trigger_network_retrieval(self):
        self.registry.register("reference", "test", {"x": {"$ref": "https://example.invalid/schema"}}, ["x"], self.handler)
        result = self.executor.execute(call("ref", "reference", {"x": 1}))
        self.assertEqual(result.error_info.code, "INVALID_SCHEMA")
        self.handler.assert_not_called()


if __name__ == "__main__":
    unittest.main()
