"""任务状态：记录消息、执行反馈和结束原因，不负责执行操作。"""

from dataclasses import dataclass, field
from typing import Literal

from contracts import ErrorInfo, Message, Observation


@dataclass
class AgentState:
    """每次 run 创建新状态；执行环境由 Agent 实例持有，可继续保留文件。"""

    task: str
    messages: list[Message] = field(default_factory=list)
    observations: list[Observation] = field(default_factory=list)
    turn: int = 0
    status: Literal["running", "completed", "limit_reached", "failed", "cancelled"] = "running"
    answer: str | None = None
    error: str | None = None
    error_info: ErrorInfo | None = None
    stop_reason: str | None = None
    trace_errors: list[str] = field(default_factory=list)
    run_id: str | None = None
