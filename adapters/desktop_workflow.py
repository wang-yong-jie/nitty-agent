"""桌面效果验证；通过回调接入通用 Runtime，不改变其他任务的结束规则。"""

import base64
from io import BytesIO
import time

from contracts import AgentCancelled, ImageContent, ToolExecutionError, ToolRejected


def encode_picture(picture):
    picture = picture.copy()
    picture.thumbnail((1600, 1600))
    buffer = BytesIO()
    picture.save(buffer, format="PNG")
    return ImageContent(base64.b64encode(buffer.getvalue()).decode("ascii"))


class DesktopWorkflow:
    def __init__(self, desktop, verifier):
        self.desktop = desktop
        self.verifier = verifier
        self.run_start_actions = 0
        self.latest = None
        self.observation_id = None

    def start(self):
        self.run_start_actions = self.desktop.action_count
        self.desktop.recovery.reset()
        self.desktop.last_before = self.desktop.last_action = None
        self.latest = None
        self.observation_id = None

    def verify(self, expected, frame_id=None, *, scope="step"):
        shot = self.desktop.observation(frame_id) if frame_id else self.desktop.screenshot()
        frame_id = shot.data["frame_id"]
        self.observation_id = frame_id
        started = time.perf_counter()
        if not shot.data.get("stable", True):
            result = {"status": "uncertain", "evidence": "当前画面尚未稳定，不能确认期望结果。"}
        else:
            before = encode_picture(self.desktop.last_before) if self.desktop.last_before is not None else None
            try:
                result = self.verifier.verify(expected, shot.images[0], before=before, action=self.desktop.last_action)
            except AgentCancelled:
                raise
            except Exception as error:
                result = {"status": "uncertain", "evidence": f"验证请求未完成：{type(error).__name__}"}
            finally:
                self.desktop.validate_frame(frame_id)
        self.latest = {**result, "expected": expected, "scope": scope, "frame_id": frame_id,
                       "duration_ms": round((time.perf_counter() - started) * 1000, 2)}
        if result["status"] == "achieved":
            self.desktop.recovery.reset()
        shot.data["verification"] = self.latest
        return shot

    def finish(self, task):
        if self.desktop.action_count == self.run_start_actions:
            return None
        started = time.perf_counter()
        try:
            return self.verify(task, scope="task").data["verification"]
        except ToolRejected as error:
            self.latest = {"status": "uncertain", "evidence": f"验证期间观察已失效，请重新观察：{error}",
                           "expected": task, "scope": "task", "frame_id": self.observation_id or "",
                           "duration_ms": round((time.perf_counter() - started) * 1000, 2)}
            return self.latest

    def observe(self, observation):
        if observation.error_info and observation.error_info.code == "RECOVERY_EXHAUSTED":
            raise ToolExecutionError("RECOVERY_EXHAUSTED", "桌面连续无进展，恢复预算已耗尽；停止本次任务，请核实当前界面后继续。",
                                     phase="recovery")
