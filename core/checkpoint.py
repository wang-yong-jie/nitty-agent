"""版本化的执行快照；不包含图片编码，不自动重放工具。"""

from dataclasses import asdict
import json
from pathlib import Path

from contracts import ErrorInfo, Message, Observation, ToolCall
from core.state import AgentState
from core.task_memory import TaskMemory


class CheckpointWriteError(RuntimeError):
    pass


def messages_to_data(messages):
    return [{"role": item.role, "content": (item.content or "") +
             ("\n[恢复后必须重新获取画面，历史截图已失效。]" if item.images else ""),
             "tool_calls": [asdict(call) for call in item.tool_calls], "tool_call_id": item.tool_call_id,
             "is_error": item.is_error} for item in messages]


def messages_from_data(messages):
    return [Message(**{**item, "tool_calls": [ToolCall(**call) for call in item.get("tool_calls", [])]}) for item in messages]


def snapshot(state, session, phase, extensions=None):
    from core.session import trim_history
    bounded, _ = trim_history(state.messages, session.max_history_chars, session.max_history_messages)
    return {"schema_version": 1, "task": state.task, "session_id": state.session_id,
            "status": state.status, "turn": state.turn, "phase": phase, "run_id": state.run_id,
            "memory": state.memory.to_dict(), "messages": messages_to_data(bounded),
            "session": session.to_dict(), "verification": state.verification,
            "observations": [{**asdict(item), "images": []} for item in state.observations[-100:]],
            "extensions": extensions or {}}


def restore(data, *, resuming=True):
    if not isinstance(data, dict) or type(data.get("schema_version")) is not int or data.get("schema_version") != 1:
        raise ValueError("不支持此执行快照版本。")
    if (not isinstance(data.get("task"), str) or not data["task"].strip()
        or not isinstance(data.get("session_id"), str) or not data["session_id"]
        or type(data.get("turn")) is not int or data["turn"] < 0
        or data.get("status") not in {"running", "completed", "failed", "cancelled", "limit_reached"}
        or data.get("phase") not in {"setup", "ready", "planned", "executing", "observed", "finished"}
        or not isinstance(data.get("memory"), dict) or not isinstance(data.get("messages"), list)
        or not isinstance(data.get("session"), dict)):
        raise ValueError("执行快照的任务、轮次或状态无效。")
    if data["session"].get("id") != data["session_id"]:
        raise ValueError("执行快照的会话 ID 不匹配。")
    if resuming and data.get("status") == "completed":
        raise ValueError("已完成的任务不能恢复。")
    memory = TaskMemory.from_dict(data["memory"])
    if memory.goal != data["task"]:
        raise ValueError("执行快照的任务目标不匹配。")
    if resuming:
        memory.restore_actions()
    from core.session import complete_history
    observations = []
    for item in data.get("observations", []):
        values = {**item, "images": []}
        if values.get("error_info"):
            values["error_info"] = ErrorInfo(**values["error_info"])
        observations.append(Observation(**values))
    return AgentState(task=data["task"], session_id=data["session_id"], turn=data["turn"], memory=memory,
                      messages=complete_history(messages_from_data(data["messages"])), observations=observations,
                      verification=data.get("verification"))


def save_file(path, data):
    path = Path(path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        stream.write(json.dumps(data, ensure_ascii=False, allow_nan=False) + "\n")
        stream.flush()
        import os
        os.fsync(stream.fileno())
    temporary.replace(path)
