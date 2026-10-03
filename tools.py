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


class ToolRegistry:
    """每个 Agent 可以使用不同注册表；注册本身不执行任何操作。"""

    def __init__(self):
        self._tools: dict[str, RegisteredTool] = {}

    def register(
        self, name: str, description: str, properties: dict, required: list[str], handler: Callable,
        *, requires_single_call: bool = False, instructions: str = "",
    ) -> None:
        """注册能力及使用规则；处理函数接收 environment 和模型给出的参数。"""
        if name in self._tools:
            raise ValueError(f"工具已注册：{name}")
        spec = ToolSpec(name, description, {
            "type": "object", "properties": properties, "required": required,
            "additionalProperties": False,
        }, instructions=instructions)
        self._tools[name] = RegisteredTool(spec, handler, requires_single_call)

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


def create_default_registry() -> ToolRegistry:
    """创建默认的三个工具；委托环境执行，没有本机路径或 Shell 逻辑。"""
    registry = ToolRegistry()
    registry.register(
        "exec_command", "执行 Shell 命令：创建目录、列目录、运行程序和测试；Shell 由环境信息指定。",
        {
            "cmd": {"type": "string", "description": "需要执行的命令，支持多行"},
            "workdir": {"type": "string", "description": "可选的已有执行目录；省略时使用默认工作目录"},
            "timeout": {"type": "integer", "description": "超时秒数，默认 30，范围 1 到 300", "minimum": 1, "maximum": 300},
        }, ["cmd"],
        lambda environment, **arguments: environment.exec_command(**arguments),
        instructions=(
            "目录创建、列目录、运行程序和测试使用 exec_command。"
            "根据环境信息中的 shell 编写命令；Windows 使用 PowerShell 语法。"
            "执行位置和 Python 解释器以环境信息为准，安装依赖使用 python -m pip。"
            "每次调用都是新 Shell，cd 和变量不会跨调用保留；需要时传入 workdir。"
            "workdir 只影响该次调用，不改变文件工具的默认目录。"
            "检查退出码、timed_out 和截断标记，未成功时不要声称操作完成。"
            "工具已分别捕获 stdout 和 stderr，PowerShell 运行 Python 时无需用 2>&1 合并输出。"
        ),
    )
    registry.register(
        "read_file", "读取 UTF-8 文本文件，最多返回 20000 个字符并标记截断。",
        {"path": {"type": "string", "description": "绝对路径或相对于工作目录的路径"}}, ["path"],
        lambda environment, **arguments: environment.read_file(**arguments),
        instructions="读取文本使用 read_file；读取被截断时不要根据部分内容覆盖原文件。",
    )
    registry.register(
        "write_file", "创建或覆盖 UTF-8 文本文件。编辑已有文件前先读取内容。",
        {
            "path": {"type": "string", "description": "绝对路径或相对于工作目录的路径"},
            "content": {"type": "string", "description": "要写入的完整文件内容"},
        }, ["path", "content"],
        lambda environment, **arguments: environment.write_file(**arguments),
        instructions="编辑已有文件前先读取，再用 write_file 写入完整内容；写完代码后按需运行验证。",
    )
    return registry
