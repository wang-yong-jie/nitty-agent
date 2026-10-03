"""运行入口：配置模型与环境，把用户任务提交给 Agent API。"""

import argparse
import os
from contextlib import ExitStack
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

from agent import Agent
from contracts import AgentCancelled, ModelAdapter
from desktop_tools import register_desktop_tools
from environment import LocalEnvironment
from model import ClaudeModel, OpenAICompatibleModel, TextOnlyModel
from tools import ToolRegistry, create_default_registry
from vision import ModelVisionAdapter


PROVIDER_KEYS = {"deepseek": "DEEPSEEK_API_KEY", "openai": "OPENAI_API_KEY", "claude": "ANTHROPIC_API_KEY"}


def _create_model(
    stack: ExitStack, provider: str, model_name: str, api_key: str, base_url: str | None,
) -> ModelAdapter:
    """主模型和视觉模型独立装配；客户端生命周期统一交给入口管理。"""
    if provider == "claude":
        return ClaudeModel(api_key, model_name, base_url or "https://api.anthropic.com")
    base_url = base_url or (
        "https://api.deepseek.com" if provider == "deepseek" else "https://api.openai.com/v1"
    )
    client = stack.enter_context(OpenAI(api_key=api_key, base_url=base_url, timeout=60, max_retries=1))
    return OpenAICompatibleModel(
        client, model_name,
        # DeepSeek 扩展只用于该提供方；两个模型分别配置，不跨服务传递。
        extra_body={"thinking": {"type": "disabled"}} if provider == "deepseek" else None,
    )


def main() -> None:
    """PyCharm 直接运行此文件；默认配置仍然是 DeepSeek Flash 和本机环境。"""
    parser = argparse.ArgumentParser(description="通用单 Agent")
    parser.add_argument("--workdir", "-C", help="默认工作目录；省略时按需创建临时目录")
    parser.add_argument("--provider", choices=["deepseek", "openai", "claude"], default="deepseek")
    parser.add_argument("--model", help="模型名称；DeepSeek 默认 deepseek-flash，其他提供方必须指定")
    parser.add_argument("--base-url", help="可选的服务根地址")
    parser.add_argument("--desktop", action="store_true", help="启用 Windows 截图和鼠标键盘模式（主显示器）")
    parser.add_argument("--vision-mode", choices=["direct", "separate"], default="direct",
                        help="direct：主模型直接看图（默认）；separate：LLM 按需向独立 VLM 提问")
    parser.add_argument("--vision-provider", choices=list(PROVIDER_KEYS), help="VLM 提供方，默认与主模型相同")
    parser.add_argument("--vision-model", help="分离模式的视觉模型名称，也可设置 VISION_MODEL")
    parser.add_argument("--vision-base-url", help="VLM 服务根地址，也可设置 VISION_BASE_URL")
    parser.add_argument("--with-local-tools", action="store_true", help="桌面模式同时提供文件和 Shell 工具")
    parser.add_argument("--max-turns", type=int, help="模型轮次上限；普通模式 10，桌面模式 50")
    parser.add_argument("--screenshot-size", type=int, default=1600, help="截图最长边像素，640～3840；默认 1600")
    args = parser.parse_args()
    if args.with_local_tools and not args.desktop:
        parser.error("--with-local-tools 需与 --desktop 一起使用。")
    if args.vision_mode == "separate" and not args.desktop:
        parser.error("--vision-mode separate 需与 --desktop 一起使用。")
    if args.vision_mode != "separate" and any((args.vision_provider, args.vision_model, args.vision_base_url)):
        parser.error("--vision-provider / --vision-model / --vision-base-url 需与 --vision-mode separate 一起使用。")
    if args.max_turns is not None and args.max_turns < 1:
        parser.error("--max-turns 必须为正整数。")
    if not 640 <= args.screenshot_size <= 3840:
        parser.error("--screenshot-size 必须在 640 到 3840 之间。")
    # 按脚本位置查找密钥；已有环境变量优先。密钥只交给模型适配器。
    load_dotenv(Path(__file__).with_name(".env"))
    key_name = PROVIDER_KEYS[args.provider]
    api_key = os.getenv(key_name)
    if not api_key:
        raise RuntimeError(f"请在环境变量或项目 .env 中填写 {key_name}。")
    model_name = args.model or ("deepseek-flash" if args.provider == "deepseek" else None)
    if model_name is None:
        parser.error("使用 OpenAI 或 Claude 时请通过 --model 指定有权限使用的模型。")
    vision_settings = None
    if args.vision_mode == "separate":
        vision_provider = args.vision_provider or os.getenv("VISION_PROVIDER") or args.provider
        if vision_provider not in PROVIDER_KEYS:
            parser.error("VISION_PROVIDER 必须为 deepseek、openai 或 claude。")
        vision_name = args.vision_model or os.getenv("VISION_MODEL")
        if not vision_name or not vision_name.strip():
            parser.error("分离模式需通过 --vision-model 或 VISION_MODEL 指定支持图片的视觉模型。")
        vision_key_name = PROVIDER_KEYS[vision_provider]
        vision_key = os.getenv("VISION_API_KEY") or os.getenv(vision_key_name)
        if not vision_key:
            raise RuntimeError(f"请为视觉模型设置 VISION_API_KEY 或 {vision_key_name}。")
        vision_settings = dict(
            provider=vision_provider, model_name=vision_name, api_key=vision_key,
            base_url=args.vision_base_url or os.getenv("VISION_BASE_URL"),
        )
    task = input("请输入任务：").strip()
    if not task:
        print("任务为空，已退出。")
        return

    # 入口装配依赖；同一桌面控制器用于工具执行、环境快照和急停检查。
    with ExitStack() as stack:
        model = _create_model(stack, args.provider, model_name, api_key, args.base_url)
        vision = None
        if vision_settings is not None:
            vision = ModelVisionAdapter(_create_model(stack, **vision_settings))
            model = TextOnlyModel(model)
        desktop = None
        registry = ToolRegistry() if args.desktop and not args.with_local_tools else create_default_registry()
        if args.desktop:
            from desktop import WindowsDesktop

            desktop = stack.enter_context(WindowsDesktop(max_image_size=args.screenshot_size))
            register_desktop_tools(registry, desktop, vision=vision)
            print("[桌面模式] 操作主显示器；F8 或鼠标移至主屏幕角落停止后续操作，终端 Ctrl+C 中止。")
            if vision is None:
                print("[视觉模式] direct：截图发送给主模型，由其直接观察和决策。")
            else:
                print("[视觉模式] separate：LLM 通过 desktop_ask 按需向 VLM 提问；图片只发送给 VLM，文字观察返回 LLM。")
            print("[桌面] 操作期间请保持桌面解锁，避免同时使用鼠标键盘。")
        environment = LocalEnvironment(args.workdir, desktop=desktop)
        print(f"[工作目录] {environment.working_directory or '未指定，需要时自动创建临时目录'}")
        agent = Agent(
            model, registry, environment, max_turns=args.max_turns or (50 if args.desktop else 10),
            cancel_check=desktop.check_cancelled if desktop else None,
        )
        try:
            if desktop is not None:
                # 在首次模型请求前确认截图可用，避免对不可访问的桌面反复调用模型。
                desktop.screenshot()
            answer = agent.run(task)
        except (AgentCancelled, KeyboardInterrupt):
            print("\n[已中止] 用户停止了任务。")
            return
    print(f"\n[最终回答]\n{answer}")


if __name__ == "__main__":
    main()
