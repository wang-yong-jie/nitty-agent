"""Agent Runtime：协调模型、上下文、状态和工具，执行单 Agent Loop。"""

import json
from typing import Callable

from context import ContextBuilder
from contracts import AgentCancelled, Environment, Message, ModelAdapter, Observation
from state import AgentState
from tools import ToolExecutor, ToolRegistry


class AgentRuntime:
    """依赖由外部传入；循环中没有厂商 SDK 或本机操作。"""

    def __init__(
        self, model: ModelAdapter, registry: ToolRegistry, environment: Environment,
        context: ContextBuilder | None = None, max_turns: int = 10, verbose: bool = True,
        cancel_check: Callable[[], None] | None = None,
    ):
        if type(max_turns) is not int or max_turns < 1:
            raise ValueError("max_turns 必须是正整数。")
        self.model = model
        self.registry = registry
        self.environment = environment
        self.context = context if context is not None else ContextBuilder()
        self.executor = ToolExecutor(registry, environment)
        self.max_turns = max_turns
        self.verbose = verbose
        self.cancel_check = cancel_check or (lambda: None)
        self.state: AgentState | None = None

    def run(self, task: str) -> AgentState:
        """每次任务独立维护历史；结束后保留状态供调用方检查。"""
        if not task.strip():
            raise ValueError("任务不能为空。")
        state = AgentState(task=task, messages=[Message(role="user", content=task)])
        self.state = state
        try:
            for turn in range(1, self.max_turns + 1):
                self.cancel_check()
                state.turn = turn
                if self.verbose:
                    print(f"\n[第 {turn} 轮] 调用模型")
                specs = self.registry.specs()
                # Context 用最新环境快照和历史组装输入，Adapter 做协议转换。
                messages = self.context.build_messages(state, self.environment.get_info(), specs)
                reply = self.model.generate(messages, specs)
                self.cancel_check()
                if not reply.tool_calls and not reply.content:
                    raise RuntimeError("模型没有返回工具调用或有效回答。")
                state.messages.append(Message("assistant", reply.content, reply.tool_calls))
                if not reply.tool_calls:
                    state.answer = reply.content
                    state.status = "completed"
                    return state

                # 先记录整组调用，再逐一执行并回传，每个结果都与调用 ID 配对。
                batch_error = self.executor.batch_error(reply.tool_calls)
                for call in reply.tool_calls:
                    self.cancel_check()
                    if self.verbose:
                        print(f"[调用工具] {call.name}")
                    observation = (
                        Observation(call.id, call.name, error=batch_error)
                        if batch_error else self.executor.execute(call)
                    )
                    state.observations.append(observation)
                    payload = {"error": observation.error} if observation.error else {"result": observation.result}
                    content = json.dumps(payload, ensure_ascii=False)
                    state.messages.append(Message(
                        "tool", content, tool_call_id=call.id, is_error=observation.error is not None,
                        images=observation.images,
                    ))
                    if self.verbose:
                        print(f"[工具结果] {content}")
                # Observation 进入历史，下一轮由模型根据真实结果重新决策。
            state.status = "limit_reached"
            raise RuntimeError(f"已达到 {self.max_turns} 轮上限，任务尚未完成。")
        except (AgentCancelled, KeyboardInterrupt) as error:
            state.status = "cancelled"
            state.error = str(error) or "用户中止任务。"
            raise
        except Exception as error:
            if state.status == "running":
                state.status = "failed"
            state.error = str(error)
            raise  # 不把 API 错误或轮次耗尽伪装成任务完成。
