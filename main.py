"""运行入口：配置模型与环境，把用户任务提交给 Agent API。"""

import argparse
import os
from contextlib import ExitStack
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

from agent import Agent
from environment import LocalEnvironment
from model import ClaudeModel, OpenAICompatibleModel
from tools import create_default_registry


def main() -> None:
    """PyCharm 直接运行此文件；默认配置仍然是 DeepSeek Flash 和本机环境。"""
    parser = argparse.ArgumentParser(description="通用单 Agent")
    parser.add_argument("--workdir", "-C", help="默认工作目录；省略时按需创建临时目录")
    parser.add_argument("--provider", choices=["deepseek", "openai", "claude"], default="deepseek")
    parser.add_argument("--model", help="模型名称；DeepSeek 默认 deepseek-flash，其他提供方必须指定")
    parser.add_argument("--base-url", help="可选的服务根地址")
    args = parser.parse_args()
    # 按脚本位置查找密钥；已有环境变量优先。密钥只交给模型适配器。
    load_dotenv(Path(__file__).with_name(".env"))
    key_names = {"deepseek": "DEEPSEEK_API_KEY", "openai": "OPENAI_API_KEY", "claude": "ANTHROPIC_API_KEY"}
    key_name = key_names[args.provider]
    api_key = os.getenv(key_name)
    if not api_key:
        raise RuntimeError(f"请在环境变量或项目 .env 中填写 {key_name}。")
    model_name = args.model or ("deepseek-flash" if args.provider == "deepseek" else None)
    if model_name is None:
        parser.error("使用 OpenAI 或 Claude 时请通过 --model 指定有权限使用的模型。")
    task = input("请输入任务：").strip()
    if not task:
        print("任务为空，已退出。")
        return

    # 入口只负责装配依赖；同一个环境实例同时提供环境快照与真实操作。
    environment = LocalEnvironment(args.workdir)
    print(f"[工作目录] {environment.working_directory or '未指定，需要时自动创建临时目录'}")
    with ExitStack() as stack:
        if args.provider == "claude":
            model = ClaudeModel(api_key, model_name, args.base_url or "https://api.anthropic.com")
        else:
            base_url = args.base_url or (
                "https://api.deepseek.com" if args.provider == "deepseek" else "https://api.openai.com/v1"
            )
            client = stack.enter_context(OpenAI(api_key=api_key, base_url=base_url, timeout=60, max_retries=1))
            model = OpenAICompatibleModel(
                client, model_name,
                # DeepSeek 扩展仅放进该提供方的配置，不会发送给其他服务。
                extra_body={"thinking": {"type": "disabled"}} if args.provider == "deepseek" else None,
            )
        agent = Agent(model, create_default_registry(), environment)
        answer = agent.run(task)
    print(f"\n[最终回答]\n{answer}")


if __name__ == "__main__":
    main()
