"""CLI 和工作进程共用的配置与装配；资源在使用它们的进程内创建和关闭。"""

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
from typing import Callable, Literal

from dotenv import load_dotenv
from openai import OpenAI

from adapters.local import LocalEnvironment
from adapters.models import ClaudeModel, OpenAICompatibleModel, TextOnlyModel
from adapters.vision import ModelVisionAdapter
from adapters.desktop_workflow import DesktopWorkflow
from adapters.task_reasoning import ModelCompactor, ModelTaskVerifier
from adapters.windows_apps import WindowsApplicationCatalog
from contracts import EventSink, ModelAdapter
from core.agent import Agent
from core.tooling import ToolRegistry
from core.skills import SkillManager, configured_catalog
from tools.applications import register_application_tools
from tools.desktop import register_desktop_tools
from tools.local import create_default_registry

ROOT = Path(__file__).resolve().parent
PROVIDER_KEYS = {"deepseek": "DEEPSEEK_API_KEY", "openai": "OPENAI_API_KEY", "claude": "ANTHROPIC_API_KEY"}


@dataclass(frozen=True)
class AgentOptions:
    provider: Literal["deepseek", "openai", "claude"] = "deepseek"
    model: str | None = None
    base_url: str | None = None
    workdir: str | None = None
    desktop: bool = False
    with_local_tools: bool = False
    vision_mode: Literal["direct", "separate"] = "direct"
    vision_provider: Literal["deepseek", "openai", "claude"] | None = None
    vision_model: str | None = None
    vision_base_url: str | None = None
    max_turns: int | None = None
    screenshot_size: int = 1600
    stability_timeout: float = 3
    desktop_recovery_limit: int = 6
    record_desktop: bool = False
    long_horizon: bool = False

    def validate(self) -> None:
        if self.provider not in PROVIDER_KEYS:
            raise ValueError("未知的模型提供方。")
        if self.with_local_tools and not self.desktop:
            raise ValueError("--with-local-tools 需与 --desktop 一起使用。")
        if self.vision_mode not in ("direct", "separate"):
            raise ValueError("未知的视觉模式。")
        if self.vision_mode == "separate" and not self.desktop:
            raise ValueError("--vision-mode separate 需与 --desktop 一起使用。")
        if self.vision_mode != "separate" and any((self.vision_provider, self.vision_model, self.vision_base_url)):
            raise ValueError("--vision-provider / --vision-model / --vision-base-url 需与 --vision-mode separate 一起使用。")
        if self.max_turns is not None and (type(self.max_turns) is not int or self.max_turns < 1):
            raise ValueError("--max-turns 必须为正整数。")
        if type(self.screenshot_size) is not int or not 640 <= self.screenshot_size <= 3840:
            raise ValueError("--screenshot-size 必须在 640 到 3840 之间。")
        if type(self.stability_timeout) not in {int, float} or not 0.5 <= self.stability_timeout <= 10:
            raise ValueError("--stability-timeout 必须在 0.5 到 10 秒之间。")
        if type(self.desktop_recovery_limit) is not int or not 3 <= self.desktop_recovery_limit <= 20:
            raise ValueError("--desktop-recovery-limit 必须在 3 到 20 之间。")
        if self.record_desktop and not self.desktop:
            raise ValueError("--record-desktop 需与 --desktop 一起使用。")
        if self.model is not None and not self.model.strip():
            raise ValueError("模型名称不能为空。")
        if not self.model and self.provider != "deepseek":
            raise ValueError("使用 OpenAI 或 Claude 时请通过 --model 指定有权限使用的模型。")


def load_settings() -> None:
    load_dotenv(ROOT / ".env")


def model_settings(options: AgentOptions) -> tuple[dict, dict | None]:
    options.validate()
    load_settings()
    key_name = PROVIDER_KEYS[options.provider]
    api_key = os.getenv(key_name)
    if not api_key:
        raise RuntimeError(f"请在环境变量或项目 .env 中填写 {key_name}。")
    main = dict(provider=options.provider, model_name=options.model or "deepseek-flash",
                api_key=api_key, base_url=options.base_url)
    vision = None
    if options.vision_mode == "separate":
        provider = options.vision_provider or os.getenv("VISION_PROVIDER") or options.provider
        if provider not in PROVIDER_KEYS:
            raise ValueError("VISION_PROVIDER 必须为 deepseek、openai 或 claude。")
        name = options.vision_model or os.getenv("VISION_MODEL")
        if not name or not name.strip():
            raise ValueError("分离模式需通过 --vision-model 或 VISION_MODEL 指定支持图片的视觉模型。")
        key_name = PROVIDER_KEYS[provider]
        key = os.getenv("VISION_API_KEY") or os.getenv(key_name)
        if not key:
            raise RuntimeError(f"请为视觉模型设置 VISION_API_KEY 或 {key_name}。")
        vision = dict(provider=provider, model_name=name, api_key=key,
                      base_url=options.vision_base_url or os.getenv("VISION_BASE_URL"))
    return main, vision


def create_model(stack: ExitStack, provider: str, model_name: str, api_key: str,
                 base_url: str | None) -> ModelAdapter:
    if provider == "claude":
        return ClaudeModel(api_key, model_name, base_url or "https://api.anthropic.com")
    client = stack.enter_context(OpenAI(
        api_key=api_key, base_url=base_url or (
            "https://api.deepseek.com" if provider == "deepseek" else "https://api.openai.com/v1"
        ), timeout=60, max_retries=1,
    ))
    return OpenAICompatibleModel(client, model_name, provider=provider,
                                 extra_body={"thinking": {"type": "disabled"}} if provider == "deepseek" else None)


@dataclass
class AgentSession:
    agent: Agent
    environment: LocalEnvironment
    desktop: object | None

    def prepare(self) -> None:
        if self.desktop is not None:
            self.desktop.screenshot()


@contextmanager
def agent_session(options: AgentOptions, *, trace: EventSink | None = None,
                  trace_strict: bool = False, cancel_check: Callable[[], None] | None = None,
                  verbose: bool = True, artifact_dir: Path | None = None):
    main_settings, vision_settings = model_settings(options)
    with ExitStack() as stack:
        model = create_model(stack, **main_settings)
        verifier = ModelVisionAdapter(model) if options.desktop else None
        vision = None
        if vision_settings:
            vision = ModelVisionAdapter(create_model(stack, **vision_settings))
            model = TextOnlyModel(model)
        desktop = None
        workflow = None
        registry = ToolRegistry() if options.desktop and not options.with_local_tools else create_default_registry()
        if options.desktop:
            from adapters.windows_desktop import WindowsDesktop

            if options.record_desktop and artifact_dir is None:
                import uuid
                artifact_dir = ROOT / ".agent-logs" / f"{uuid.uuid4().hex}.frames"
            desktop = stack.enter_context(WindowsDesktop(max_image_size=options.screenshot_size,
                stability_timeout=options.stability_timeout, recovery_limit=options.desktop_recovery_limit,
                artifact_dir=artifact_dir if options.record_desktop else None))
            workflow = DesktopWorkflow(desktop, vision or verifier)
            register_desktop_tools(registry, desktop, vision=vision, workflow=workflow)

        def check_cancelled():
            if cancel_check is not None:
                cancel_check()
            if desktop is not None:
                desktop.check_cancelled()

        environment = LocalEnvironment(options.workdir, desktop=desktop)
        if desktop is not None:
            register_application_tools(registry, WindowsApplicationCatalog(
                environment.exec_command, cancel_check=check_cancelled,
            ))
        agent = Agent(
            model, registry, environment, max_turns=options.max_turns or (200 if options.long_horizon else 50 if options.desktop else 10),
            cancel_check=check_cancelled, trace=trace, trace_strict=trace_strict, verbose=verbose,
            completion_check=workflow.finish if workflow else None, on_run_start=workflow.start if workflow else None,
            on_observation=workflow.observe if workflow else None,
            long_horizon=options.long_horizon, task_verifier=ModelTaskVerifier(model) if options.long_horizon else None,
            desktop_verifier=(lambda expected: workflow.verify(expected, scope="task").data["verification"]) if workflow else None,
            compactor=ModelCompactor(model) if options.long_horizon else None,
            snapshot_extension=workflow.snapshot if workflow else None, restore_extension=workflow.restore if workflow else None,
            skills=SkillManager(configured_catalog(ROOT)),
            run_config={
                "mode": "desktop" if options.desktop else "local",
                "vision_mode": options.vision_mode if options.desktop else None,
                "working_directory": str(environment.working_directory) if environment.working_directory else None,
                "vision_model": {"provider": vision_settings["provider"], "model": vision_settings["model_name"]}
                                if vision_settings else None,
            },
        )
        yield AgentSession(agent, environment, desktop)
