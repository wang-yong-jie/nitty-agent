"""Windows 主显示器的截图/输入闭环；模型坐标始终相对于实际发送的图片。"""

import base64
import ctypes
import math
import os
import threading
import time
import uuid
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from contracts import AgentCancelled, ImageContent, ToolExecutionError, ToolRejected, ToolResult
from adapters.desktop_support import DesktopRecorder, RecoveryTracker, difference


@dataclass(frozen=True)
class Frame:
    id: str
    geometry: tuple[int, int, int, int]
    image_size: tuple[int, int]
    window: int
    captured_at: float
    screen_geometry: tuple[int, int, int, int] | None = None
    source_id: str | None = None
    stable: bool = True

    def point(self, x: int, y: int) -> tuple[int, int]:
        width, height = self.image_size
        if type(x) is not int or type(y) is not int or not (0 <= x < width and 0 <= y < height):
            raise ToolRejected("INVALID_ARGUMENTS", f"坐标必须是截图范围内的整数：0 <= x < {width}，0 <= y < {height}。")
        left, top, native_width, native_height = self.geometry
        return (
            left + min(native_width - 1, round(x * native_width / width)),
            top + min(native_height - 1, round(y * native_height / height)),
        )


def bounded_number(value, name, minimum, maximum):
    if type(value) not in {int, float} or not math.isfinite(value) or not minimum <= value <= maximum:
        raise ToolRejected("INVALID_ARGUMENTS", f"{name} 必须在 {minimum} 到 {maximum} 之间。")
    return value


class WindowsDesktop:
    """独立于 Runtime 的桌面控制器；backend 可注入以进行不触碰桌面的测试。"""

    def __init__(self, *, max_image_size: int = 1600, settle_seconds: float = 0.4,
                 stability_timeout: float = 3, stability_interval: float = 0.15,
                 recovery_limit: int = 6, artifact_dir: Path | None = None, backend=None):
        if type(max_image_size) is not int or not 640 <= max_image_size <= 3840:
            raise ValueError("max_image_size 必须是 640 到 3840 之间的整数。")
        self.settle_seconds = bounded_number(settle_seconds, "settle_seconds", 0, 5)
        self.max_image_size = max_image_size
        self.stability_timeout = bounded_number(stability_timeout, "stability_timeout", 0, 10)
        self.stability_interval = bounded_number(stability_interval, "stability_interval", 0.01, 1)
        self.recovery = RecoveryTracker(recovery_limit)
        self.recorder = DesktopRecorder(artifact_dir) if artifact_dir is not None else None
        self.backend = backend if backend is not None else _WindowsBackend()
        self.frame: Frame | None = None
        self._native = self._view = self._result = None
        self.last_before = self.last_action = None
        self.action_count = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self.frame = None
        self._native = self._view = self._result = self.last_before = None
        self.backend.close()

    def check_cancelled(self):
        self.backend.check_cancelled()

    def get_info(self) -> dict:
        return {
            "monitor": "primary", "geometry": list(self.backend.geometry()),
            "coordinate_space": "latest screenshot pixels", "max_image_size": self.max_image_size,
            "foreground_window": self.backend.foreground(),
            "stop_key": "F8（全局停止后续操作）或终端 Ctrl+C",
        }

    def screenshot(self) -> ToolResult:
        self.check_cancelled()
        self.frame = None
        return self._capture_stable(0)

    def _capture_once(self):
        self.check_cancelled()
        geometry = self.backend.geometry()
        window = self.backend.foreground()
        picture = self.backend.capture(geometry)
        if geometry != self.backend.geometry() or window["handle"] != self.backend.foreground()["handle"]:
            raise RuntimeError("截图时显示设置或前台窗口发生变化，请重新截图。")
        if picture.size != geometry[2:]:
            raise RuntimeError("截图尺寸与物理屏幕尺寸不一致，请检查 DPI 设置。")
        return picture, geometry, window, time.monotonic()

    def _capture_stable(self, minimum):
        started = time.perf_counter()
        self._wait(minimum)
        picture, geometry, window, captured_at = self._capture_once()
        stable_count = 0
        deadline = time.monotonic() + self.stability_timeout
        diff = {"mean": 0, "changed_fraction": 0}
        while self.stability_timeout and time.monotonic() < deadline:
            self._wait(min(self.stability_interval, max(0, deadline - time.monotonic())))
            newer, new_geometry, new_window, captured_at = self._capture_once()
            diff = difference(picture, newer)
            quiet = (geometry == new_geometry and window["handle"] == new_window["handle"]
                     and diff["mean"] <= 1.2 and diff["changed_fraction"] <= 0.005)
            stable_count = stable_count + 1 if quiet else 0
            picture, geometry, window = newer, new_geometry, new_window
            if stable_count >= 2:
                break
        stable = not self.stability_timeout or stable_count >= 2
        self._native = picture
        frame_id = uuid.uuid4().hex
        return self._publish(picture, geometry, geometry, window, captured_at, frame_id, stable,
                             stability={"stable": stable, "samples": stable_count,
                                        "duration_ms": round((time.perf_counter() - started) * 1000, 2), "difference": diff})

    def _publish(self, picture, geometry, screen_geometry, window, captured_at, source_id, stable, **extra):
        ratio = min(1, self.max_image_size / max(picture.size))
        if ratio < 1:
            from PIL import Image

            picture = picture.resize(tuple(max(1, round(n * ratio)) for n in picture.size), Image.Resampling.LANCZOS)
        output = BytesIO()
        picture.save(output, format="PNG")
        frame = Frame(uuid.uuid4().hex, geometry, picture.size, window["handle"], captured_at,
                      screen_geometry, source_id, stable)
        self.frame = frame
        self._view = picture
        self._result = ToolResult({
            "frame_id": frame.id, "image_width": picture.width, "image_height": picture.height,
            "screen_left": geometry[0], "screen_top": geometry[1],
            "screen_width": geometry[2], "screen_height": geometry[3],
            "scale_x": geometry[2] / picture.width, "scale_y": geometry[3] / picture.height,
            "foreground_window": window,
            "source_frame_id": source_id, "stable": stable, **extra,
        }, [ImageContent(base64.b64encode(output.getvalue()).decode("ascii"))])
        return self._result

    def observation(self, frame_id):
        self.validate_frame(frame_id)
        return ToolResult(dict(self._result.data), list(self._result.images))

    def crop(self, frame_id, x, y, width, height):
        frame = self._current(frame_id)
        frame.point(x, y)
        if (type(width) is not int or type(height) is not int or width < 1 or height < 1
                or x + width > frame.image_size[0] or y + height > frame.image_size[1]):
            raise ToolRejected("INVALID_ARGUMENTS", "裁剪区域必须完整位于当前截图内。")
        left, top, native_width, native_height = frame.geometry
        x1 = left + round(x * native_width / frame.image_size[0])
        y1 = top + round(y * native_height / frame.image_size[1])
        x2 = left + round((x + width) * native_width / frame.image_size[0])
        y2 = top + round((y + height) * native_height / frame.image_size[1])
        screen = frame.screen_geometry or frame.geometry
        image = self._native.crop((x1 - screen[0], y1 - screen[1], x2 - screen[0], y2 - screen[1]))
        return self._publish(image, (x1, y1, x2 - x1, y2 - y1), screen, self.backend.foreground(),
                             frame.captured_at, frame.source_id, frame.stable, parent_frame_id=frame.id,
                             crop_region=[x, y, width, height])

    def validate_frame(self, frame_id: str) -> None:
        """供耗时视觉请求完成后复核；不产生新截图，也不消费编号。"""
        frame = self._current(frame_id)
        picture, geometry, window, _ = self._capture_once()
        if geometry != (frame.screen_geometry or frame.geometry) or window["handle"] != frame.window:
            self.frame = None
            raise ToolRejected("STALE_FRAME", "观察期间前台窗口或屏幕变化，请重新截图。")
        left, top, width, height = frame.geometry
        current = picture.crop((left - geometry[0], top - geometry[1], left - geometry[0] + width, top - geometry[1] + height))
        from PIL import Image
        current = current.resize(self._view.size, Image.Resampling.LANCZOS)
        diff = difference(self._view, current)
        if diff["mean"] > 1.2 or diff["changed_fraction"] > 0.005:
            self.frame = None
            raise ToolRejected("STALE_FRAME", "观察期间界面内容已变化，请重新截图并定位。")

    def _current(self, frame_id: str) -> Frame:
        self.check_cancelled()
        frame = self.frame
        if frame is None or frame.id != frame_id:
            raise ToolRejected("STALE_FRAME", "frame_id 已过期或尚未截图，请先调用 desktop_screenshot。")
        if (time.monotonic() - frame.captured_at > 120 or self.backend.geometry() != (frame.screen_geometry or frame.geometry)
                or self.backend.foreground()["handle"] != frame.window):
            self.frame = None
            raise ToolRejected("STALE_FRAME", "截图已过时，或显示设置/前台窗口已变化，请重新截图。")
        return frame

    def _wait(self, seconds):
        deadline = time.monotonic() + seconds
        while True:
            self.check_cancelled()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.05, remaining))

    def _act(self, frame_id, action, **arguments):
        # 参数校验完成后，再次检查焦点和截图，输入前消费掉 frame_id。
        frame = self._current(frame_id)
        if not frame.stable:
            raise ToolRejected("UI_UNSTABLE", "画面尚未稳定，请等待后重新观察，不能立即输入。")
        if time.monotonic() - frame.captured_at > 2:
            self.validate_frame(frame_id)
        self.recovery.check(action, arguments)
        before = self._native
        started = time.perf_counter()
        self.frame = None
        # 输入失败也可能部分生效，结束检查必须覆盖这次尝试。
        self.action_count += 1
        self.last_before = before
        self.last_action = {"type": action, **{key: value for key, value in arguments.items() if key != "text"}}
        try:
            self.backend.send(action, arguments)
        except AgentCancelled:
            raise
        except Exception as error:
            raise ToolExecutionError("ACTION_FAILED", f"动作可能已部分执行，请先重新截图，不要直接重试：{error}") from error
        input_ms = round((time.perf_counter() - started) * 1000, 2)
        try:
            result = self._capture_stable(self.settle_seconds)
        except AgentCancelled:
            raise
        except Exception as error:
            raise ToolExecutionError("POST_ACTION_CAPTURE_FAILED", f"动作已发送，但后续截图失败；先重新截图，不要重复动作：{error}",
                                     phase="result") from error
        result.data["action"] = action
        result.data["input_sent"] = True
        diff = difference(before, self._native)
        recovery = self.recovery.record(action, arguments, diff)
        result.data.update(difference=diff, recovery=recovery,
                           timing_ms={"input": input_ms, "settle_capture": result.data["stability"]["duration_ms"]})
        if self.recorder is not None:
            try:
                result.data["trajectory"] = self.recorder.record(before, self._native, action, arguments,
                    frame.screen_geometry or frame.geometry, result.data["timing_ms"], diff, recovery)
            except OSError as error:
                result.data["recording_error"] = f"轨迹保存失败：{type(error).__name__}"
        return result

    @staticmethod
    def _button(button):
        if button not in ("left", "right", "middle"):
            raise ToolRejected("INVALID_ARGUMENTS", "button 只能是 left、right 或 middle。")
        return button

    def _keys(self, keys):
        if not isinstance(keys, list) or not 1 <= len(keys) <= 4:
            raise ToolRejected("INVALID_ARGUMENTS", "keys 必须是包含 1 到 4 个键名的数组。")
        if any(not isinstance(key, str) for key in keys):
            raise ToolRejected("INVALID_ARGUMENTS", "键名必须是字符串。")
        keys = [key.lower() for key in keys]
        if any(key == "f8" or key not in self.backend.key_names for key in keys):
            raise ToolRejected("INVALID_ARGUMENTS", "键名无效，或使用了保留的急停键 F8。")
        if len(set(keys)) != len(keys):
            raise ToolRejected("INVALID_ARGUMENTS", "组合键不能重复。")
        return keys

    def _click(self, frame_id, x, y, button="left", clicks=1):
        point = self._current(frame_id).point(x, y)
        if type(clicks) is not int or clicks not in (1, 2):
            raise ToolRejected("INVALID_ARGUMENTS", "clicks 只能是 1 或 2。")
        return self._act(frame_id, "click", x=point[0], y=point[1], button=self._button(button), clicks=clicks)

    def _move(self, frame_id, x, y):
        point = self._current(frame_id).point(x, y)
        return self._act(frame_id, "move", x=point[0], y=point[1])

    def _drag(self, frame_id, from_x, from_y, to_x, to_y, button="left", duration=0.5):
        frame = self._current(frame_id)
        return self._act(frame_id, "drag", start=frame.point(from_x, from_y), end=frame.point(to_x, to_y),
                         button=self._button(button), duration=bounded_number(duration, "duration", 0.2, 3))

    def _scroll(self, frame_id, x, y, amount):
        point = self._current(frame_id).point(x, y)
        if type(amount) is not int or not -20 <= amount <= 20 or amount == 0:
            raise ToolRejected("INVALID_ARGUMENTS", "amount 必须是 -20 到 20 之间的非零整数。")
        return self._act(frame_id, "scroll", x=point[0], y=point[1], amount=amount)

    def _press_key(self, frame_id, key):
        return self._act(frame_id, "keys", keys=self._keys([key]))

    def _hotkey(self, frame_id, keys):
        return self._act(frame_id, "keys", keys=self._keys(keys))

    def _type_text(self, frame_id, text):
        if not isinstance(text, str) or not 1 <= len(text) <= 2000:
            raise ToolRejected("INVALID_ARGUMENTS", "text 必须是 1 到 2000 字符的文本。")
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        if any((ord(c) < 32 and c not in "\n\t") or 0xD800 <= ord(c) <= 0xDFFF for c in text):
            raise ToolRejected("INVALID_ARGUMENTS", "文本包含不支持的控制字符或无效 Unicode。")
        return self._act(frame_id, "text", text=text)

    def _wait_and_capture(self, seconds):
        self.frame = None
        self._wait(bounded_number(seconds, "seconds", 0, 10))
        return self.screenshot()

    def perform(self, action: str, **arguments) -> ToolResult:
        handlers = {
            "click": self._click, "move": self._move, "drag": self._drag, "scroll": self._scroll,
            "press_key": self._press_key, "hotkey": self._hotkey, "type_text": self._type_text,
            "wait": self._wait_and_capture,
        }
        if action not in handlers:
            raise ValueError(f"未知桌面动作：{action}")
        return handlers[action](**arguments)


class _WindowsBackend:
    """将 Windows / MSS / PyAutoGUI 依赖限制在这个后端，普通模式不导入它们。"""

    def __init__(self):
        if os.name != "nt":
            raise RuntimeError("桌面模式目前仅支持 Windows。")
        from ctypes import wintypes

        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        self.user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        self._previous_dpi = self.user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        if not self._previous_dpi:
            raise ctypes.WinError(ctypes.get_last_error())
        self._stop = threading.Event()
        self._cancelled = threading.Event()
        self._watcher = None
        self.capture_client = None
        try:
            from mss import MSS
            import pyautogui

            self.gui = pyautogui
            self.key_names = set(pyautogui.KEYBOARD_KEYS)
            self.gui.FAILSAFE = True
            self.capture_client = MSS()
            self.user32.GetForegroundWindow.restype = wintypes.HWND
            self.user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
            self.user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
            self.user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
            self.user32.GetAsyncKeyState.restype = ctypes.c_short
            self.user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
            self.user32.GetCursorPos.restype = wintypes.BOOL
            self._setup_unicode_input()
            self._watcher = threading.Thread(target=self._watch_stop, name="desktop-stop", daemon=True)
            self._watcher.start()
        except BaseException:
            self.close()
            raise

    def _watch_stop(self):
        while not self._stop.wait(0.02):
            if self.user32.GetAsyncKeyState(0x77) & 0x8000:  # F8
                self._cancelled.set()

    def check_cancelled(self):
        if self._stop.is_set() or self._cancelled.is_set() or self.user32.GetAsyncKeyState(0x77) & 0x8000:
            self._cancelled.set()
            raise AgentCancelled("桌面操作已中止。")
        from ctypes import wintypes

        # PyAutoGUI 忽略 GetCursorPos 的失败，会把不可访问的桌面误报为鼠标在 (0,0)。
        if not self.user32.GetCursorPos(ctypes.byref(wintypes.POINT())):
            raise RuntimeError("无法访问当前输入桌面，请在已登录、解锁的 Windows 桌面会话中运行。")
        try:
            self.gui.failSafeCheck()
        except self.gui.FailSafeException as error:
            self._cancelled.set()
            raise AgentCancelled("鼠标到达主显示器角落，已触发急停。") from error

    def geometry(self):
        # 主屏幕原点固定为 (0,0)；在 DPI-aware 线程中每次读取物理尺寸，避免显示器缓存过时。
        width, height = self.user32.GetSystemMetrics(0), self.user32.GetSystemMetrics(1)
        if width <= 0 or height <= 0:
            raise RuntimeError("找不到主显示器，请检查 Windows 桌面会话。")
        return 0, 0, width, height

    def foreground(self):
        handle = self.user32.GetForegroundWindow()
        length = self.user32.GetWindowTextLengthW(handle)
        title = ctypes.create_unicode_buffer(length + 1)
        self.user32.GetWindowTextW(handle, title, len(title))
        return {"handle": int(handle or 0), "title": title.value}

    def capture(self, geometry):
        from PIL import Image

        region = dict(zip(("left", "top", "width", "height"), geometry))
        try:
            captured = self.capture_client.grab(region)
        except Exception as error:
            raise RuntimeError(
                "无法截取 Windows 主屏幕。请在已登录且解锁的桌面会话中运行；"
                f"检查远程桌面是否断开或截图是否受限。原始错误：{error}"
            ) from error
        return Image.frombytes("RGB", captured.size, captured.rgb)

    def _release(self, function, *args, **kwargs):
        # 急停后也必须释放已按下的键/鼠标；这里只绕过释放动作的角落检查。
        previous = self.gui.FAILSAFE
        try:
            self.gui.FAILSAFE = False
            function(*args, **kwargs, _pause=False)
        finally:
            self.gui.FAILSAFE = previous

    def _keys(self, keys):
        held = []
        try:
            for key in keys:
                self.check_cancelled()
                held.append(key)
                self.gui.keyDown(key, _pause=False)
        finally:
            for key in reversed(held):
                self._release(self.gui.keyUp, key)

    def send(self, action, arguments):
        self.check_cancelled()
        try:
            if action == "click":
                self.gui.click(**arguments, interval=0.1)
            elif action == "move":
                self.gui.moveTo(**arguments)
            elif action == "scroll":
                self.gui.moveTo(arguments["x"], arguments["y"])
                self.check_cancelled()
                self._wheel(arguments["amount"])
            elif action == "drag":
                self.gui.moveTo(*arguments["start"])
                try:
                    self.gui.mouseDown(button=arguments["button"], _pause=False)
                    self.gui.moveTo(*arguments["end"], duration=arguments["duration"])
                finally:
                    self._release(self.gui.mouseUp, button=arguments["button"])
            elif action == "keys":
                self._keys(arguments["keys"])
            elif action == "text":
                window = self.foreground()["handle"]
                for character in arguments["text"]:
                    self.check_cancelled()
                    if self.foreground()["handle"] != window:
                        raise RuntimeError("输入过程中前台窗口发生变化，已停止继续输入。")
                    if character in "\n\t":
                        self._keys(["enter" if character == "\n" else "tab"])
                    else:
                        self._unicode(character)
            else:
                raise ValueError(f"未知输入动作：{action}")
        except self.gui.FailSafeException as error:
            self._cancelled.set()
            raise AgentCancelled("鼠标角落急停已触发。") from error

    def _setup_unicode_input(self):
        from ctypes import wintypes

        class KeyboardInput(ctypes.Structure):
            _fields_ = [("vk", wintypes.WORD), ("scan", wintypes.WORD),
                        ("flags", wintypes.DWORD), ("time", wintypes.DWORD), ("extra", ctypes.c_size_t)]

        class MouseInput(ctypes.Structure):
            _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("data", wintypes.DWORD),
                        ("flags", wintypes.DWORD), ("time", wintypes.DWORD), ("extra", ctypes.c_size_t)]

        class InputUnion(ctypes.Union):
            _fields_ = [("ki", KeyboardInput), ("mi", MouseInput)]

        class Input(ctypes.Structure):
            _fields_ = [("type", wintypes.DWORD), ("value", InputUnion)]

        self.Input, self.KeyboardInput, self.MouseInput = Input, KeyboardInput, MouseInput
        self.user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(Input), ctypes.c_int]
        self.user32.SendInput.restype = wintypes.UINT

    def _wheel(self, amount):
        # Windows 一格滚轮是 WHEEL_DELTA=120；不能直接发送工具中的格数。
        inputs = (self.Input * 1)()
        inputs[0].type = 0  # INPUT_MOUSE
        inputs[0].value.mi = self.MouseInput(0, 0, amount * 120 & 0xFFFFFFFF, 0x0800, 0, 0)
        if self.user32.SendInput(1, inputs, ctypes.sizeof(self.Input)) != 1:
            raise RuntimeError("Windows 未接受滚轮输入；目标窗口可能具有更高权限。")

    def _unicode(self, character):
        # KEYEVENTF_UNICODE 接收 UTF-16 code unit；非 BMP 字符按代理对发送。
        encoded = character.encode("utf-16-le")
        units = [int.from_bytes(encoded[i:i + 2], "little") for i in range(0, len(encoded), 2)]
        inputs = (self.Input * (len(units) * 2))()
        for index, unit in enumerate(units):
            for offset, flags in ((0, 0x0004), (1, 0x0004 | 0x0002)):
                item = inputs[index * 2 + offset]
                item.type = 1  # INPUT_KEYBOARD
                item.value.ki = self.KeyboardInput(0, unit, flags, 0, 0)
        if self.user32.SendInput(len(inputs), inputs, ctypes.sizeof(self.Input)) != len(inputs):
            raise RuntimeError("Windows 未接受全部 Unicode 输入；目标窗口可能具有更高权限。")

    def close(self):
        self._stop.set()
        if self._watcher is not None:
            self._watcher.join(timeout=1)
        if self.capture_client is not None:
            self.capture_client.close()
            self.capture_client = None
        if self._previous_dpi:
            self.user32.SetThreadDpiAwarenessContext(self._previous_dpi)
            self._previous_dpi = None
