"""工具注册与执行：定义能力，找到处理函数，将执行结果转成 Observation。"""

import json
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Callable

from jsonschema import Draft202012Validator, ValidationError, validators
from referencing import Registry

from contracts import AgentCancelled, Environment, ErrorInfo, ImageContent, Observation, ToolCall, ToolFailure, ToolResult, ToolSpec


# JSON Schema 的 integer 默认也接受 1.0；工具接口使用严格 Python 整数，避免校验通过后再失败。
ArgumentValidator = validators.extend(Draft202012Validator, type_checker=
    Draft202012Validator.TYPE_CHECKER.redefine("integer", lambda _checker, value: type(value) is int))


@dataclass
class RegisteredTool:
    """描述与实现绑定在一起，避免两个列表出现名称或参数不一致。"""

    spec: ToolSpec
    handler: Callable
    requires_single_call: bool = False
    sensitive_parameters: tuple[str, ...] = ()
    validator: ArgumentValidator | None = None
    effect: str = "unknown"
    recovery_probe: Callable | None = None


class ToolRegistry:
    """每个 Agent 可以使用不同注册表；注册本身不执行任何操作。"""

    def __init__(self):
        self._tools: dict[str, RegisteredTool] = {}

    def register(
        self, name: str, description: str, properties: dict, required: list[str], handler: Callable,
        *, requires_single_call: bool = False, instructions: str = "",
        sensitive_parameters: tuple[str, ...] = (),
        effect: str = "unknown",
        recovery_probe: Callable | None = None,
    ) -> None:
        """注册能力及使用规则；处理函数接收 environment 和模型给出的参数。"""
        if name in self._tools:
            raise ValueError(f"工具已注册：{name}")
        parameters = deepcopy({
            "type": "object", "properties": properties, "required": required,
            "additionalProperties": False,
        })
        ArgumentValidator.check_schema(parameters)
        if any(key not in properties for key in required):
            raise ValueError("required 中的参数必须在 properties 中定义。")
        if not callable(handler):
            raise ValueError("工具处理函数必须可调用。")
        spec = ToolSpec(name, description, parameters, instructions=instructions)
        # 不自动联网解析远程 $ref；本地片段引用仍由校验器支持。
        validator = ArgumentValidator(deepcopy(parameters), registry=Registry())
        if effect not in {"read", "write", "unknown", "internal"}:
            raise ValueError("未知的工具副作用类型。")
        self._tools[name] = RegisteredTool(spec, handler, requires_single_call, sensitive_parameters, validator, effect, recovery_probe)

    def specs(self) -> list[ToolSpec]:
        """只把描述交给模型，真实执行函数不会发送给模型。"""
        return [deepcopy(tool.spec) for tool in self._tools.values()]

    def get(self, name: str) -> RegisteredTool:
        """查找实现；未知工具会在 Executor 边界转成错误反馈。"""
        return self._tools[name]


class ToolExecutor:
    """统一调度边界：解析参数、调用工具、捕获工具错误。"""

    def __init__(self, registry: ToolRegistry, environment: Environment):
        self.registry = registry
        self.environment = environment

    def batch_error(self, calls: list[ToolCall]) -> str | None:
        """桌面动作依赖新观察；整批拒绝，避免先执行一部分后再拒绝。"""
        if len({call.id for call in calls}) != len(calls):
            return "本轮工具调用 ID 重复；本批所有调用均未执行。"
        if len(calls) > 1:
            for call in calls:
                try:
                    if self.registry.get(call.name).requires_single_call:
                        return "本轮包含需观察后再决策的工具，请每轮只提交一个工具调用；本批所有调用均未执行。"
                except KeyError:
                    continue
        return None

    @staticmethod
    def reject(call: ToolCall, code: str, message: str, *, exception_type: str | None = None) -> Observation:
        info = ErrorInfo(code, "validation", message, exception_type=exception_type)
        return Observation(call.id, call.name, error=message, status="rejected", error_info=info)

    def execute(self, call: ToolCall, *, on_execution_start: Callable[[], None] | None = None) -> Observation:
        """失败也返回匹配的调用 ID，让模型可以在后续轮次修正。"""
        try:
            tool = self.registry.get(call.name)
        except KeyError:
            return self.reject(call, "UNKNOWN_TOOL", f"未注册的工具：{call.name}")
        try:
            def invalid_constant(_value):
                raise ValueError("JSON 不允许非有限数字。")
            arguments = json.loads(call.arguments, parse_constant=invalid_constant)
            # 包括指数溢出产生的 Infinity；不回显原始值，避免日志泄漏敏感参数。
            json.dumps(arguments, allow_nan=False)
        except (ValueError, TypeError, OverflowError) as error:
            return self.reject(call, "INVALID_JSON", "工具参数必须是有效 JSON，且数字必须有限。",
                               exception_type=type(error).__name__)
        if not isinstance(arguments, dict):
            return self.reject(call, "INVALID_ARGUMENTS", "工具参数必须是 JSON 对象。")
        try:
            tool.validator.validate(arguments)
        except ValidationError as error:
            path = "$" + "".join(f"[{part}]" if isinstance(part, int) else f".{part}" for part in error.absolute_path)
            return self.reject(call, "INVALID_ARGUMENTS", f"参数 {path} 不符合 {error.validator} 约束：{error.validator_value}",
                               exception_type="ValidationError")
        except Exception as error:
            return self.reject(call, "INVALID_SCHEMA", "工具参数 Schema 无法完成校验。",
                               exception_type=type(error).__name__)

        # 日志回调在执行边界之外：严格日志失败时不能调用处理函数，也不能转成可重试工具错误。
        if on_execution_start is not None:
            on_execution_start()
        try:
            result = tool.handler(self.environment, **arguments)
        except (AgentCancelled, KeyboardInterrupt):
            raise
        except ToolFailure as error:
            info = replace(error.info, exception_type=error.info.exception_type or type(error.__cause__ or error).__name__)
            status = "rejected" if info.phase == "validation" and info.side_effects == "none" else "failed"
            return Observation(call.id, call.name, error=f"{type(error).__name__}: {error}",
                               status=status, executed=True, error_info=info)
        except Exception as error:
            info = ErrorInfo("EXECUTION_FAILED", "execution", str(error), "possible", type(error).__name__)
            return Observation(call.id, call.name, error=f"{type(error).__name__}: {error}",
                               status="failed", executed=True, error_info=info)
        try:
            images = []
            if isinstance(result, ToolResult):
                images, result = result.images, result.data
            if not isinstance(images, list) or any(not isinstance(image, ImageContent) or
                    not isinstance(image.data, str) or not isinstance(image.mime_type, str) for image in images):
                raise TypeError("工具图片必须是 ImageContent 列表。")
            # 反馈必须能发送给模型；不能序列化的返回值也作为工具错误处理。
            json.dumps(result, ensure_ascii=False, allow_nan=False)
            return Observation(call.id, call.name, result=result, images=images, executed=True)
        except Exception as error:
            info = ErrorInfo("INVALID_RESULT", "result", str(error), "possible", type(error).__name__)
            return Observation(call.id, call.name, error=f"{type(error).__name__}: {error}",
                               status="failed", executed=True, error_info=info)
