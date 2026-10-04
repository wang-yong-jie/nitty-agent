"""工具注册与执行：定义能力，找到处理函数，将执行结果转成 Observation。"""

import json
from dataclasses import dataclass
from typing import Callable

from contracts import AgentCancelled, Environment, Observation, ToolCall, ToolResult, ToolSpec


@dataclass
class RegisteredTool:
    """描述与实现绑定在一起，避免两个列表出现名称或参数不一致。"""

    spec: ToolSpec
    handler: Callable
    requires_single_call: bool = False
    sensitive_parameters: tuple[str, ...] = ()


class ToolRegistry:
    """每个 Agent 可以使用不同注册表；注册本身不执行任何操作。"""

    def __init__(self):
        self._tools: dict[str, RegisteredTool] = {}

    def register(
        self, name: str, description: str, properties: dict, required: list[str], handler: Callable,
        *, requires_single_call: bool = False, instructions: str = "",
        sensitive_parameters: tuple[str, ...] = (),
    ) -> None:
        """注册能力及使用规则；处理函数接收 environment 和模型给出的参数。"""
        if name in self._tools:
            raise ValueError(f"工具已注册：{name}")
        spec = ToolSpec(name, description, {
            "type": "object", "properties": properties, "required": required,
            "additionalProperties": False,
        }, instructions=instructions)
        self._tools[name] = RegisteredTool(spec, handler, requires_single_call, sensitive_parameters)

    def specs(self) -> list[ToolSpec]:
        """只把描述交给模型，真实执行函数不会发送给模型。"""
        return [tool.spec for tool in self._tools.values()]

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
        if len(calls) > 1:
            for call in calls:
                try:
                    if self.registry.get(call.name).requires_single_call:
                        return "本轮包含需观察后再决策的工具，请每轮只提交一个工具调用；本批所有调用均未执行。"
                except KeyError:
                    continue
        return None

    def execute(self, call: ToolCall) -> Observation:
        """失败也返回匹配的调用 ID，让模型可以在后续轮次修正。"""
        try:
            arguments = json.loads(call.arguments)
            if not isinstance(arguments, dict):
                raise ValueError("工具参数必须是 JSON 对象。")
            tool = self.registry.get(call.name)
            result = tool.handler(self.environment, **arguments)
            images = []
            if isinstance(result, ToolResult):
                images, result = result.images, result.data
            # 反馈必须能发送给模型；不能序列化的返回值也作为工具错误处理。
            json.dumps(result, ensure_ascii=False)
            return Observation(call.id, call.name, result=result, images=images)
        except AgentCancelled:
            raise
        except Exception as error:
            # 这里只捕获工具异常；模型/API 错误由 Runtime 处理并向调用方抛出。
            return Observation(call.id, call.name, error=f"{type(error).__name__}: {error}")
