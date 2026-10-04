"""会话历史独立于单次执行；裁剪时将工具调用与结果作为一个整体。"""

from dataclasses import asdict, dataclass, field, replace
import json
import uuid

from contracts import Message, ToolCall


def complete_history(messages: list[Message]) -> list[Message]:
    """给中断的工具调用补齐未知结果，绝不重新执行历史中的调用。"""
    result = []
    pending = []

    def close_pending():
        for call in pending:
            result.append(Message("tool", json.dumps({"error": "上次执行中断，未收到此调用的完整结果；操作可能已产生影响，请先核实。"},
                                                    ensure_ascii=False), tool_call_id=call.id, is_error=True))
        pending.clear()

    for message in messages:
        if message.role != "tool":
            close_pending()
        elif pending:
            match = next((call for call in pending if call.id == message.tool_call_id), None)
            if match is not None:
                pending.remove(match)
            else:
                continue
        else:
            continue  # 不向协议传递孤立工具结果。
        result.append(message)
        if message.tool_calls:
            pending.extend(message.tool_calls)
    close_pending()
    return result


def trim_history(messages: list[Message], max_chars: int = 64000, max_messages: int = 160) -> tuple[list[Message], int]:
    """字符预算不是精确 token 计数；保留最新问题，按完整调用组保留最近历史。"""
    if type(max_chars) is not int or max_chars < 1000 or type(max_messages) is not int or max_messages < 2:
        raise ValueError("历史预算至少为 1000 字符和 2 条消息。")
    groups = []
    for message in messages:
        if message.role == "tool" and groups and groups[-1][0].tool_calls:
            groups[-1].append(message)
        else:
            groups.append([message])
    latest_user = next((i for i in range(len(groups) - 1, -1, -1) if groups[i][0].role == "user"), None)
    kept = {}
    chars = count = 0
    order = ([latest_user] if latest_user is not None else []) + [i for i in reversed(range(len(groups))) if i != latest_user]
    for index in order:
        group = groups[index]
        size = sum(len(message.content or "") + sum(len(call.arguments) + len(call.name) + len(call.id)
                                                    for call in message.tool_calls) + 64 for message in group)
        if chars + size > max_chars or count + len(group) > max_messages:
            # 单个用户问题过长时显式裁剪；工具调用参数不能截成非法 JSON。
            if index == latest_user:
                message = group[0]
                group = [replace(message, content=(message.content or "")[:max_chars - 128] + "\n[问题过长，后半部分已裁剪。]")]
                size = len(group[0].content) + 64
            else:
                continue
        kept[index] = group
        chars += size
        count += len(group)
    history = [message for index in sorted(kept) for message in kept[index]]
    return history, len(messages) - len(history)


@dataclass
class Session:
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    messages: list[Message] = field(default_factory=list)
    omitted_messages: int = 0
    working_directory: str | None = None
    max_history_chars: int = 64000
    max_history_messages: int = 160

    def update(self, messages: list[Message]) -> None:
        history, omitted = trim_history(complete_history(messages), self.max_history_chars, self.max_history_messages)
        self.omitted_messages += omitted
        # 截图只用于本次执行，不落盘，也不让追问误用已经过期的桌面画面。
        self.messages = [replace(message, images=[], content=(message.content or "") +
                                 ("\n[历史截图未保留，请重新截图核实当前画面。]" if message.images else ""))
                         for message in history]

    def to_dict(self) -> dict:
        return {"id": self.id, "messages": [{key: value for key, value in asdict(message).items() if key != "images"}
                                            for message in self.messages],
                "omitted_messages": self.omitted_messages, "working_directory": self.working_directory,
                "max_history_chars": self.max_history_chars, "max_history_messages": self.max_history_messages}

    @classmethod
    def from_dict(cls, data: dict) -> "Session":
        values = {key: value for key, value in data.items() if key != "messages"}
        messages = [Message(**{**message, "tool_calls": [ToolCall(**call) for call in message.get("tool_calls", [])]})
                    for message in data.get("messages", [])]
        session = cls(**values)
        session.update(messages)
        return session
