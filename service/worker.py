"""工作进程：进程内装配 Windows 资源，可靠返回任务终态。"""

from contextlib import ExitStack
from dataclasses import asdict
from pathlib import Path
import multiprocessing
import json
import traceback

from bootstrap import AgentOptions, agent_session
from contracts import AgentCancelled
from tracing import JsonlTrace, LogRedactor
from core.session import Session


class TaskTrace:
    """文件日志和 UI 传输独立；文件故障只停止该文件的后续写入。"""

    def __init__(self, channel, file_sink=None):
        self.channel, self.file_sink = channel, file_sink
        self.redactor = LogRedactor()

    def warning(self, error: Exception) -> None:
        self.channel.put({"type": "event", "data": {
            "event": "trace_warning", "message": self.redactor.clean(str(error)),
        }})

    def emit(self, event: dict) -> None:
        if self.file_sink is not None:
            try:
                self.file_sink.emit(event)
            except Exception as error:
                self.file_sink = None
                self.warning(error)
        self.channel.put({"type": "event", "data": event})


def run_task(task_id: str, task: str, options: AgentOptions, cancel, channel, trace_path: str,
             session_data: dict | None = None) -> None:
    """仅接收可序列化配置和 IPC 对象；桌面控制器不能从父进程传入。"""
    session = None
    redactor = LogRedactor()
    result = {"status": "failed", "answer": None, "error": None, "error_info": None,
              "run_id": None, "turn": 0, "stop_reason": None}
    phase = "setup"

    def check_cancelled():
        parent = multiprocessing.parent_process()
        if parent is not None and not parent.is_alive():
            raise AgentCancelled("父服务已退出，停止后续操作。")
        if cancel.is_set():
            raise AgentCancelled("用户请求停止任务。")

    try:
        with ExitStack() as stack:
            sink = TaskTrace(channel)
            try:
                sink.file_sink = stack.enter_context(JsonlTrace(Path(trace_path)))
            except Exception as error:
                sink.warning(error)
            check_cancelled()
            session_arguments = dict(trace=sink, cancel_check=check_cancelled, verbose=False)
            if options.record_desktop:
                session_arguments["artifact_dir"] = Path(trace_path).parent / f"{task_id}.frames"
            session = stack.enter_context(agent_session(options, **session_arguments))
            session.agent.runtime.session = Session.from_dict(session_data) if session_data else Session()

            def save_conversation(conversation):
                data = conversation.to_dict()
                redactor.secrets.update(LogRedactor().secrets)
                for item in data["messages"]:
                    item["content"] = redactor.clean(item["content"])
                    for call in item["tool_calls"]:
                        try:
                            call["arguments"] = json.dumps(redactor.clean(json.loads(call["arguments"])), ensure_ascii=False)
                        except (TypeError, ValueError):
                            call["arguments"] = "{}"  # 无效历史参数已经失败，不用于重新执行。
                channel.put({"type": "session", "data": data})

            session.agent.runtime.on_session_update = save_conversation
            check_cancelled()
            phase = "prepare"
            session.prepare()
            check_cancelled()
            phase = "run"
            result["answer"] = session.agent.run(task)
            phase = "cleanup"
        result["status"] = "completed"
    except (AgentCancelled, KeyboardInterrupt) as error:
        result.update(status="cancelled", error=str(error) or "用户中止任务。", stop_reason="user_cancelled", error_info={
            "code": "CANCELLED", "phase": phase, "message": str(error) or "用户中止任务。",
            "side_effects": "possible" if phase in ("run", "cleanup") else "none", "exception_type": type(error).__name__,
        })
    except Exception as error:
        result.update(error=str(error), stop_reason="worker_cleanup_failed" if phase == "cleanup" else "run_failure", error_info={
            "code": "WORKER_FAILED", "phase": phase, "message": str(error),
            "side_effects": "possible" if phase in ("run", "cleanup") else "none", "exception_type": type(error).__name__,
        })
        if session is not None and session.agent.last_state is not None and session.agent.last_state.error_info:
            result["error_info"] = asdict(session.agent.last_state.error_info)
        redactor.secrets.update(LogRedactor().secrets)
        channel.put({"type": "event", "data": redactor.clean({
            "event": "worker_failed", "error_info": result["error_info"], "traceback": traceback.format_exc(),
        })})
    finally:
        if session is not None and session.agent.last_state is not None:
            state = session.agent.last_state
            result.update(turn=state.turn, run_id=state.run_id)
            result["verification"] = state.verification
            if phase != "cleanup" or result["status"] == "completed":
                result["stop_reason"] = state.stop_reason or result["stop_reason"]
            if state.error_info:
                result["error_info"] = asdict(state.error_info)
            # 清理资源失败也应保留工作进程失败，不被已完成的 Runtime 状态覆盖。
            if result["status"] != "completed":
                result["answer"] = None
        # 在 .env 被加载后重新读取已知秘密，再清理面向浏览器的错误和回答。
        redactor.secrets.update(LogRedactor().secrets)
        cleaned = redactor.clean(result)
        for key in ("status", "stop_reason", "run_id", "turn"):
            cleaned[key] = result[key]
        if result["error_info"]:
            for key in ("code", "phase", "side_effects", "exception_type"):
                cleaned["error_info"][key] = result["error_info"].get(key)
        channel.put({"type": "result", "data": cleaned})
        channel.close()
        channel.join_thread()
