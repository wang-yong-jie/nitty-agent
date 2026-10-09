"""任务状态：记录消息、执行反馈和结束原因，不负责执行操作。"""

from dataclasses import dataclass, field
from typing import Literal

from contracts import ErrorInfo, Message, Observation
from core.task_memory import TaskMemory


@dataclass
class AgentState:
    """每次 run 创建新执行状态；消息包含当前会话历史，Observation 仅属于本次执行。"""

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
    session_id: str | None = None
    omitted_messages: int = 0
    verification: dict | None = None
    memory: TaskMemory | None = None
    context_summary: str = ""
    active_skills: list[dict] = field(default_factory=list)
    skill_resources: list[dict] = field(default_factory=list)
    skill_context: str = ""  # 每轮由 SkillManager 重建，不作为普通历史保存。
