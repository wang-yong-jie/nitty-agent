"""Agent Runtime：协调模型、上下文、状态和工具，执行单 Agent Loop。"""

import json
import time
from dataclasses import asdict
from typing import Callable

from core.context import ContextBuilder
from contracts import (
    AgentCancelled, Environment, ErrorInfo, EventSink, Message, ModelAdapter,
    ModelMetadata, ModelResponseError, Observation, ToolFailure, TraceWriteError,
)
from core.state import AgentState
from core.session import Session
from core.tooling import ToolExecutor, ToolRegistry
from core.task_memory import TaskMemory
from core.task_controller import TaskController
from core.checkpoint import snapshot, restore, CheckpointWriteError
from tracing import RunLogger


class AgentRuntime:
    """依赖由外部传入；循环中没有厂商 SDK 或本机操作。"""

    def __init__(
        self, model: ModelAdapter, registry: ToolRegistry, environment: Environment,
        context: ContextBuilder | None = None, max_turns: int = 10, verbose: bool = True,
        cancel_check: Callable[[], None] | None = None,
        trace: EventSink | None = None,
        *, trace_strict: bool = False, run_config: dict | None = None,
        completion_check=None, on_run_start=None, on_observation=None,
        long_horizon=False, task_verifier=None, desktop_verifier=None, compactor=None,
        on_checkpoint=None, snapshot_extension=None, restore_extension=None,
        skills=None,
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
        self.session = Session()
        self.on_session_update = None
        self.completion_check = completion_check
        self.on_run_start = on_run_start
        self.on_observation = on_observation
        self.long_horizon = long_horizon
        self.compactor = compactor
        self.on_checkpoint = on_checkpoint
        self.snapshot_extension, self.restore_extension = snapshot_extension, restore_extension
        self.last_checkpoint = None
        self.skills = skills
        self.checkpoint_phase = "setup"
        self.controller = TaskController(self, task_verifier, desktop_verifier)
        self.control_registered = False
        if long_horizon:
            self.controller.register()
            self.control_registered = True

    def _checkpoint(self, state: AgentState) -> None:
        self.session.omitted_messages = max(self.session.omitted_messages, state.omitted_messages)
        self.session.update(state.messages)
        directory = getattr(self.environment, "working_directory", None)
        if directory is not None:
            self.session.working_directory = str(directory)
        if self.on_session_update is not None:
            self.on_session_update(self.session)
        self._save_checkpoint(state)

    def _save_checkpoint(self, state):
        if state is self.state and state.memory is not None:
            state.run_id = self.logger.run_id
            self.last_checkpoint = snapshot(state, self.session, self.checkpoint_phase,
                self.snapshot_extension() if self.snapshot_extension else {})
            if self.on_checkpoint is not None:
                try:
                    self.on_checkpoint(self.last_checkpoint)
                except (AgentCancelled, KeyboardInterrupt):
                    raise
                except Exception as error:
                    raise CheckpointWriteError(f"执行快照保存失败，已停止后续操作：{error}") from error

    @staticmethod
    def _error_info(error: Exception, phase: str) -> ErrorInfo:
        if isinstance(error, CheckpointWriteError):
            return ErrorInfo("CHECKPOINT_WRITE_FAILED", "checkpoint", str(error), "possible", type(error).__name__)
        if isinstance(error, ToolFailure):
            return error.info
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

    def run(self, task: str, *, checkpoint=None) -> AgentState:
        """执行状态属于本次任务；历史继承会话，结束后保留状态供检查。"""
        if not task.strip():
            raise ValueError("任务不能为空。")
        if checkpoint is not None:
            state = restore(checkpoint)
            if task != state.task:
                raise ValueError("恢复时不能更改任务目标，请新建任务。")
            self.session = Session.from_dict(checkpoint["session"])
            self.long_horizon = True
            if not self.control_registered:
                self.controller.register()
                self.control_registered = True
            state.messages.append(Message("user", "[自动恢复观察，不是新的用户指令] 从已保存的任务状态继续，"
                "已完成步骤先核实产物；未决动作不得重放。所有桌面画面均已失效，必须重新截图。"))
        else:
            state = AgentState(task=task, session_id=self.session.id, omitted_messages=self.session.omitted_messages,
                               messages=[*self.session.messages, Message(role="user", content=task)], memory=TaskMemory(task))
        self.session.compactor = self.compactor
        self.state = state
        first_turn = state.turn + 1
        run_started = time.perf_counter()
        phase = "setup"
        completion_checks = 0
        try:
            if self.on_run_start is not None:
                self.on_run_start()
            if checkpoint is not None and self.restore_extension is not None:
                self.restore_extension(checkpoint.get("extensions", {}))
            self.checkpoint_phase = "ready"
            self._checkpoint(state)
            # 只记录调用方显式提供的非敏感配置，不遍历模型对象或保存用户任务。
            self.logger.start(config=self.run_config, max_turns=self.max_turns,
                              tools=[spec.name for spec in self.registry.specs()],
                              model={"adapter": type(self.model).__name__})
            phase = "skills"
            if self.skills is not None:
                self.skills.prepare(state, self.registry, restoring=checkpoint is not None, emit=self.logger.emit)
                self._checkpoint(state)
            elif state.active_skills or state.skill_resources:
                raise ValueError("此任务包含 Skill 激活记录，但当前 Runtime 没有配置 SkillManager。")
            getter = getattr(self.model, "get_info", None)
            if callable(getter):
                self.logger.emit("model_configured", model=getter())
            for turn in range(first_turn, first_turn + self.max_turns):
                phase = "cancellation"
                self.cancel_check()
                state.turn = turn
                if self.long_horizon:
                    state.messages = self.session.compact(state.messages)
                state.context_summary = self.session.summary
                state.omitted_messages = self.session.omitted_messages
                if self.verbose:
                    print(f"\n[第 {turn} 轮] 调用模型")
                specs = self.registry.specs()
                # Context 用最新环境快照和历史组装输入，Adapter 做协议转换。
                phase = "context"
                environment_info = self.environment.get_info()
                state.skill_context = self.skills.context() if self.skills is not None else ""
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
                    if self.completion_check is not None or self.long_horizon:
                        phase = "verification"
                        self.logger.emit("verification_started", turn=turn)
                        state.verification = self.controller.finish(reply.content) if self.long_horizon else self.completion_check(task)
                        if state.verification and state.verification["status"] == "achieved" and self.completion_check is not None and self.long_horizon:
                            additional = self.completion_check(task)
                            if additional is not None:
                                state.verification = additional
                        self.logger.emit("verification_finished", turn=turn, verification=state.verification)
                        self.cancel_check()
                        if state.verification and state.verification["status"] != "achieved":
                            completion_checks += 1
                            state.memory.verification_attempts += 1
                            if completion_checks >= 2:
                                state.error_info = ErrorInfo("TARGET_UNVERIFIED", phase,
                                    "目标尚未验证成功：" + state.verification["evidence"], "possible")
                                state.stop_reason = "verification_failed"
                                raise RuntimeError(state.error_info.message)
                            state.messages.append(Message("user", "[自动环境观察，不是用户新指令] 最终效果验证：" +
                                json.dumps(state.verification, ensure_ascii=False) + "。请根据证据继续处理，不能宣称目标已完成。"))
                            self._checkpoint(state)
                            continue
                    state.answer = reply.content
                    state.status = "completed"
                    state.stop_reason = "model_answer"
                    break

                actions = {}
                for call in reply.tool_calls:
                    try:
                        effect = self.registry.get(call.name).effect
                    except KeyError:
                        effect = "unknown"
                    actions[call.id] = state.memory.plan_action(call, effect)
                self.checkpoint_phase = "planned"
                self._checkpoint(state)

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
                        action = actions[call.id]
                        if self.long_horizon:
                            state.memory.guard(action)
                        probe = self.registry.get(call.name).recovery_probe
                        if probe is not None:
                            action.recovery_checks = probe(self.environment, json.loads(call.arguments))
                        action.status = "started"
                        self.checkpoint_phase = "executing"
                        self._checkpoint(state)
                        self.cancel_check()
                        self.logger.emit("tool_execution_started", turn=turn, call_id=call.id, tool=call.name,
                                         validation="passed")
                        executed = True

                    phase = "tool"
                    try:
                        observation = (
                            self.executor.reject(call, "BATCH_REJECTED", batch_error)
                            if batch_error else self.executor.execute(call, on_execution_start=execution_started)
                        )
                    except ToolFailure as error:
                        observation = Observation(call.id, call.name, error=str(error), status="rejected", error_info=error.info)
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
                    state.memory.observed(actions[call.id], observation, state.turn)
                    self.checkpoint_phase = "observed"
                    self._checkpoint(state)
                    self.logger.tool_finished(observation, turn, duration)
                    if self.on_observation is not None:
                        phase = "observation"
                        self.on_observation(observation)
                    if self.long_horizon and state.memory.no_progress_turns >= 8:
                        state.memory.blocked_reason = "连续八次操作未获得新的进展证据。"
                        state.error_info = ErrorInfo("NO_PROGRESS", "progress", state.memory.blocked_reason, "possible")
                        raise RuntimeError(state.error_info.message)
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
                history = list(state.messages)
                if state.status != "completed":
                    history.append(Message("assistant", f"[本次执行状态：{state.status}；{state.error or state.stop_reason}。"
                                           "已有操作可能产生影响，后续追问应先核实。]"))
                # 补齐取消时未返回的工具结果只作用于会话，不改变原始执行状态。
                saved_state = AgentState(task=state.task, messages=history, omitted_messages=state.omitted_messages)
                self._checkpoint(saved_state)
                self.checkpoint_phase = "finished"
                # 最终快照仍保存真实执行状态，不能被仅供会话的补齐历史覆盖。
                try:
                    self._save_checkpoint(state)
                except (AgentCancelled, KeyboardInterrupt) as error:
                    if state.error is None:
                        state.status, state.stop_reason, state.error = "cancelled", "user_cancelled", str(error) or "用户中止任务。"
                        state.answer = None
                        state.error_info = ErrorInfo("CANCELLED", "checkpoint", state.error, "possible", type(error).__name__)
                        self.last_checkpoint = snapshot(state, self.session, "finished",
                            self.snapshot_extension() if self.snapshot_extension else {})
                        raise
                except Exception as error:
                    if state.error is None:
                        state.status, state.stop_reason, state.error = "failed", "checkpoint_failure", str(error)
                        state.answer = None
                        state.error_info = self._error_info(error, "checkpoint")
                        # 即使最终持久化失败，进程内检查和恢复也不能看到 completed。
                        self.last_checkpoint = snapshot(state, self.session, "finished",
                            self.snapshot_extension() if self.snapshot_extension else {})
                        raise
        return state
