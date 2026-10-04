"""Agent API：供命令行、PyCharm 或其他 Python 程序调用的统一入口。"""

from typing import Callable

from core.context import ContextBuilder
from contracts import Environment, EventSink, ModelAdapter
from core.runtime import AgentRuntime
from core.state import AgentState
from core.tooling import ToolRegistry


class Agent:
    """单 Agent；通过构造参数替换 Model、Tools、Environment 或 Context。"""

    def __init__(
        self, model: ModelAdapter, registry: ToolRegistry, environment: Environment,
        context: ContextBuilder | None = None, max_turns: int = 10, verbose: bool = True,
        cancel_check: Callable[[], None] | None = None,
        trace: EventSink | None = None,
    ):
        self.runtime = AgentRuntime(model, registry, environment, context, max_turns, verbose, cancel_check, trace)

    def run(self, task: str) -> str:
        """提交一个任务，返回最终回答；执行失败时抛出异常。"""
        state = self.runtime.run(task)
        return state.answer or ""

    @property
    def last_state(self) -> AgentState | None:
        """检查最近任务的轮次、历史、Observation 和结束原因。"""
        return self.runtime.state
