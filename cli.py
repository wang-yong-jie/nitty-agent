"""命令行入口；与 Web 工作进程共用 bootstrap 的配置和装配。"""

import argparse
from dataclasses import replace
import json
from pathlib import Path
from contextlib import ExitStack

from bootstrap import AgentOptions, PROVIDER_KEYS, ROOT, agent_session, model_settings
from contracts import AgentCancelled
from core.session import Session
from tracing import JsonlTrace


def main() -> None:
    parser = argparse.ArgumentParser(description="通用单 Agent")
    parser.add_argument("--workdir", "-C", help="默认工作目录；省略时按需创建临时目录")
    parser.add_argument("--provider", choices=list(PROVIDER_KEYS), default="deepseek")
    parser.add_argument("--model", help="模型名称；DeepSeek 默认 deepseek-flash，其他提供方必须指定")
    parser.add_argument("--base-url", help="可选的服务根地址")
    parser.add_argument("--desktop", action="store_true", help="启用 Windows 截图和鼠标键盘模式（主显示器）")
    parser.add_argument("--vision-mode", choices=["direct", "separate"], default="direct")
    parser.add_argument("--vision-provider", choices=list(PROVIDER_KEYS))
    parser.add_argument("--vision-model", help="分离模式的视觉模型名称，也可设置 VISION_MODEL")
    parser.add_argument("--vision-base-url", help="VLM 服务根地址，也可设置 VISION_BASE_URL")
    parser.add_argument("--with-local-tools", action="store_true", help="桌面模式同时提供文件和 Shell 工具")
    parser.add_argument("--max-turns", type=int, help="模型轮次上限；普通模式 10，桌面模式 50")
    parser.add_argument("--screenshot-size", type=int, default=1600, help="截图最长边，640～3840；默认 1600")
    parser.add_argument("--trace-file", help="JSONL 日志路径；新建文件，不覆盖已有文件")
    parser.add_argument("--trace-strict", action="store_true", help="日志写入失败时停止任务；需与 --trace-file 一起使用")
    parser.add_argument("--chat", action="store_true", help="连续会话；/new 新建会话，/exit 退出")
    parser.add_argument("--session-file", type=Path, help="会话历史 JSON 路径；存在时恢复，--chat 默认保存在 .agent-data/cli-session.json")
    parser.add_argument("--new-session", action="store_true", help="开始新会话，不继承已有会话文件")
    args = parser.parse_args()
    if args.trace_strict and not args.trace_file:
        parser.error("--trace-strict 需与 --trace-file 一起使用。")
    options = AgentOptions(**{key: value for key, value in vars(args).items()
                              if key not in ("trace_file", "trace_strict", "chat", "session_file", "new_session")})
    try:
        model_settings(options)
    except ValueError as error:
        parser.error(str(error))
    session_file = args.session_file or (ROOT / ".agent-data" / "cli-session.json" if args.chat else None)
    conversation = Session()
    if session_file is not None and session_file.exists() and not args.new_session:
        conversation = Session.from_dict(json.loads(session_file.read_text(encoding="utf-8")))
        if options.workdir is None and conversation.working_directory:
            options = replace(options, workdir=conversation.working_directory)

    def save_conversation(value):
        if session_file is None:
            return
        session_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = session_file.with_name(session_file.name + ".tmp")
        temporary.write_text(json.dumps(value.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(session_file)

    if args.new_session:
        save_conversation(conversation)

    try:
        task = input("请输入任务：").strip()
    except EOFError:
        return
    if not task:
        print("任务为空，已退出。")
        return
    with ExitStack() as stack:
        trace = stack.enter_context(JsonlTrace(args.trace_file)) if args.trace_file else None
        session = stack.enter_context(agent_session(options, trace=trace, trace_strict=args.trace_strict))
        session.agent.runtime.session = conversation
        session.agent.runtime.on_session_update = save_conversation
        if options.desktop:
            print("[桌面模式] 操作主显示器；F8 或鼠标移至主屏幕角落停止后续操作，终端 Ctrl+C 中止。")
            print(f"[视觉模式] {options.vision_mode}")
            print("[桌面] 操作期间请保持桌面解锁，避免同时使用鼠标键盘。")
        print(f"[工作目录] {session.environment.working_directory or '未指定，需要时自动创建临时目录'}")
        if args.chat:
            print("[会话] 输入 /new 新建会话，/exit 退出；历史自动保存。")
        while True:
            if args.chat and task == "/exit":
                break
            if args.chat and task == "/new":
                save_conversation(session.agent.new_session())
                print("[会话] 已新建会话。")
            else:
                try:
                    session.prepare()
                    answer = session.agent.run(task)
                    print(f"\n[最终回答]\n{answer}")
                except (AgentCancelled, KeyboardInterrupt):
                    print("\n[已中止] 用户停止了任务。")
                    return
                except Exception as error:
                    if not args.chat:
                        raise
                    print(f"\n[执行失败] {error}")
            if not args.chat:
                break
            try:
                task = input("继续提问：").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not task:
                break


if __name__ == "__main__":
    main()
