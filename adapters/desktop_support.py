"""像素差异、有限恢复与可选视觉轨迹；不读取控件树或应用内部状态。"""

import hashlib
import json
import re
from pathlib import Path

from PIL import ImageChops, ImageDraw, ImageStat

from contracts import ToolRejected


def difference(before, after) -> dict:
    if before.size != after.size:
        return {"mean": 255.0, "changed_fraction": 1.0}
    size = (min(512, before.width), min(288, before.height))
    delta = ImageChops.difference(before.convert("RGB").resize(size), after.convert("RGB").resize(size))
    gray = delta.convert("L")
    histogram = gray.histogram()
    return {"mean": round(sum(ImageStat.Stat(delta).mean) / 3, 4),
            "changed_fraction": round(sum(histogram[9:]) / (size[0] * size[1]), 6)}


class RecoveryTracker:
    def __init__(self, limit=6):
        if type(limit) is not int or not 3 <= limit <= 20:
            raise ValueError("恢复预算必须是 3 到 20 之间的整数。")
        self.limit = limit
        self.reset()

    def reset(self):
        self.stalled = 0
        self.repeated = 0
        self.signature = None

    @staticmethod
    def signature_for(action, arguments):
        return hashlib.sha256(json.dumps([action, arguments], sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def check(self, action, arguments):
        if self.stalled >= self.limit:
            raise ToolRejected("RECOVERY_EXHAUSTED", "已达到无进展恢复预算；请报告当前阻塞原因，不能继续盲目输入。")
        if self.repeated >= 3 and self.signature == self.signature_for(action, arguments):
            raise ToolRejected("REPEATED_ACTION", "相同动作连续无进展；请重新观察、裁剪放大定位或改变计划，不要重复提交。")

    def record(self, action, arguments, diff):
        signature = self.signature_for(action, arguments)
        changed = diff["mean"] > 0.15 or diff["changed_fraction"] > 0.0005
        if changed:
            self.reset()
        else:
            self.stalled += 1
            self.repeated = self.repeated + 1 if signature == self.signature else 1
            self.signature = signature
        stage = "none" if not self.stalled else ("reobserve" if self.stalled < 3 else "reground" if self.stalled < 5 else "replan")
        return {"no_progress_steps": self.stalled, "budget": self.limit, "suggestion": stage,
                "exhausted": self.stalled >= self.limit}


class DesktopRecorder:
    """仅写入自己的轨迹目录；元数据不包含输入文本，截图按显式配置保存。"""

    def __init__(self, directory: Path):
        self.directory = directory
        self.step = max((int(match[1]) for path in directory.glob("step-*-*.png")
                         if (match := re.fullmatch(r"step-(\d+)-(?:before|after|target)\.png", path.name))), default=0)

    def record(self, before, after, action, arguments, geometry, timing, diff, recovery):
        self.directory.mkdir(parents=True, exist_ok=True)
        self.step += 1
        stem = f"step-{self.step:04d}"
        before_name, after_name, marked_name = (f"{stem}-{kind}.png" for kind in ("before", "after", "target"))
        before.save(self.directory / before_name)
        after.save(self.directory / after_name)
        marked = before.copy()
        point = arguments.get("end") or ((arguments["x"], arguments["y"]) if "x" in arguments else None)
        if point:
            x, y = point[0] - geometry[0], point[1] - geometry[1]
            draw = ImageDraw.Draw(marked)
            draw.ellipse((x - 12, y - 12, x + 12, y + 12), outline="#ff304a", width=3)
            draw.line((x - 18, y, x + 18, y), fill="#ff304a", width=2)
            draw.line((x, y - 18, x, y + 18), fill="#ff304a", width=2)
        marked.save(self.directory / marked_name)
        safe_arguments = {key: value for key, value in arguments.items() if key != "text"}
        if "text" in arguments:
            safe_arguments["text_length"] = len(arguments["text"])
        result = {"step": self.step, "before": before_name, "after": after_name, "target": marked_name,
                  "action": {"type": action, **safe_arguments}, "timing_ms": timing,
                  "difference": diff, "recovery": recovery}
        if point:
            result["position"] = {"x_px": point[0], "y_px": point[1],
                                  "x_norm": (point[0] - geometry[0]) / geometry[2],
                                  "y_norm": (point[1] - geometry[1]) / geometry[3]}
        with (self.directory / "trajectory.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(result, ensure_ascii=False) + "\n")
        return result
