"""控制台与可选 JSONL 追踪；脱敏只作用于日志，不改变模型输入或工具参数。"""

import json
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from dataclasses import asdict
from pathlib import Path

from contracts import EventSink, Observation, ToolCall, TraceWriteError


_SECRET_KEY = re.compile(r"password|passwd|pwd|secret|token|api[_-]?key|authorization|cookie|credential", re.I)
_ASSIGNMENT = re.compile(
    r'''(?ix)([\w:$-]*(?:password|passwd|pwd|secret|token|api[_-]?key|credential)[\w-]*["']?\s*(?:=|:|\s)\s*)'''
    r'''("[^"\r\n]*"|'[^'\r\n]*'|[^\s;,}\]\r\n]+)'''
)
_AUTH = re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+")
_DATA_IMAGE = re.compile(r"data:image/[^;\s]+;base64,[A-Za-z0-9+/=]+", re.I)


_TOKEN_COUNTS = {"input_tokens", "output_tokens", "prompt_tokens", "completion_tokens", "total_tokens",
                 "cache_creation_input_tokens", "cache_read_input_tokens"}


def _secret_field(key, value):
    # 模型用量中的计数不是凭证；仅放行已知名称且为整数的字段。
    return bool(_SECRET_KEY.search(str(key))) and not (key in _TOKEN_COUNTS and type(value) is int)


def _report_trace_error(message: str) -> None:
    # 报告日志故障本身也不能遮蔽任务错误（例如 stderr 已被宿主关闭）。
    try:
        print(f"[日志故障] {message}", file=sys.stderr)
    except Exception:
        pass


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
                if _secret_field(key, item):
                    self.remember(item)
            return {k: "[REDACTED]" if _secret_field(k, v) else self.clean(v) for k, v in value.items()}
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

    def __exit__(self, exc_type, exc_value, traceback):
        if self.file is not None:
            try:
                self.file.close()
            except Exception as error:
                # 每次 emit 已刷新；关闭阶段的清理错误不覆盖正在传播的任务异常。
                message = LogRedactor().clean(f"关闭追踪文件失败：{type(error).__name__}: {error}")
                _report_trace_error(message)

    def emit(self, event: dict) -> None:
        if self.file is None:
            raise RuntimeError("追踪文件尚未打开。")
        self.file.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n")
        self.file.flush()


class RunLogger:
    def __init__(self, verbose: bool, sink: EventSink | None, *, strict: bool = False):
        self.verbose, self.sink = verbose, sink
        self.redactor = LogRedactor()
        self.run_id = ""
        self.strict = strict
        self.sequence = 0
        self.errors: list[str] = []
        self.sink_error: TraceWriteError | None = None

    def emit(self, kind: str, *, fatal: bool = True, **fields) -> dict:
        # 事件名、运行 ID 和时间是框架元数据，不能被输入的短文本误替换。
        cleaned = self.redactor.clean(fields)
        # 状态与错误分类来自框架契约，不能被用户输入的短文本误改，影响事件解释。
        for key in ("status", "stop_reason", "validation"):
            if key in fields:
                cleaned[key] = fields[key]
        if isinstance(fields.get("error_info"), dict):
            for key in ("code", "phase", "side_effects", "exception_type"):
                cleaned["error_info"][key] = fields["error_info"].get(key)
        self.sequence += 1
        event = {
            "schema_version": 2, "sequence": self.sequence,
            "event": kind, "run_id": self.run_id,
            "timestamp": datetime.now(timezone.utc).isoformat(), **cleaned,
        }
        if self.sink is not None:
            if self.sink_error is None:
                try:
                    self.sink.emit(event)
                except Exception as error:
                    message = self.redactor.clean(f"{type(error).__name__}: {error}")
                    self.errors.append(message)
                    self.sink_error = TraceWriteError(f"运行日志写入失败：{message}")
                    # 每次运行只报告一次；坏掉的日志接收器不再重试，避免重复阻塞执行。
                    _report_trace_error(str(self.sink_error))
            if self.sink_error is not None and self.strict and fatal:
                raise self.sink_error
        return event

    def start(self, **fields) -> None:
        self.run_id = uuid.uuid4().hex
        self.sequence = 0
        self.errors = []
        self.sink_error = None
        self.emit("run_started", **fields)

    def tool_started(self, call: ToolCall, turn: int, sensitive_parameters: tuple[str, ...]) -> None:
        try:
            arguments = json.loads(call.arguments)
            json.dumps(arguments, allow_nan=False)
        except (ValueError, TypeError):
            # 无效 JSON 可能包含本应被标为敏感的参数；不记录未经结构化的原文。
            arguments = {"invalid_json": True, "length": len(call.arguments) if isinstance(call.arguments, str) else None}
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
            print(f"[调用工具] {event['tool']} {json.dumps(event['arguments'], ensure_ascii=False)}")

    def tool_finished(self, observation: Observation, turn: int, duration: float) -> None:
        payload = {"error": observation.error} if observation.error else {"result": observation.result}
        if observation.error_info is not None:
            payload["error_info"] = asdict(observation.error_info)
        event = self.emit(
            "tool_finished", turn=turn, call_id=observation.tool_call_id, tool=observation.tool_name,
            duration_ms=round(duration * 1000, 2), status=observation.status, executed=observation.executed, **payload,
            images=[{"mime_type": image.mime_type, "encoded_length": len(image.data)} for image in observation.images],
        )
        if self.verbose:
            safe_payload = {key: event[key] for key in payload}
            print(f"[工具结果] [{event['status']}, executed={event['executed']}] "
                  f"{json.dumps(safe_payload, ensure_ascii=False)} [耗时 {duration:.2f}s]")
