"""可被 Windows spawn 导入的离线工作进程替身。"""

import os
import time
from unittest.mock import patch

from bootstrap import AgentSession
from contracts import AgentCancelled, ModelReply
from core.agent import Agent
from tools.local import create_default_registry
from service.worker import run_task
from tests.fakes import MemoryEnvironment, call


def wait_terminal(manager, task_id, timeout=15):
    from service.store import ACTIVE

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with manager.lock:
            task = manager.store.get(task_id)
        if task["status"] not in ACTIVE:
            return task
        time.sleep(0.05)
    raise AssertionError("工作进程没有在测试期限内完成。")


class FixtureModel:
    def __init__(self, task, cancel):
        self.task, self.cancel, self.count = task, cancel, 0

    def generate(self, messages, tools):
        self.count += 1
        if "[history]" in self.task:
            return ModelReply("历史用户消息：" + " | ".join(message.content for message in messages if message.role == "user"))
        if "[fail]" in self.task:
            raise RuntimeError("离线测试：模型请求失败。")
        delay = 20 if "[slow]" in self.task else 0.25
        deadline = time.monotonic() + delay
        while time.monotonic() < deadline:
            if self.cancel.is_set():
                raise AgentCancelled("用户请求停止任务。")
            time.sleep(0.03)
        if self.count == 1:
            return ModelReply(tool_calls=[call("fixture-read", "read_file", {"path": "README.md"})])
        return ModelReply("离线验证完成：已通过测试工具读取项目说明。\n任务、执行事件和最终回答均已保存。")


def fixture_worker(task_id, task, options, cancel, channel, trace_path, session_data=None, **transport):
    if options.long_horizon:
        from dataclasses import replace
        from pathlib import Path
        directory = Path(options.workdir) if options.workdir else Path(trace_path).parent.parent / "durable-artifacts"
        directory.mkdir(parents=True, exist_ok=True)
        return durable_crash_worker(task_id, task, replace(options, workdir=str(directory)), cancel, channel,
                                    trace_path, session_data, **transport)
    from contextlib import contextmanager

    @contextmanager
    def session_factory(options, *, trace, cancel_check, verbose):
        environment = MemoryEnvironment()
        environment.files["README.md"] = "Nitty Agent 离线示例说明"
        agent = Agent(FixtureModel(task, cancel), create_default_registry(), environment,
                      trace=trace, cancel_check=cancel_check, verbose=False)
        yield AgentSession(agent, environment, None)

    with patch("service.worker.agent_session", session_factory):
        run_task(task_id, task, options, cancel, channel, trace_path, session_data, **transport)


def crash_worker(task_id, task, options, cancel, channel, trace_path, session_data=None, **transport):
    os._exit(7)


def blocking_worker(task_id, task, options, cancel, channel, trace_path, session_data=None, **transport):
    # 模拟不能立即取消的外部调用：停止后仍需等待返回。
    time.sleep(1.5)
    fixture_worker(task_id, task, options, cancel, channel, trace_path, session_data, **transport)


def durable_crash_worker(task_id, task, options, cancel, channel, trace_path, session_data=None, **transport):
    """写入已生效、结果尚未返回时真正退出子进程；恢复不能再次写入。"""
    from contextlib import contextmanager
    from pathlib import Path
    from adapters.local import LocalEnvironment
    from tests.fakes import SequenceModel

    @contextmanager
    def session_factory(options, *, trace, cancel_check, verbose):
        recovering = bool(transport.get("checkpoint_data"))
        class DiskEnvironment(LocalEnvironment):
            def write_file(self, path, content):
                result = super().write_file(path, content)
                counter = Path(options.workdir) / "write-count.txt"
                counter.write_text(str(int(counter.read_text()) + 1) if counter.exists() else "1")
                if not recovering:
                    os._exit(7)
                return result
        environment = DiskEnvironment(options.workdir)
        milestone = {"id": "save", "title": "保存评测文件", "success_criteria": "产物包含 已保存", "checks": [
            {"kind": "file_equals", "path": "out.txt", "expected": "已保存"}]}
        replies = [ModelReply(tool_calls=[call("reconcile", "task_reconcile", {"action_id": "write", "expected": "核实产物"})]),
                   ModelReply(tool_calls=[call("verify", "task_verify", {"step_id": "save"})]), ModelReply("已保存且核实。")]
        if not recovering:
            replies = [ModelReply(tool_calls=[call("plan", "task_plan", {"steps": [milestone]})]),
                       ModelReply(tool_calls=[call("write", "write_file", {"path": "out.txt", "content": "已保存"})])]
        agent = Agent(SequenceModel(*replies), create_default_registry(), environment, long_horizon=True,
                      trace=trace, cancel_check=cancel_check, verbose=False)
        yield AgentSession(agent, environment, None)

    with patch("service.worker.agent_session", session_factory):
        run_task(task_id, task, options, cancel, channel, trace_path, session_data, **transport)
