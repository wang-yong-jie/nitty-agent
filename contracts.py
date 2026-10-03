"""模块间的公共接口：Runtime 不需要认识厂商 SDK 或具体执行环境。"""

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol


class AgentCancelled(Exception):
    """用户中止；必须穿过工具错误边界，不能让模型重试。"""


@dataclass(frozen=True)
class ImageContent:
    """内存中的图片附件；不将编码内容写入日志或普通 JSON 工具反馈。"""

    data: str = field(repr=False)
    mime_type: str = "image/png"


@dataclass
class ToolResult:
    """带图片的工具返回值；普通工具仍可直接返回 JSON 可序列化对象。"""

    data: Any
    images: list[ImageContent] = field(default_factory=list)


@dataclass
class ToolSpec:
    """工具定义；instructions 由 Context 收集，其余字段由适配器转换。"""

    name: str
    description: str
    parameters: dict
    instructions: str = ""


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
    images: list[ImageContent] = field(default_factory=list)


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
    images: list[ImageContent] = field(default_factory=list)


class ModelAdapter(Protocol):
    """替换模型只需实现 generate，不需要修改 Runtime。"""

    def generate(self, messages: list[Message], tools: list[ToolSpec]) -> ModelReply:
        ...


@dataclass(frozen=True)
class VisualTarget:
    """视觉模型定位到的目标；坐标相对于发送给它的图片。"""

    label: str
    x: int
    y: int


@dataclass
class VisionAnswer:
    """answered 仅表示问题可回答；任务是否成功需阅读 answer 中的证据。"""

    status: Literal["answered", "not_found", "ambiguous", "uncertain"]
    answer: str
    targets: list[VisualTarget] = field(default_factory=list)


class VisionAdapter(Protocol):
    """按需回答截图问题；不执行动作，不依赖主模型或具体桌面实现。"""

    def answer(self, question: str, image: ImageContent, *, width: int, height: int) -> VisionAnswer:
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


class DesktopController(Protocol):
    """可选桌面能力；文件/Shell 环境不必实现此接口。"""

    def get_info(self) -> dict:
        ...

    def screenshot(self) -> ToolResult:
        ...

    def validate_frame(self, frame_id: str) -> None:
        """无输入地检查截图是否仍有效；失效或取消时抛出异常。"""
        ...

    def perform(self, action: str, **arguments) -> ToolResult:
        ...
