"""控制台与可选 JSONL 追踪；脱敏只作用于日志，不改变模型输入或工具参数。"""

import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from contracts import EventSink, Observation, ToolCall


_SECRET_KEY = re.compile(r"password|passwd|pwd|secret|token|api[_-]?key|authorization|cookie|credential", re.I)
_ASSIGNMENT = re.compile(
    r'''(?ix)([\w:$-]*(?:password|passwd|pwd|secret|token|api[_-]?key|credential)[\w-]*["']?\s*(?:=|:|\s)\s*)'''
    r'''("[^"\r\n]*"|'[^'\r\n]*'|[^\s;,}\]\r\n]+)'''
)
_AUTH = re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+")
_DATA_IMAGE = re.compile(r"data:image/[^;\s]+;base64,[A-Za-z0-9+/=]+", re.I)


class LogRedactor:
    """遮蔽已知环境密钥、常见凭证字段和内联赋值；不声称能识别任意自然语言秘密。"""

    def __init__(self):
        self.secrets = {v for k, v in os.environ.items() if _SECRET_KEY.search(k) and v}

    def remember(self, value) -> None:
        if isinstance(value, str) and value:
            self.secrets.add(value)
        elif isinstance(value, dict):
            for item in value.values():
                self.remember(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                self.remember(item)

    def clean(self, value):
        if isinstance(value, dict):
            for key, item in value.items():
                if _SECRET_KEY.search(str(key)):
                    self.remember(item)
            return {k: "[REDACTED]" if _SECRET_KEY.search(str(k)) else self.clean(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.clean(v) for v in value]
        if not isinstance(value, str):
            return value

        def assignment(match):
            secret = match[2].strip("\"'")
            if secret and secret != "[REDACTED]":
                self.secrets.add(secret)
            return match[1] + "[REDACTED]"

        value = _ASSIGNMENT.sub(assignment, value)

        def authorization(match):
            self.remember(match[0].split(None, 1)[1])
            return match[1] + " [REDACTED]"

        value = _AUTH.sub(authorization, value)
        value = _DATA_IMAGE.sub("[IMAGE OMITTED]", value)
        for secret in sorted(self.secrets, key=len, reverse=True):
            value = value.replace(secret, "[REDACTED]")
        return value


class JsonlTrace:
    """每条事件立即落盘；独占创建文件，避免覆盖已有记录。"""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().resolve()
        self.file = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("x", encoding="utf-8")
        return self

    def __exit__(self, *args):
        if self.file is not None:
            self.file.close()

    def emit(self, event: dict) -> None:
        if self.file is None:
            raise RuntimeError("追踪文件尚未打开。")
        self.file.write(json.dumps(event, ensure_ascii=False) + "\n")
        self.file.flush()


class RunLogger:
    def __init__(self, verbose: bool, sink: EventSink | None):
        self.verbose, self.sink = verbose, sink
        self.redactor = LogRedactor()
        self.run_id = ""

    def emit(self, kind: str, **fields) -> dict:
        # 事件名、运行 ID 和时间是框架元数据，不能被输入的短文本误替换。
        event = {
            "event": kind, "run_id": self.run_id,
            "timestamp": datetime.now(timezone.utc).isoformat(), **self.redactor.clean(fields),
        }
        if self.sink is not None:
            self.sink.emit(event)
        return event

    def start(self) -> None:
        self.run_id = uuid.uuid4().hex
        self.emit("run_started")

    def tool_started(self, call: ToolCall, turn: int, sensitive_parameters: tuple[str, ...]) -> None:
        try:
            arguments = json.loads(call.arguments)
        except (ValueError, TypeError):
            # 无效 JSON 可能包含本应被标为敏感的参数；不记录未经结构化的原文。
            arguments = {"invalid_json": True, "length": len(call.arguments)}
        if isinstance(arguments, dict):
            arguments = dict(arguments)
            for key in sensitive_parameters:
                if key in arguments:
                    self.redactor.remember(arguments[key])
                    arguments[key] = "[REDACTED]"
        elif sensitive_parameters:
            arguments = "[REDACTED: invalid arguments]"
        event = self.emit("tool_started", turn=turn, call_id=call.id, tool=call.name, arguments=arguments)
        if self.verbose:
            print(f"[调用工具] {call.name} {json.dumps(event['arguments'], ensure_ascii=False)}")

    def tool_finished(self, observation: Observation, turn: int, duration: float, *, executed: bool) -> None:
        payload = {"error": observation.error} if observation.error else {"result": observation.result}
        event = self.emit(
            "tool_finished", turn=turn, call_id=observation.tool_call_id, tool=observation.tool_name,
            duration_ms=round(duration * 1000, 2), executed=executed, **payload,
            images=[{"mime_type": image.mime_type, "encoded_length": len(image.data)} for image in observation.images],
        )
        if self.verbose:
            safe_payload = {key: event[key] for key in payload}
            print(f"[工具结果] {json.dumps(safe_payload, ensure_ascii=False)} [耗时 {duration:.2f}s]")
