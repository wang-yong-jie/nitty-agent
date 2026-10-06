"""任务语义状态与动作账本；独立于消息长度和模型供应商。"""

from dataclasses import asdict, dataclass, field
import hashlib
import json
import uuid

from contracts import ToolRejected


@dataclass
class Milestone:
    id: str
    title: str
    success_criteria: str
    checks: list[dict] = field(default_factory=list)
    status: str = "pending"
    verification: dict | None = None


@dataclass
class ActionRecord:
    id: str
    tool: str
    fingerprint: str
    effect: str
    status: str = "planned"
    evidence: str | None = None
    resolution: str | None = None
    recovery_checks: dict | None = None
    observation_hash: str | None = None


@dataclass
class TaskMemory:
    goal: str
    milestones: list[Milestone] = field(default_factory=list)
    facts: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    current_step: str | None = None
    blocked_reason: str | None = None
    actions: list[ActionRecord] = field(default_factory=list)
    completed_actions: int = 0
    last_progress_turn: int = 0
    no_progress_turns: int = 0
    verification_attempts: int = 0
    attempt: int = 1

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        values = dict(data)
        values["milestones"] = [Milestone(**step) for step in data.get("milestones", [])]
        values["actions"] = [ActionRecord(**action) for action in data.get("actions", [])]
        return cls(**values)

    def progress(self):
        data = self.to_dict()
        # SSE 的进展投影不重复传送大段验收正文；快照仍保存完整条件。
        for step in data["milestones"]:
            step["checks"] = [{key: value for key, value in check.items() if key != "expected"}
                              for check in step["checks"]]
        data["actions"] = [asdict(action) for action in self.actions if action.status in {"started", "uncertain"}]
        return data

    def context(self):
        # 完整状态持久化，模型每轮只看当前步骤及有界索引；细节可分页读取。
        selected = [step for step in self.milestones if step.id == self.current_step]
        selected += [step for step in self.milestones if step.status != "completed" and step not in selected][:5]
        selected += [step for step in self.milestones if step.status == "completed"][-3:]
        data = {"goal": self.goal, "current_step": self.current_step, "blocked_reason": self.blocked_reason,
                "completed_steps": sum(step.status == "completed" for step in self.milestones),
                "total_steps": len(self.milestones), "milestones": [{"id": step.id, "title": step.title[:300],
                    "success_criteria": step.success_criteria[:600], "status": step.status,
                    "verification": {"status": step.verification["status"], "evidence": step.verification["evidence"][:600]}
                        if step.verification else None} for step in selected],
                "unresolved_actions": [{"id": action.id, "tool": action.tool, "effect": action.effect,
                    "status": action.status} for action in self.actions if action.status in {"uncertain", "started"}][:20],
                "omitted": {"milestones": len(self.milestones) - len(selected)}, "no_progress_turns": self.no_progress_turns}
        for key in ("facts", "constraints", "decisions"):
            values, budget = [], 0
            for value in reversed(getattr(self, key)):
                if budget + len(value) > 3000:
                    break
                values.insert(0, value)
                budget += len(value)
            data[key] = values
            data["omitted"][key] = len(getattr(self, key)) - len(values)
        return data

    def set_plan(self, steps):
        ids = [step["id"] for step in steps]
        if len(set(ids)) != len(ids):
            raise ToolRejected("INVALID_PLAN", "步骤 ID 必须唯一。")
        existing = {step.id: step for step in self.milestones}
        if any(step.status == "completed" and step.id not in ids for step in self.milestones):
            raise ToolRejected("INVALID_PLAN", "不能从计划中移除已验证的步骤。")
        updated = []
        for data in steps:
            step = Milestone(**data)
            old = existing.get(step.id)
            if old and (old.title, old.success_criteria, old.checks) == (step.title, step.success_criteria, step.checks):
                step.status, step.verification = old.status, old.verification
            updated.append(step)
        self.milestones = updated
        self.current_step = next((step.id for step in updated if step.status != "completed"), None)

    def step(self, step_id):
        step = next((step for step in self.milestones if step.id == step_id), None)
        if step is None:
            raise ToolRejected("UNKNOWN_STEP", "步骤不存在，请先建立计划。")
        return step

    @staticmethod
    def fingerprint(call):
        try:
            arguments = json.loads(call.arguments)
        except ValueError:
            arguments = call.arguments
        return hashlib.sha256(json.dumps([call.name, arguments], sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def plan_action(self, call, effect):
        # 协议调用 ID 可能跨轮复用，账本 ID 仍保持唯一。
        action_id = call.id if not any(action.id == call.id for action in self.actions) else call.id + "@" + uuid.uuid4().hex[:12]
        action = ActionRecord(action_id, call.name, self.fingerprint(call), effect)
        self.actions.append(action)
        # 未决动作永不裁掉；最近结果留作模型证据。
        settled = [item for item in self.actions if item.status not in {"started", "uncertain", "planned"}]
        removable = {item.id for item in settled[:-100]}
        self.actions = [item for item in self.actions if item.id not in removable]
        return action

    def guard(self, action):
        if action.effect not in {"read", "internal"} and any(
            item.effect not in {"read", "internal"} and item.status == "uncertain" and item.id != action.id
            for item in self.actions
        ):
            raise ToolRejected("UNRESOLVED_EFFECT", "已有操作的结果未知；先读取产物或重新观察，再用 task_reconcile 核实，不能继续发送有副作用的操作。")

    def restore_actions(self):
        for action in self.actions:
            if action.status == "started":
                action.status = "uncertain"
            elif action.status == "planned":
                action.status = "not_executed"
        self.attempt += 1
        self.verification_attempts = 0

    def observed(self, action, observation, turn):
        action.status = observation.status
        if observation.error_info and observation.error_info.side_effects == "possible":
            action.status = "uncertain"
        data = observation.error or observation.result
        action.evidence = json.dumps(data, ensure_ascii=False, default=str)[:2000]
        def stable(value):
            if isinstance(value, dict):
                return {key: stable(item) for key, item in value.items() if key not in {
                    "duration_ms", "timing_ms", "completed_actions", "last_progress_turn", "no_progress_turns", "attempt", "actions"}}
            if isinstance(value, list):
                return [stable(item) for item in value]
            return value
        action.observation_hash = hashlib.sha256(json.dumps(stable(data), sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()
        if observation.status == "succeeded":
            self.completed_actions += 1
        # 成功调用不等于里程碑完成。失败与重复反馈用于检测停滞。
        prior = [item for item in self.actions if item.id != action.id and item.observation_hash == action.observation_hash
                 and item.fingerprint == action.fingerprint]
        if observation.status != "succeeded" or prior:
            self.no_progress_turns += 1
        else:
            self.last_progress_turn, self.no_progress_turns = turn, 0
