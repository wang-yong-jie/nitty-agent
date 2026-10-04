"""Agent API：供命令行、PyCharm 或其他 Python 程序调用的统一入口。"""

from typing import Callable

from core.context import ContextBuilder
from contracts import Environment, EventSink, ModelAdapter
from core.runtime import AgentRuntime
from core.state import AgentState
from core.session import Session
from core.tooling import ToolRegistry


class Agent:
    """单 Agent；通过构造参数替换 Model、Tools、Environment 或 Context。"""

    def __init__(
        self, model: ModelAdapter, registry: ToolRegistry, environment: Environment,
        context: ContextBuilder | None = None, max_turns: int = 10, verbose: bool = True,
        cancel_check: Callable[[], None] | None = None,
        trace: EventSink | None = None,
        trace_strict: bool = False,
        run_config: dict | None = None,
        session: Session | None = None,
        on_session_update: Callable[[Session], None] | None = None,
    ):
        self.runtime = AgentRuntime(model, registry, environment, context, max_turns, verbose, cancel_check, trace,
                                    trace_strict=trace_strict, run_config=run_config)
        self.runtime.session = session if session is not None else Session()
        self.runtime.on_session_update = on_session_update

    @property
    def session(self) -> Session:
        return self.runtime.session

    def new_session(self) -> Session:
        """显式开始新会话；不删除已有文件或先前持有的 Session 对象。"""
        self.runtime.session = Session()
        return self.runtime.session

    def run(self, task: str) -> str:
        """提交一个任务，返回最终回答；执行失败时抛出异常。"""
        state = self.runtime.run(task)
        return state.answer or ""

    @property
    def last_state(self) -> AgentState | None:
        """检查最近任务的轮次、历史、Observation 和结束原因。"""
        return self.runtime.state
