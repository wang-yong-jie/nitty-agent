"""计划、进展和验收工具；完成状态只能由独立验收写入。"""

import time

from contracts import AgentCancelled, ToolRejected
from core.verification import verify_checks


INSTRUCTIONS = (
    "长任务先用 task_plan 建立稳定 ID 的里程碑及明确验收条件；文件任务优先设置可重复运行的文件验收。"
    "用 task_progress 保存事实、约束、决定和当前步骤。事实说明来源，不要把推测写成已验证事实。"
    "状态索引标注 omitted 时，用 task_state 分页读取相关约束和步骤细节。"
    "完成一个步骤后调用 task_verify；只有独立验收通过才能标为 completed。"
    "恢复任务后先核实当前环境。存在 uncertain 动作时禁止再次发送有副作用的操作；"
    "先读取产物或截图，再用 task_reconcile 验收上次结果。文件写入会依据执行前保存的内容哈希核对是否已生效；无法确认时说明阻塞原因。"
)

CHECK_SCHEMA = {"type": "object", "additionalProperties": False,
    "properties": {"kind": {"type": "string", "enum": ["file_contains", "file_equals", "file_sha256", "command_succeeded"]},
                   "path": {"type": "string", "minLength": 1, "maxLength": 4096},
                   "expected": {"type": "string", "maxLength": 20000},
                   "call_id": {"type": "string", "minLength": 1, "maxLength": 200}},
    "required": ["kind"], "oneOf": [{"properties": {"kind": {"enum": ["file_contains", "file_equals", "file_sha256"]}},
                                    "required": ["path", "expected"]},
                                   {"properties": {"kind": {"const": "command_succeeded"}}, "required": ["call_id"]}]}


class TaskController:
    def __init__(self, runtime, verifier=None, desktop_verifier=None):
        self.runtime, self.verifier, self.desktop_verifier = runtime, verifier, desktop_verifier

    @property
    def state(self):
        if self.runtime.state is None:
            raise ToolRejected("NO_TASK", "没有正在执行的任务。")
        return self.runtime.state

    def verify(self, expected, checks=None, candidate=None):
        started = time.perf_counter()
        try:
            if checks:
                result = verify_checks(checks, self.runtime.environment, self.state.observations)
            elif self.desktop_verifier is not None:
                result = self.desktop_verifier(expected)
            elif self.verifier is not None:
                result = self.verifier(expected, self.state, candidate)
            else:
                result = {"status": "uncertain", "evidence": "没有配置独立验收器，请提供可重复运行的文件或命令验收条件。"}
        except AgentCancelled:
            raise
        except Exception as error:
            result = {"status": "uncertain", "evidence": f"验收未完成：{type(error).__name__}"}
        return {**result, "expected": expected, "scope": "task", "frame_id": result.get("frame_id", ""),
                "duration_ms": round((time.perf_counter() - started) * 1000, 2)}

    def finish(self, candidate):
        memory = self.state.memory
        unfinished = [step.title for step in memory.milestones if step.status != "completed"]
        if unfinished:
            return {"status": "unmet", "expected": memory.goal, "evidence": "仍有未验收步骤：" + "、".join(unfinished),
                    "scope": "task", "frame_id": "", "duration_ms": 0}
        if any(action.status == "uncertain" and action.effect not in {"read", "internal"} for action in memory.actions):
            return {"status": "uncertain", "expected": memory.goal, "evidence": "仍有操作的实际结果未核实。",
                    "scope": "task", "frame_id": "", "duration_ms": 0}
        # 有确定性验收的计划再次读取实际产物，避免凭陈旧的完成状态结束。
        checks = [check for step in memory.milestones for check in step.checks]
        result = self.verify(memory.goal, checks, candidate)
        if checks and result["status"] == "achieved" and self.verifier is not None:
            return self.verify(memory.goal, candidate=candidate)
        return result

    def register(self):
        registry = self.runtime.registry
        strings = {"type": "array", "maxItems": 50, "items": {"type": "string", "minLength": 1, "maxLength": 1000}}
        checks = {"type": "array", "minItems": 1, "maxItems": 20, "items": CHECK_SCHEMA}

        def plan(_environment, steps):
            self.state.memory.set_plan(steps)
            self.state.memory.last_progress_turn = self.state.turn
            return self.state.memory.context()

        def progress(_environment, **data):
            memory = self.state.memory
            updates = {}
            for key in ("facts", "constraints", "decisions"):
                values = data.get(key, [])
                combined = list(dict.fromkeys([*getattr(memory, key), *values]))
                if len(combined) > 500:
                    raise ToolRejected("MEMORY_LIMIT", "状态条目超过 500 项，请将明细写入任务产物并保留索引，不能丢弃已有约束。")
                updates[key] = combined
            # 所有参数先核实，拒绝调用时不能留下部分写入的状态。
            step = memory.step(data["current_step"]) if "current_step" in data else None
            for key, values in updates.items():
                setattr(memory, key, values)
            if "current_step" in data:
                if step.status != "completed":
                    step.status = "running"
                memory.current_step = step.id
            if "blocked_reason" in data:
                memory.blocked_reason = data["blocked_reason"]
            return memory.context()

        def verify(_environment, step_id):
            memory = self.state.memory
            step = memory.step(step_id)
            result = self.verify(step.success_criteria, step.checks)
            step.verification = result
            step.status = "completed" if result["status"] == "achieved" else "blocked" if result["status"] == "uncertain" else "running"
            if step.status == "completed":
                memory.last_progress_turn, memory.no_progress_turns = self.state.turn, 0
                memory.current_step = next((item.id for item in memory.milestones if item.status != "completed"), None)
                memory.blocked_reason = None
            return {"step_id": step.id, "verification": result, "progress": memory.context()}

        def reconcile(_environment, action_id, expected, checks=None):
            memory = self.state.memory
            action = next((item for item in memory.actions if item.id == action_id and item.status == "uncertain"), None)
            if action is None:
                raise ToolRejected("UNKNOWN_ACTION", "没有此未决动作。")
            if action.recovery_checks:
                post = action.recovery_checks["post"]
                result = self.verify(expected, [post])
                if result["status"] == "achieved":
                    action.status, action.resolution = "succeeded", result["evidence"]
                elif action.recovery_checks.get("pre"):
                    result = self.verify("执行前的文件状态仍保持", [action.recovery_checks["pre"]])
                    if result["status"] == "achieved":
                        action.status, action.resolution = "not_executed", result["evidence"]
                return {"action_id": action.id, "verification": result, "resolved": action.status != "uncertain",
                        "outcome": action.status}
            if checks and any(check["kind"] == "command_succeeded" for check in checks):
                raise ToolRejected("STALE_EVIDENCE", "核对未知副作用必须重新读取当前产物，不能用旧命令结果。")
            result = self.verify(expected, checks)
            if result["status"] == "achieved":
                action.status, action.resolution = "succeeded", result["evidence"]
                memory.last_progress_turn, memory.no_progress_turns = self.state.turn, 0
            return {"action_id": action.id, "verification": result, "resolved": action.status != "uncertain"}

        registry.register("task_plan", "建立或调整任务里程碑；已验收步骤保留。", {
            "steps": {"type": "array", "minItems": 1, "maxItems": 100, "items": {"type": "object", "additionalProperties": False,
                "properties": {"id": {"type": "string", "minLength": 1, "maxLength": 100},
                    "title": {"type": "string", "minLength": 1, "maxLength": 1000},
                    "success_criteria": {"type": "string", "minLength": 1, "maxLength": 2000}, "checks": checks},
                "required": ["id", "title", "success_criteria"]}}}, ["steps"], plan,
            requires_single_call=True, instructions=INSTRUCTIONS, effect="internal")
        registry.register("task_progress", "保存当前步骤、事实来源、约束、决定及阻塞原因；不能自行标记完成。", {
            "facts": strings, "constraints": strings, "decisions": strings,
            "current_step": {"type": "string", "minLength": 1, "maxLength": 100},
            "blocked_reason": {"type": "string", "maxLength": 2000}}, [], progress,
            requires_single_call=True, instructions=INSTRUCTIONS, effect="internal")
        registry.register("task_verify", "只读验收指定里程碑，返回证据；通过后才标记完成。", {
            "step_id": {"type": "string", "minLength": 1, "maxLength": 100}}, ["step_id"], verify,
            requires_single_call=True, instructions=INSTRUCTIONS, effect="internal")
        registry.register("task_reconcile", "重新核实未知操作的当前结果；证据通过后解除未决状态，不重放上次操作。", {
            "action_id": {"type": "string", "minLength": 1, "maxLength": 200},
            "expected": {"type": "string", "minLength": 1, "maxLength": 2000}, "checks": checks},
            ["action_id", "expected"], reconcile, requires_single_call=True, instructions=INSTRUCTIONS, effect="internal")

        def read_state(_environment, section, offset=0, limit=5):
            from dataclasses import asdict
            items = getattr(self.state.memory, section)
            selected = [asdict(item) if section in {"milestones", "actions"} else item for item in items[offset:offset + limit]]
            # 验收条件的完整内容仍由验证器保存和使用；这里仅展示索引。
            if section == "milestones":
                for item in selected:
                    item["checks"] = [{**check, "expected": check.get("expected", "")[:1000],
                        "expected_truncated": len(check.get("expected", "")) > 1000} for check in item["checks"]]
            return {"section": section, "offset": offset, "total": len(items), "items": selected,
                    "next_offset": offset + len(selected) if offset + len(selected) < len(items) else None}
        registry.register("task_state", "分页读取持久化步骤、事实、约束、决定或动作账本。", {
            "section": {"type": "string", "enum": ["milestones", "facts", "constraints", "decisions", "actions"]},
            "offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 10}},
            ["section"], read_state, requires_single_call=True, instructions=INSTRUCTIONS, effect="internal")
