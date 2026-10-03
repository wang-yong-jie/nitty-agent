"""模块间的公共接口：Runtime 不需要认识厂商 SDK 或具体执行环境。"""

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class ToolSpec:
    """工具能力与参数定义；由模型适配器转成厂商要求的格式。"""

    name: str
    description: str
    parameters: dict


@dataclass
class ToolCall:
    """模型发出的工具调用；保留原始 JSON，方便反馈参数解析错误。"""

    id: str
    name: str
    arguments: str


@dataclass
class Message:
    """统一消息表示，避免在状态和循环里依赖 OpenAI/Claude 的类型。"""

    role: str
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    is_error: bool = False


@dataclass
class ModelReply:
    """模型的一次决策：回答文本、工具调用，或两者兼有。"""

    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)


@dataclass
class Observation:
    """执行工具后的真实反馈；error 为空表示工具函数正常返回。"""

    tool_call_id: str
    tool_name: str
    result: Any = None
    error: str | None = None


class ModelAdapter(Protocol):
    """替换模型只需实现 generate，不需要修改 Runtime。"""

    def generate(self, messages: list[Message], tools: list[ToolSpec]) -> ModelReply:
        ...


class Environment(Protocol):
    """默认工具依赖的环境能力；本机、容器或远程环境均可实现此接口。"""

    def get_info(self) -> dict:
        """提供真实环境信息给上下文模块，不触发执行操作。"""
        ...

    def exec_command(self, cmd: str, workdir: str | None = None, timeout: int = 30) -> dict:
        ...

    def read_file(self, path: str) -> dict:
        ...

    def write_file(self, path: str, content: str) -> str:
        ...
