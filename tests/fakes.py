"""共享离线替身与模型响应构造器；不访问真实模型或桌面。"""

import copy
import json

from openai.types.chat import ChatCompletion
from PIL import Image

from contracts import AgentCancelled, ToolCall


class MemoryTrace:
    """保存成功写入的事件，可按事件名模拟日志接收器故障。"""

    def __init__(self, fail_on=()):
        self.events = []
        self.fail_on = set(fail_on)

    def emit(self, event):
        if event["event"] in self.fail_on:
            raise OSError("模拟日志写入失败")
        self.events.append(copy.deepcopy(event))


class SequenceModel:
    """完全不依赖厂商 SDK 的测试模型，证明 Runtime 可以替换 Model。"""

    def __init__(self, *replies):
        self.replies = iter(replies)
        self.requests = []

    def generate(self, messages, tools):
        self.requests.append(copy.deepcopy((messages, tools)))
        reply = next(self.replies)
        if isinstance(reply, BaseException):
            raise reply
        return reply


class MemoryEnvironment:
    """不接触本机文件或 Shell 的环境插件，证明默认工具可以替换 Environment。"""

    def __init__(self):
        self.files = {}
        self.commands = []

    def get_info(self):
        return {"shell": "memory-shell", "working_directory": "memory:/work" if self.files else None}

    def read_file(self, path):
        return {"content": self.files[path], "truncated": False}

    def write_file(self, path, content):
        self.files[path] = content
        return f"已写入 {path}"

    def exec_command(self, cmd, workdir=None, timeout=30):
        self.commands.append(cmd)
        return {"exit_code": 0, "stdout": "模拟执行结果", "stderr": "", "timed_out": False}


def call(call_id, name, arguments):
    """创建统一调用，不依赖 OpenAI 的函数调用类。"""
    return ToolCall(call_id, name, json.dumps(arguments, ensure_ascii=False))


def sdk_response(content=None, calls=None, finish_reason=None):
    """适配器测试使用真实 OpenAI SDK 响应类型，不发送网络请求。"""
    return ChatCompletion.model_validate({
        "id": "test", "object": "chat.completion", "created": 0, "model": "test-model",
        "choices": [{
            "index": 0, "finish_reason": finish_reason or ("tool_calls" if calls else "stop"),
            "message": {"role": "assistant", "content": content, "tool_calls": [
                {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}}
                for c in calls or []
            ] or None},
        }],
    })


class FakeBackend:
    key_names = {"ctrl", "alt", "shift", "s", "a", "enter", "tab", "f8", "win"}

    def __init__(self):
        self.screen = (0, 0, 1280, 720)
        self.window = 123
        self.sent = []
        self.cancelled = False
        self.closed = False
        self.fail_capture = False

    def geometry(self):
        return self.screen

    def foreground(self):
        return {"handle": self.window, "title": "测试窗口"}

    def capture(self, geometry):
        if self.fail_capture:
            raise OSError("capture failed")
        return Image.new("RGB", geometry[2:], "white")

    def check_cancelled(self):
        if self.cancelled:
            raise AgentCancelled("F8")

    def send(self, action, arguments):
        self.sent.append((action, arguments))

    def close(self):
        self.closed = True
