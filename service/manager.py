"""单任务调度、协作取消与工作进程监控。任务终态独立于运行日志。"""

from datetime import datetime, timezone
from dataclasses import asdict, replace
from pathlib import Path
import multiprocessing
import queue
import threading
import uuid

from bootstrap import AgentOptions
from contracts import Message
from core.session import Session
from service.store import ACTIVE, TaskStore
from service.instance import InstanceLock
from service.worker import run_task
from tracing import LogRedactor, _report_trace_error


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TaskBusy(ValueError):
    pass


class TaskManager:
    def __init__(self, data_dir: Path, trace_dir: Path, *, worker_target=run_task):
        self.instance_lock = InstanceLock(data_dir)
        try:
            self.store = TaskStore(data_dir / "tasks.sqlite3")
        except Exception:
            self.instance_lock.close()
            raise
        self.trace_dir = trace_dir
        self.worker_target = worker_target
        self.context = multiprocessing.get_context("spawn")
        self.lock = threading.RLock()
        self.active_id = None
        self.process = self.cancel = self.monitor = None
        self.closed = False
        # 不重新执行重启前的任务，避免重复输入或写文件。
        for record in self.store.list(limit=1000000):
            if record["status"] in ACTIVE:
                record.update(status="interrupted", finished_at=now(), error="服务已重启，任务执行被中断。",
                              stop_reason="service_restart")
                self._save_status(record)
                self._remember_failure(record)

    def _save_status(self, record: dict) -> None:
        self.store.save_status(record, now())

    def _remember_failure(self, record: dict) -> None:
        session = Session.from_dict(self.store.session_history(record["session_id"]))
        session.update([*session.messages, Message("assistant", f"[上次执行未完成：{record.get('error')}；"
                                                  "已有操作可能产生影响，请先核实。]")])
        self.store.save_session_history(session.to_dict())

    def submit(self, task: str, options: AgentOptions, session_id: str | None = None) -> dict:
        options.validate()
        if not task.strip():
            raise ValueError("任务不能为空。")
        with self.lock:
            if self.closed:
                raise TaskBusy("服务正在关闭。")
            if self.active_id is not None:
                raise TaskBusy("已有任务正在执行，请等待完成或停止后再提交。")
            session_record = self.store.get_session(session_id) if session_id else self.store.create_session(task.strip()[:80])
            session_id = session_record["id"]
            history = self.store.session_history(session_id)
            if options.workdir is None and history.get("working_directory"):
                options = replace(options, workdir=history["working_directory"])
            task_id = uuid.uuid4().hex
            record = dict(id=task_id, session_id=session_id, task=task.strip(), options=asdict(options), status="queued",
                          created_at=now(), started_at=None, finished_at=None, answer=None, error=None,
                          error_info=None, run_id=None, turn=0, stop_reason=None)
            self._save_status(record)
            pending = Session.from_dict(history)
            pending.update([*pending.messages, Message("user", record["task"])])
            self.store.save_session_history(pending.to_dict(), record["options"],
                                            record["task"][:80] if not history["messages"] else None)
            cancel = self.context.Event()
            channel = self.context.Queue()
            process = self.context.Process(
                target=self.worker_target,
                args=(task_id, record["task"], options, cancel, channel, str(self.trace_dir / f"{task_id}.trace.jsonl"), history),
                name=f"agent-{task_id[:8]}",
            )
            try:
                process.start()
            except Exception:
                channel.close()
                record.update(status="failed", error="无法启动执行进程。", finished_at=now(), stop_reason="worker_start_failed")
                self._save_status(record)
                self._remember_failure(record)
                raise
            self.active_id, self.process, self.cancel = task_id, process, cancel
            record.update(status="running", started_at=now())
            self._save_status(record)
            self.monitor = threading.Thread(target=self._monitor, args=(task_id, process, channel), daemon=True)
            self.monitor.start()
            return record

    def _monitor(self, task_id, process, channel):
        outcome = None
        try:
            while True:
                try:
                    message = channel.get(timeout=0.1)
                except queue.Empty:
                    if not process.is_alive():
                        break
                    continue
                if message["type"] == "event":
                    self.store.append(task_id, message["data"])
                elif message["type"] == "result":
                    outcome = message["data"]
                    # 收到结果后继续排空通道，等资源真正关闭并退出才允许下一任务。
                elif message["type"] == "session":
                    session_id = self.store.get(task_id)["session_id"]
                    if message["data"]["id"] != session_id:
                        raise ValueError("工作进程返回的会话 ID 不匹配。")
                    self.store.save_session_history(message["data"])
            process.join()
            if outcome is None or process.exitcode != 0:
                outcome = dict(status="failed", answer=None, error="执行进程意外退出。",
                               stop_reason="worker_exit", error_info={
                                   "code": "WORKER_EXIT", "phase": "worker", "message": f"退出码：{process.exitcode}",
                                   "side_effects": "possible", "exception_type": None,
                               })
            with self.lock:
                record = self.store.get(task_id)
                record.update(outcome, finished_at=now())
                if record["status"] != "completed":
                    self._remember_failure(record)
                self._save_status(record)
                self.active_id = self.process = self.cancel = None
        except Exception as error:
            # 监控或事件存储故障时不能留下无人管理、仍在执行操作的进程。
            message = LogRedactor().clean(f"{type(error).__name__}: {error}")
            _report_trace_error(f"任务监控失败：{message}")
            with self.lock:
                if self.active_id == task_id and self.cancel is not None:
                    self.cancel.set()
            process.join(timeout=5)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
            try:
                with self.lock:
                    record = self.store.get(task_id)
                    record.update(status="failed", answer=None, error="任务事件存储或进程监控失败。", finished_at=now(),
                                  stop_reason="monitor_failure", error_info={
                                      "code": "MONITOR_FAILED", "phase": "monitor", "message": message,
                                      "side_effects": "possible", "exception_type": type(error).__name__,
                                  })
                    self._save_status(record)
                    self._remember_failure(record)
                    self.active_id = self.process = self.cancel = None
            except Exception as storage_error:
                _report_trace_error(LogRedactor().clean(f"保存任务失败状态失败：{storage_error}"))
        finally:
            channel.close()
            with self.lock:
                if self.active_id == task_id:
                    self.active_id = self.process = self.cancel = None
            if not process.is_alive():
                process.close()

    def stop(self, task_id: str) -> dict:
        with self.lock:
            record = self.store.get(task_id)
            if record["status"] in ACTIVE and self.active_id == task_id:
                if record["status"] != "stopping":
                    self.cancel.set()
                    record["status"] = "stopping"
                    self._save_status(record)
            return record

    def close(self) -> None:
        with self.lock:
            self.closed = True
            process, monitor = self.process, self.monitor
            if self.active_id:
                self.stop(self.active_id)
        if monitor is not None:
            monitor.join(timeout=5)
            if monitor.is_alive() and process is not None:
                # 仅在整个服务关闭时结束工作进程；按钮停止始终为协作取消。
                process.terminate()
                monitor.join(timeout=5)
        if monitor is not None and monitor.is_alive():
            raise RuntimeError("工作进程未能退出。")
        self.store.close()
        self.instance_lock.close()
