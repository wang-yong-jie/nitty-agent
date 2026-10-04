"""Agent Runtime：协调模型、上下文、状态和工具，执行单 Agent Loop。"""

import json
import time
from dataclasses import asdict
from typing import Callable

from core.context import ContextBuilder
from contracts import (
    AgentCancelled, Environment, ErrorInfo, EventSink, Message, ModelAdapter,
    ModelMetadata, ModelResponseError, Observation, TraceWriteError,
)
from core.state import AgentState
from core.tooling import ToolExecutor, ToolRegistry
from tracing import RunLogger


class AgentRuntime:
    """依赖由外部传入；循环中没有厂商 SDK 或本机操作。"""

    def __init__(
        self, model: ModelAdapter, registry: ToolRegistry, environment: Environment,
        context: ContextBuilder | None = None, max_turns: int = 10, verbose: bool = True,
        cancel_check: Callable[[], None] | None = None,
        trace: EventSink | None = None,
        *, trace_strict: bool = False, run_config: dict | None = None,
    ):
        if type(max_turns) is not int or max_turns < 1:
            raise ValueError("max_turns 必须是正整数。")
        if trace_strict and trace is None:
            raise ValueError("trace_strict=True 时必须提供日志接收器。")
        self.model = model
        self.registry = registry
        self.environment = environment
        self.context = context if context is not None else ContextBuilder()
        self.executor = ToolExecutor(registry, environment)
        self.max_turns = max_turns
        self.verbose = verbose
        self.cancel_check = cancel_check or (lambda: None)
        self.state: AgentState | None = None
        self.logger = RunLogger(verbose, trace, strict=trace_strict)
        self.run_config = dict(run_config or {})

    @staticmethod
    def _error_info(error: Exception, phase: str) -> ErrorInfo:
        code = "TRACE_WRITE_FAILED" if isinstance(error, TraceWriteError) else (
            error.code if isinstance(error, ModelResponseError) else f"{phase.upper()}_FAILED"
        )
        return ErrorInfo(code, "logging" if isinstance(error, TraceWriteError) else phase,
                         str(error), exception_type=type(error).__name__)

    @staticmethod
    def _record(state: AgentState, observation: Observation) -> None:
        state.observations.append(observation)
        payload = {"error": observation.error, "error_info": asdict(observation.error_info)} if observation.error_info else (
            {"error": observation.error} if observation.error is not None else {"result": observation.result}
        )
        state.messages.append(Message(
            "tool", json.dumps(payload, ensure_ascii=False, allow_nan=False), tool_call_id=observation.tool_call_id,
            is_error=observation.error is not None, images=observation.images,
        ))

    def run(self, task: str) -> AgentState:
        """每次任务独立维护历史；结束后保留状态供调用方检查。"""
        if not task.strip():
            raise ValueError("任务不能为空。")
        state = AgentState(task=task, messages=[Message(role="user", content=task)])
        self.state = state
        run_started = time.perf_counter()
        phase = "setup"
        try:
            # 只记录调用方显式提供的非敏感配置，不遍历模型对象或保存用户任务。
            self.logger.start(config=self.run_config, max_turns=self.max_turns,
                              tools=[spec.name for spec in self.registry.specs()],
                              model={"adapter": type(self.model).__name__})
            getter = getattr(self.model, "get_info", None)
            if callable(getter):
                self.logger.emit("model_configured", model=getter())
            for turn in range(1, self.max_turns + 1):
                phase = "cancellation"
                self.cancel_check()
                state.turn = turn
                if self.verbose:
                    print(f"\n[第 {turn} 轮] 调用模型")
                specs = self.registry.specs()
                # Context 用最新环境快照和历史组装输入，Adapter 做协议转换。
                phase = "context"
                environment_info = self.environment.get_info()
                messages = self.context.build_messages(state, environment_info, specs)
                self.logger.emit("context_built", turn=turn, environment=environment_info,
                                 message_count=len(messages), image_count=sum(len(message.images) for message in messages))
                self.logger.emit("model_started", turn=turn)
                started = time.perf_counter()
                phase = "model"
                try:
                    reply = self.model.generate(messages, specs)
                    if not reply.tool_calls and not (reply.content and reply.content.strip()):
                        raise RuntimeError("模型没有返回工具调用或有效回答。")
                    if reply.metadata is not None and not isinstance(reply.metadata, ModelMetadata):
                        raise TypeError("模型响应的 metadata 必须是 ModelMetadata。")
                    response_metadata = asdict(reply.metadata) if reply.metadata else None
                except (AgentCancelled, KeyboardInterrupt) as error:
                    self.logger.emit("model_cancelled", fatal=False, turn=turn, exception_type=type(error).__name__,
                                     duration_ms=round((time.perf_counter() - started) * 1000, 2))
                    raise
                except Exception as error:
                    state.error_info = self._error_info(error, phase)
                    metadata = getattr(error, "metadata", None)
                    self.logger.emit("model_failed", fatal=False, turn=turn, error_info=asdict(state.error_info),
                                     duration_ms=round((time.perf_counter() - started) * 1000, 2),
                                     request_id=getattr(error, "request_id", None), status_code=getattr(error, "status_code", None),
                                     metadata=asdict(metadata) if isinstance(metadata, ModelMetadata) else None)
                    raise
                self.logger.emit("model_finished", turn=turn, duration_ms=round((time.perf_counter() - started) * 1000, 2),
                                 tool_call_count=len(reply.tool_calls), metadata=response_metadata)
                phase = "cancellation"
                self.cancel_check()
                state.messages.append(Message("assistant", reply.content, reply.tool_calls))
                if not reply.tool_calls:
                    state.answer = reply.content
                    state.status = "completed"
                    state.stop_reason = "model_answer"
                    break

                # 先记录整组调用，再逐一执行并回传，每个结果都与调用 ID 配对。
                batch_error = self.executor.batch_error(reply.tool_calls)
                for call in reply.tool_calls:
                    phase = "cancellation"
                    self.cancel_check()
                    try:
                        sensitive = self.registry.get(call.name).sensitive_parameters
                    except KeyError:
                        sensitive = ()
                    self.logger.tool_started(call, turn, sensitive)
                    started = time.perf_counter()
                    executed = False

                    def execution_started():
                        nonlocal executed
                        self.logger.emit("tool_execution_started", turn=turn, call_id=call.id, tool=call.name,
                                         validation="passed")
                        executed = True

                    phase = "tool"
                    try:
                        observation = (
                            self.executor.reject(call, "BATCH_REJECTED", batch_error)
                            if batch_error else self.executor.execute(call, on_execution_start=execution_started)
                        )
                    except (AgentCancelled, KeyboardInterrupt) as error:
                        info = ErrorInfo("CANCELLED", "execution" if executed else "validation", str(error) or "用户中止任务。",
                                         "possible" if executed else "none", type(error).__name__)
                        # 取消结果留在状态中供调试，不再发送给模型。
                        state.observations.append(Observation(call.id, call.name, error=info.message, status="cancelled",
                                                              executed=executed, error_info=info))
                        state.error_info = info
                        self.logger.emit("tool_cancelled", fatal=False, turn=turn, call_id=call.id, tool=call.name,
                                         status="cancelled", executed=executed, error_info=asdict(info),
                                         duration_ms=round((time.perf_counter() - started) * 1000, 2))
                        raise
                    duration = time.perf_counter() - started
                    self._record(state, observation)
                    self.logger.tool_finished(observation, turn, duration)
                # Observation 进入历史，下一轮由模型根据真实结果重新决策。
            else:
                phase = "runtime"
                state.status = "limit_reached"
                state.stop_reason = "turn_limit"
                state.error_info = ErrorInfo("TURN_LIMIT_REACHED", phase, f"已达到 {self.max_turns} 轮上限，任务尚未完成。")
                raise RuntimeError(state.error_info.message)
        except (AgentCancelled, KeyboardInterrupt) as error:
            state.status = "cancelled"
            state.error = str(error) or "用户中止任务。"
            state.stop_reason = "user_cancelled"
            if state.error_info is None:
                state.error_info = ErrorInfo("CANCELLED", phase, state.error, exception_type=type(error).__name__)
            raise
        except Exception as error:
            if state.status == "running":
                state.status = "failed"
            state.error = str(error)
            if state.error_info is None:
                state.error_info = self._error_info(error, phase)
            state.stop_reason = state.stop_reason or ("trace_failure" if isinstance(error, TraceWriteError) else "run_failure")
            self.logger.emit("run_failed", fatal=False, turn=state.turn, error_info=asdict(state.error_info))
            raise  # 不把 API 错误或轮次耗尽伪装成任务完成。
        finally:
            try:
                # 已有任务异常时，日志错误不能覆盖它；正常结束的严格写入失败仍需停止并更新状态。
                self.logger.emit("run_finished", fatal=state.error is None, status=state.status, turn=state.turn,
                                 error=state.error, stop_reason=state.stop_reason,
                                 error_info=asdict(state.error_info) if state.error_info else None,
                                 duration_ms=round((time.perf_counter() - run_started) * 1000, 2),
                                 trace_error_count=len(self.logger.errors))
            except TraceWriteError as error:
                state.status, state.stop_reason, state.error = "failed", "trace_failure", str(error)
                state.error_info = self._error_info(error, "logging")
                raise
            finally:
                state.trace_errors = list(self.logger.errors)
                state.run_id = self.logger.run_id
        return state
