"""文件和 Shell 工具定义；操作委托给注入的 Environment。"""

from core.tooling import ToolRegistry


def create_default_registry() -> ToolRegistry:
    """创建默认的三个工具；委托环境执行，没有本机路径或 Shell 逻辑。"""
    registry = ToolRegistry()
    registry.register(
        "exec_command", "执行 Shell 命令：创建目录、列目录、运行程序和测试；Shell 由环境信息指定。",
        {
            "cmd": {"type": "string", "description": "需要执行的命令，支持多行"},
            "workdir": {"type": "string", "description": "可选的已有执行目录；省略时使用默认工作目录"},
            "timeout": {"type": "integer", "description": "超时秒数，默认 30，范围 1 到 300", "minimum": 1, "maximum": 300},
        }, ["cmd"],
        lambda environment, **arguments: environment.exec_command(**arguments),
        instructions=(
            "目录创建、列目录、运行命令行程序和测试使用 exec_command。"
            "同时提供桌面能力时，打开图形应用优先遵循桌面操作规则，Shell 仅用于辅助查询。"
            "根据环境信息中的 shell 编写命令；Windows 使用 PowerShell 语法。"
            "执行位置和 Python 解释器以环境信息为准，安装依赖使用 python -m pip。"
            "每次调用都是新 Shell，cd 和变量不会跨调用保留；需要时传入 workdir。"
            "workdir 只影响该次调用，不改变文件工具的默认目录。"
            "检查退出码、timed_out 和截断标记，未成功时不要声称操作完成。"
            "timed_out=true 即使 exit_code=0 也表示执行未完成；空输出不等于目标不存在。"
            "只能陈述实际检查过的范围；部分路径不存在不能推断应用未安装，搜索超时不能声称全盘检查完毕。"
            "工具已分别捕获 stdout 和 stderr，PowerShell 运行 Python 时无需用 2>&1 合并输出。"
        ),
    )
    registry.register(
        "read_file", "读取 UTF-8 文本文件，最多返回 20000 个字符并标记截断。",
        {"path": {"type": "string", "description": "绝对路径或相对于工作目录的路径"}}, ["path"],
        lambda environment, **arguments: environment.read_file(**arguments),
        instructions="读取文本使用 read_file；读取被截断时不要根据部分内容覆盖原文件。",
    )
    registry.register(
        "write_file", "创建或覆盖 UTF-8 文本文件。编辑已有文件前先读取内容。",
        {
            "path": {"type": "string", "description": "绝对路径或相对于工作目录的路径"},
            "content": {"type": "string", "description": "要写入的完整文件内容"},
        }, ["path", "content"],
        lambda environment, **arguments: environment.write_file(**arguments),
        instructions="编辑已有文件前先读取，再用 write_file 写入完整内容；写完代码后按需运行验证。",
        sensitive_parameters=("content",),
    )
    return registry
