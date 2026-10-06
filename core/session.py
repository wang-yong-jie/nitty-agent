"""会话历史独立于单次执行；裁剪时将工具调用与结果作为一个整体。"""

from dataclasses import asdict, dataclass, field, replace
import json
import hashlib
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
    summary: str = ""
    summary_mode: str = "none"
    compaction_runs: int = 0
    compacted_hash: str = ""
    compactor: object = field(default=None, repr=False, compare=False)

    def compact(self, messages):
        history, omitted = trim_history(messages, self.max_history_chars, self.max_history_messages)
        if not omitted:
            return history
        retained = {id(message) for message in history}
        removed = [{"role": item.role, "content": (item.content or "")[:2000],
                    "tools": [{"id": call.id, "name": call.name, "arguments": call.arguments[:1000]} for call in item.tool_calls]}
                   for item in messages if id(item) not in retained]
        digest = hashlib.sha256(json.dumps(removed, ensure_ascii=False).encode()).hexdigest()
        if digest != self.compacted_hash:
            # 摘要请求本身也有预算；任务目标及结构化状态不依赖这个摘要。
            selected, budget = [], 0
            for item in reversed(removed):
                size = len(json.dumps(item, ensure_ascii=False))
                if budget + size > 24000:
                    break
                selected.insert(0, item)
                budget += size
            try:
                if self.compactor is None:
                    raise ValueError("未配置模型摘要器")
                summary = self.compactor(self.summary, selected)
                if not isinstance(summary, str) or not 1 <= len(summary) <= 8000:
                    raise ValueError("摘要长度无效")
                self.summary, self.summary_mode = summary, "semantic"
            except BaseException as error:
                from contracts import AgentCancelled
                if isinstance(error, (AgentCancelled, KeyboardInterrupt, SystemExit)):
                    raise
                excerpts = "\n".join(f"[{item['role']}] {item['content'][:600]} {json.dumps(item['tools'], ensure_ascii=False)[:400]}"
                                     for item in selected)
                self.summary = ("[摘要器不可用，以下为历史片段，可能不完整]\n" + self.summary[-3500:] + "\n" + excerpts[-4000:])[:8000]
                self.summary_mode = "extractive"
            self.compaction_runs += 1
            self.compacted_hash = digest
            self.omitted_messages += omitted
        return history

    def update(self, messages: list[Message]) -> None:
        history = self.compact(complete_history(messages))
        # 截图只用于本次执行，不落盘，也不让追问误用已经过期的桌面画面。
        self.messages = [replace(message, images=[], content=(message.content or "") +
                                 ("\n[历史截图未保留，请重新截图核实当前画面。]" if message.images else ""))
                         for message in history]

    def to_dict(self) -> dict:
        return {"id": self.id, "messages": [{key: value for key, value in asdict(message).items() if key != "images"}
                                            for message in self.messages],
                "omitted_messages": self.omitted_messages, "working_directory": self.working_directory,
                "max_history_chars": self.max_history_chars, "max_history_messages": self.max_history_messages,
                "summary": self.summary, "summary_mode": self.summary_mode, "compaction_runs": self.compaction_runs,
                "compacted_hash": self.compacted_hash}

    @classmethod
    def from_dict(cls, data: dict) -> "Session":
        values = {key: value for key, value in data.items() if key != "messages"}
        messages = [Message(**{**message, "tool_calls": [ToolCall(**call) for call in message.get("tool_calls", [])]})
                    for message in data.get("messages", [])]
        session = cls(**values)
        session.update(messages)
        return session
