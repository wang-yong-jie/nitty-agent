"""模块间的公共接口：Runtime 不需要认识厂商 SDK 或具体执行环境。"""

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol


class AgentCancelled(Exception):
    """用户中止；必须穿过工具错误边界，不能让模型重试。"""


@dataclass(frozen=True)
class ErrorInfo:
    """稳定的错误分类；possible 表示不能保证操作没有产生副作用。"""

    code: str
    phase: str
    message: str
    side_effects: Literal["none", "possible"] = "none"
    exception_type: str | None = None


class ToolFailure(Exception):
    """适配器可显式说明业务校验或部分执行失败，避免按异常文本猜测。"""

    def __init__(self, info: ErrorInfo):
        super().__init__(info.message)
        self.info = info


class ToolRejected(ToolFailure, ValueError):
    """业务校验拒绝；处理函数可能已进入，但尚未发送输入。"""

    def __init__(self, code: str, message: str):
        super().__init__(ErrorInfo(code, "validation", message))


class TraceWriteError(RuntimeError):
    """严格日志模式下停止任务；原始任务异常优先于日志异常。"""


class ToolExecutionError(ToolFailure, RuntimeError):
    """已进入处理函数的具体失败；适配器负责声明已知的副作用范围。"""

    def __init__(self, code: str, message: str, *, phase: str = "execution", side_effects: Literal["none", "possible"] = "possible"):
        super().__init__(ErrorInfo(code, phase, message, side_effects))


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
class ModelMetadata:
    """响应诊断信息，不包含提示词、推理、凭证或完整厂商响应。"""

    provider: str
    model: str
    finish_reason: str | None = None
    request_id: str | None = None
    response_id: str | None = None
    usage: dict[str, int] = field(default_factory=dict)


class ModelResponseError(RuntimeError):
    """请求已有响应但不能作为有效决策使用；保留可用的响应诊断信息。"""

    def __init__(self, code: str, message: str, metadata: ModelMetadata):
        super().__init__(message)
        self.code, self.metadata = code, metadata


@dataclass
class ModelReply:
    """模型的一次决策：回答文本、工具调用，或两者兼有。"""

    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    metadata: ModelMetadata | None = None


@dataclass
class Observation:
    """调用反馈；executed 表示进入处理函数调用，不保证业务成功或已产生副作用。"""

    tool_call_id: str
    tool_name: str
    result: Any = None
    error: str | None = None
    images: list[ImageContent] = field(default_factory=list)
    status: Literal["rejected", "succeeded", "failed", "cancelled"] = "succeeded"
    executed: bool = False
    error_info: ErrorInfo | None = None


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


class ApplicationCatalog(Protocol):
    """只读应用发现；不负责启动、截图或任务决策。"""

    def find(self, name: str) -> dict:
        ...


class EventSink(Protocol):
    """接收已经脱敏的运行事件；Runtime 不管理日志文件。"""

    def emit(self, event: dict) -> None:
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
