"""本地 Skill 目录、按任务激活和资源读取；发现文件不会执行代码。"""

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re

import yaml

from contracts import ToolRejected


NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
MAX_SKILLS = 100
MAX_SKILL_BYTES = 32_000
MAX_RESOURCE_BYTES = 256_000
MAX_PACKAGE_BYTES = 2_000_000
MAX_FILES = 100
MAX_ACTIVE_CHARS = 40_000
MAX_CATALOG_CHARS = 24_000
CONTROL_TOOLS = ("activate_skill", "read_skill_resource", "deactivate_skill")


class SkillFormatError(ValueError):
    pass


class UniqueSafeLoader(yaml.SafeLoader):
    """安全 YAML，同时拒绝重复键，避免配置含义取决于解析器。"""


def _mapping(loader, node):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if not isinstance(key, str) or key in result:
            raise SkillFormatError("YAML 字段必须是唯一字符串。")
        result[key] = loader.construct_object(value_node)
    return result


UniqueSafeLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def _read_bytes(path: Path, limit: int) -> bytes:
    if not path.is_file():
        raise SkillFormatError(f"资源必须是普通文件：{path.name}")
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise SkillFormatError(f"文件超过 {limit} 字节：{path.name}")
    return data


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    instructions: str
    location: str
    version: str
    compatibility: str
    required_tools: tuple[str, ...]
    files: tuple[tuple[str, str], ...]
    sha256: str

    def record(self) -> dict:
        return {"name": self.name, "version": self.version, "location": self.location, "sha256": self.sha256}


def load_skill(directory: Path) -> Skill:
    """读取有界的包快照；正文与资源哈希固定，资源内容仍按需提供给模型。"""
    if directory.is_symlink() or directory.is_junction():
        raise SkillFormatError("Skill 目录不能是符号链接或目录联接。")
    directory = directory.resolve()
    location = directory / "SKILL.md"
    if location.is_symlink():
        raise SkillFormatError("SKILL.md 不能是符号链接。")
    raw = _read_bytes(location, MAX_SKILL_BYTES)
    text = raw.decode("utf-8-sig")
    parts = re.split(r"(?m)^---\s*$", text, maxsplit=2)
    if len(parts) != 3 or parts[0].strip():
        raise SkillFormatError("SKILL.md 必须以 YAML frontmatter 开头。")
    try:
        meta = yaml.load(parts[1], Loader=UniqueSafeLoader)
    except RecursionError as error:
        raise SkillFormatError("YAML 嵌套过深。") from error
    if not isinstance(meta, dict):
        raise SkillFormatError("Skill 元数据必须是对象。")
    name, description, body = meta.get("name"), meta.get("description"), parts[2].strip()
    if not isinstance(name, str) or len(name) > 64 or not NAME.fullmatch(name) or name != directory.name:
        raise SkillFormatError("name 必须是与目录同名的 1～64 位小写字母、数字和单连字符。")
    if not isinstance(description, str) or not 1 <= len(description.strip()) <= 1024 or not body:
        raise SkillFormatError("description 必须是 1～1024 字符的文本，正文不能为空。")
    metadata = meta.get("metadata", {})
    if not isinstance(metadata, dict) or any(not isinstance(v, str) for v in metadata.values()):
        raise SkillFormatError("metadata 必须是字符串键值映射；版本号请加引号。")
    compatibility = meta.get("compatibility", "")
    if not isinstance(compatibility, str) or len(compatibility) > 500:
        raise SkillFormatError("compatibility 必须是不超过 500 字符的文本。")
    # 项目扩展：机器可检查的工具依赖；compatibility 仍保留为人类可读说明。
    required = tuple(dict.fromkeys(re.split(r"[\s,]+", metadata.get("nitty-required-tools", "").strip())))
    required = tuple(item for item in required if item)
    files = {"SKILL.md": _sha(raw)}
    total = len(raw)
    directories = 0
    for root, dirs, names in os.walk(directory, followlinks=False):
        directories += 1
        if directories > 100:
            raise SkillFormatError("Skill 包超过 100 个资源目录。")
        base = Path(root)
        if len(dirs) + len(names) > 200:
            raise SkillFormatError("Skill 资源目录单层超过 200 项。")
        dirs[:] = sorted(d for d in dirs if d not in {".git", "node_modules", "__pycache__"})
        if len(base.relative_to(directory).parts) > 5:
            raise SkillFormatError("Skill 资源目录超过五层。")
        for entry in [*(base / d for d in dirs), *(base / n for n in names)]:
            if entry.is_symlink() or entry.is_junction() or not entry.resolve().is_relative_to(directory):
                raise SkillFormatError("Skill 包不能包含符号链接或越界资源。")
        for filename in sorted(names):
            path = base / filename
            relative = path.relative_to(directory).as_posix()
            if relative == "SKILL.md" or filename.endswith(".pyc"):
                continue
            if len(files) >= MAX_FILES:
                raise SkillFormatError(f"Skill 包超过 {MAX_FILES} 个文件。")
            data = _read_bytes(path, MAX_RESOURCE_BYTES)
            total += len(data)
            if total > MAX_PACKAGE_BYTES:
                raise SkillFormatError("Skill 包超过 2 MB；请拆分资源。")
            files[relative] = _sha(data)
    digest = _sha(json.dumps(files, sort_keys=True, ensure_ascii=False).encode("utf-8"))
    return Skill(name, description.strip(), body, str(location), metadata.get("version", "unversioned"),
                 compatibility, required, tuple(sorted(files.items())), digest)


class SkillCatalog:
    """仅扫描显式配置的根目录的一级子目录；后一个根目录优先。"""

    def __init__(self, roots=(), disabled=()):
        self.roots = tuple(dict.fromkeys(Path(root).expanduser().resolve() for root in roots))
        self.disabled = frozenset(disabled)
        self.skills: dict[str, Skill] = {}
        self.diagnostics: list[dict] = []

    def discover(self) -> None:
        self.skills, self.diagnostics = {}, []
        count = 0
        examined = 0
        for root in self.roots:
            if not root.exists():
                continue
            try:
                # 不先 list 整个目录，扫描工作量与上下文都保持有界。
                with os.scandir(root) as entries:
                    candidates = []
                    for entry in entries:
                        examined += 1
                        if examined > 2000:
                            raise SkillFormatError("Skill 根目录扫描超过 2000 项。")
                        if entry.name.startswith(".") or not entry.is_dir(follow_symlinks=False):
                            continue
                        count += 1
                        if count > MAX_SKILLS:
                            raise SkillFormatError(f"配置目录共超过 {MAX_SKILLS} 个候选 Skill。")
                        candidates.append(Path(entry.path))
                for path in sorted(candidates):
                    if path.name in self.disabled or not (path / "SKILL.md").exists():
                        continue
                    # 高优先级损坏的同名包不能悄悄退回低优先级版本。
                    previous = self.skills.pop(path.name, None)
                    try:
                        skill = load_skill(path)
                        if previous:
                            self.diagnostics.append({"code": "SKILL_SHADOWED", "name": skill.name,
                                                     "location": previous.location, "replacement": skill.location})
                        self.skills[skill.name] = skill
                    except (OSError, UnicodeError, ValueError, yaml.YAMLError) as error:
                        self.diagnostics.append({"code": "SKILL_INVALID", "location": str(path), "message": str(error)})
            except (OSError, ValueError) as error:
                self.diagnostics.append({"code": "SKILL_DISCOVERY_FAILED", "location": str(root), "message": str(error)})
                if count > MAX_SKILLS or examined > 2000:
                    self.skills.clear()
                    return


class SkillManager:
    """每个 Runtime 独立使用；激活记录属于 Run，不继承上个 Run 的指导。"""

    def __init__(self, catalog: SkillCatalog):
        self.catalog = catalog
        self.state = None
        self.available: dict[str, Skill] = {}
        self.emit = lambda *_args, **_kwargs: None
        self._owned_registry = None
        self._owned_tools = {}

    def prepare(self, state, registry, *, restoring=False, emit=None) -> None:
        self.state = state
        self.emit = emit or (lambda *_args, **_kwargs: None)
        self.catalog.discover()
        # 只有本管理器注册的控制工具可以撤销；不覆盖调用方的同名工具。
        if self._owned_registry is not None:
            if registry is not self._owned_registry:
                raise ValueError("SkillManager 不能同时绑定多个工具注册表。")
            for name in CONTROL_TOOLS:
                if registry.get(name) is not self._owned_tools[name]:
                    raise ValueError("Skill 控制工具已被外部替换，不能覆盖调用方工具。")
            for name in CONTROL_TOOLS:
                registry.unregister(name)
            self._owned_registry = None
            self._owned_tools = {}
        tool_names = {spec.name for spec in registry.specs()}
        self.available = {}
        for name, skill in self.catalog.skills.items():
            missing = sorted(set(skill.required_tools) - tool_names)
            if missing:
                self.emit("skill_unavailable", name=name, missing_tools=missing)
            else:
                self.available[name] = skill
        for diagnostic in self.catalog.diagnostics:
            self.emit("skill_diagnostic", **diagnostic)
        catalog_chars = len(json.dumps(self.catalog_data(), ensure_ascii=False))
        if catalog_chars > MAX_CATALOG_CHARS:
            raise ToolRejected("SKILL_CATALOG_LIMIT", "Skill 目录超出上下文预算，请禁用不需要的 Skill。")
        if restoring:
            self._validate_restore()
            for record in state.active_skills:
                self.emit("skill_restored", **record)
        elif state.active_skills or state.skill_resources:
            raise ValueError("新任务不能继承激活状态。")
        if self.available:
            if set(CONTROL_TOOLS) & tool_names:
                raise ValueError("Skill 控制工具名称与现有工具冲突。")
            self._register(registry)
            self._owned_registry = registry
            self._owned_tools = {name: registry.get(name) for name in CONTROL_TOOLS}
        self.emit("skills_discovered", available=list(self.available), diagnostics=len(self.catalog.diagnostics),
                  tools=[spec.name for spec in registry.specs()])
        # 显式指令只识别任务开头的独立行，避免把脚本中的 $变量 当作 Skill。
        if not restoring:
            for line in state.task.splitlines():
                if not line.strip().startswith("/skill "):
                    break
                name = line.strip()[7:].strip()
                if not NAME.fullmatch(name):
                    raise ToolRejected("INVALID_SKILL_DIRECTIVE", "使用独立行 /skill skill-name 指定 Skill。")
                self.activate(name, reason="user")

    def catalog_data(self) -> list[dict]:
        return [{"name": s.name, "description": s.description} for s in self.available.values()]

    def _skill(self, name: str) -> Skill:
        if name not in self.available:
            skill = self.catalog.skills.get(name)
            if skill:
                raise ToolRejected("SKILL_DEPENDENCY_MISSING", f"Skill {name} 的工具依赖不可用：{', '.join(skill.required_tools)}")
            raise ToolRejected("SKILL_NOT_FOUND", f"Skill 不存在、已禁用或格式无效：{name}")
        return self.available[name]

    def _register(self, registry) -> None:
        names = {"name": {"type": "string", "enum": list(self.available)}}
        registry.register("activate_skill", "激活任务 Skill；完整指导在下一轮上下文提供，激活后再决定操作。",
                          names, ["name"], lambda _env, name: self.activate(name), effect="internal",
                          requires_single_call=True)
        registry.register("read_skill_resource", "读取已激活 Skill 内的 UTF-8 文本资源；不执行脚本。offset/limit 为字符数。",
                          {**names, "path": {"type": "string", "minLength": 1, "maxLength": 1024},
                           "offset": {"type": "integer", "minimum": 0},
                           "limit": {"type": "integer", "minimum": 1, "maximum": 12000}},
                          ["name", "path"], lambda _env, **args: self.read_resource(**args), effect="read")
        registry.register("deactivate_skill", "当前阶段不再需要 Skill 时移除指导；不撤销已执行操作。",
                          names, ["name"], lambda _env, name: self.deactivate(name), effect="internal",
                          requires_single_call=True)

    def activate(self, name: str, *, reason="model") -> dict:
        skill = self._skill(name)
        if any(r["name"] == name for r in self.state.active_skills):
            return {**skill.record(), "already_active": True}
        if len(self.state.active_skills) >= 8:
            raise ToolRejected("SKILL_CONTEXT_LIMIT", "同时最多激活八个 Skill。")
        if sum(len(self.available[r["name"]].instructions) for r in self.state.active_skills) + len(skill.instructions) > MAX_ACTIVE_CHARS:
            raise ToolRejected("SKILL_CONTEXT_LIMIT", "Skill 正文超出 40000 字符预算，请先停用无关 Skill。")
        self._check_package(skill)
        self.emit("skill_activated", **skill.record(), reason=reason)
        self.state.active_skills.append(skill.record())
        return {**skill.record(), "already_active": False, "directory": str(Path(skill.location).parent),
                "resources": [p for p, _ in skill.files if p != "SKILL.md"]}

    def deactivate(self, name: str) -> dict:
        self._skill(name)
        self.emit("skill_deactivated", name=name)
        self.state.active_skills[:] = [r for r in self.state.active_skills if r["name"] != name]
        self.state.skill_resources[:] = [r for r in self.state.skill_resources if r["skill"] != name]
        return {"name": name, "active": False}

    @staticmethod
    def _check_package(skill: Skill) -> None:
        try:
            current = load_skill(Path(skill.location).parent)
        except (OSError, UnicodeError, ValueError, yaml.YAMLError) as error:
            raise ToolRejected("SKILL_CHANGED", f"Skill 包无法核对：{skill.name}；{error}") from error
        if current.sha256 != skill.sha256 or current.location != skill.location:
            raise ToolRejected("SKILL_CHANGED", f"Skill 包已变化：{skill.name}；请开始新任务。")

    def read_resource(self, name: str, path: str, offset=0, limit=12000) -> dict:
        skill = self._skill(name)
        if not any(r["name"] == name for r in self.state.active_skills):
            raise ToolRejected("SKILL_NOT_ACTIVE", "请先激活 Skill 再读取资源。")
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 12000:
            raise ToolRejected("SKILL_RESOURCE_RANGE", "offset/limit 超出允许范围。")
        root = Path(skill.location).parent
        candidate = Path(path)
        resolved = (root / candidate).resolve()
        if candidate.is_absolute() or candidate.drive or not resolved.is_relative_to(root):
            raise ToolRejected("SKILL_RESOURCE_PATH", "资源必须是 Skill 目录内的相对路径。")
        for entry in [root, root / candidate, *(root / candidate).parents]:
            if entry == root.parent:
                break
            if entry.is_symlink() or entry.is_junction():
                raise ToolRejected("SKILL_RESOURCE_PATH", "资源路径不能包含符号链接或目录联接。")
        relative = resolved.relative_to(root).as_posix()
        expected = dict(skill.files).get(relative)
        if expected is None or relative == "SKILL.md":
            raise ToolRejected("SKILL_RESOURCE_PATH", "资源不在当前任务的包清单中。")
        try:
            data = _read_bytes(resolved, MAX_RESOURCE_BYTES)
        except (OSError, ValueError) as error:
            raise ToolRejected("SKILL_RESOURCE_CHANGED", f"资源无法读取：{relative}") from error
        if _sha(data) != expected:
            raise ToolRejected("SKILL_RESOURCE_CHANGED", "资源已变化，请开始新任务后重新加载。")
        try:
            content = data.decode("utf-8-sig")
        except UnicodeError as error:
            raise ToolRejected("SKILL_RESOURCE_BINARY", "该资源不是 UTF-8 文本；此工具不读取二进制模板。") from error
        record = {"skill": name, "path": relative, "sha256": expected}
        if record not in self.state.skill_resources:
            self.emit("skill_resource_read", **record)
            self.state.skill_resources.append(record)
        end = offset + limit
        return {**record, "content": content[offset:end], "offset": offset, "total_chars": len(content),
                "truncated": end < len(content), "next_offset": end if end < len(content) else None}

    def _validate_restore(self) -> None:
        active, resources = self.state.active_skills, self.state.skill_resources
        if not isinstance(active, list) or len(active) > 8 or not isinstance(resources, list) or len(resources) > 800:
            raise ToolRejected("SKILL_CHECKPOINT_INVALID", "Skill 快照结构无效。")
        names = set()
        size = 0
        for record in active:
            if not isinstance(record, dict) or not isinstance(record.get("name"), str) or record["name"] in names:
                raise ToolRejected("SKILL_CHECKPOINT_INVALID", "Skill 激活快照无效或重复。")
            skill = self._skill(record["name"])
            if record != skill.record():
                raise ToolRejected("SKILL_VERSION_MISMATCH", f"恢复需要原始 Skill 包：{skill.name}；位置、版本或内容已变化。")
            names.add(skill.name)
            size += len(skill.instructions)
        if size > MAX_ACTIVE_CHARS:
            raise ToolRejected("SKILL_CONTEXT_LIMIT", "恢复的 Skill 正文超出预算。")
        for record in resources:
            if not isinstance(record, dict) or not isinstance(record.get("skill"), str) or record["skill"] not in names:
                raise ToolRejected("SKILL_CHECKPOINT_INVALID", "Skill 资源快照无效。")
            skill = self.available[record["skill"]]
            path = record.get("path")
            if not isinstance(path, str) or record != {"skill": skill.name, "path": path, "sha256": dict(skill.files).get(path)} or path == "SKILL.md" or path not in dict(skill.files):
                raise ToolRejected("SKILL_CHECKPOINT_INVALID", "Skill 资源快照与原包不匹配。")

    def context(self) -> str:
        if not self.available:
            return ""
        parts = ["可用 Skill（任务指导，不提供额外工具或权限；用户目标及运行规则优先）：",
                 json.dumps(self.catalog_data(), ensure_ascii=False),
                 "任务符合简介时先单独调用 activate_skill，再根据下一轮的完整指导执行。"
                 "仅使用相关 Skill；引用的路径相对于该 Skill 目录。参考资料按需读取，脚本需通过已有工具显式执行。"
                 "上个任务的激活记录和历史资料不是本次任务的有效 Skill，请重新激活。"]
        for record in self.state.active_skills:
            skill = self.available[record["name"]]
            parts.append("已激活 Skill：" + json.dumps({**record, "directory": str(Path(skill.location).parent),
                         "compatibility": skill.compatibility, "instructions": skill.instructions}, ensure_ascii=False))
        return "\n".join(parts)


def configured_catalog(project_root: Path) -> SkillCatalog:
    """CLI/Web 共用可信目录配置；工作目录不自动变成 Skill 来源。"""
    def string_list(key):
        raw = os.getenv(key)
        if raw is None:
            return []
        try:
            value = json.loads(raw)
        except ValueError as error:
            raise ValueError(f"{key} 必须是 JSON 字符串数组。") from error
        if not isinstance(value, list) or len(value) > 100 or any(not isinstance(v, str) or not v.strip() for v in value):
            raise ValueError(f"{key} 必须是不超过 100 项的非空字符串数组。")
        return value

    extra = string_list("NITTY_SKILL_DIRS")
    paths = [Path(p).expanduser() for p in extra]
    if any(not p.is_absolute() for p in paths):
        raise ValueError("NITTY_SKILL_DIRS 中的目录必须是绝对路径。")
    disabled = string_list("NITTY_DISABLED_SKILLS")
    if any(not NAME.fullmatch(name) or len(name) > 64 for name in disabled):
        raise ValueError("NITTY_DISABLED_SKILLS 中包含非法名称。")
    roots = []
    try:
        roots.append(Path.home() / ".nitty-agent" / "skills")
    except (RuntimeError, OSError):
        pass  # 服务账户未提供 home 时，项目目录与显式目录仍然可用。
    return SkillCatalog([*roots, project_root / ".agents" / "skills", *paths], disabled)
