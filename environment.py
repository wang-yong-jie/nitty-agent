"""本机执行环境：提供文件系统和 Shell 能力，每个实例独立管理工作目录。"""

import base64
import os
import platform
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

from contracts import DesktopController


class LocalEnvironment:
    """默认 Environment 实现；实际操作都在这里，不依赖模型或 Agent Loop。"""

    def __init__(self, workdir: str | Path | None = None, *, desktop: DesktopController | None = None):
        # 仅用于 get_info 的桌面快照；桌面工具在注册时单独绑定控制器。
        self.desktop = desktop
        # 工作目录属于环境实例；未指定时不会提前创建临时目录。
        self.working_directory: Path | None = None
        if workdir is not None:
            directory = Path(os.path.expandvars(str(workdir))).expanduser().resolve()
            directory.mkdir(parents=True, exist_ok=True)
            self.working_directory = directory

    def get_info(self) -> dict:
        """收集真实环境供上下文使用；不会创建临时工作目录，也不注册为工具。"""
        desktop = Path.home() / "Desktop"
        if os.name == "nt":
            # 桌面可能被移动到 OneDrive，优先读取 Windows 登记的路径。
            import winreg

            try:
                with winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
                ) as key:
                    desktop = Path(os.path.expandvars(winreg.QueryValueEx(key, "Desktop")[0]))
            except OSError:
                pass  # 未登记时使用用户主目录下的 Desktop。
        info = {
            "system": platform.system(),
            "shell": "PowerShell" if os.name == "nt" else "/bin/sh",
            "home_directory": str(Path.home()),
            "desktop_directory": str(desktop),
            "working_directory": str(self.working_directory) if self.working_directory else None,
            "python_executable": sys.executable,
            "python_policy": "命令 PATH 优先使用当前 Agent 的 Python 环境，python 命令使用 python_executable。",
            "path_policy": "工作目录不是访问边界；绝对路径直接访问，相对路径按工作目录解析。未指定时按需创建并保留临时目录。桌面位置使用 desktop_directory。",
        }
        if self.desktop is not None:
            info["desktop"] = self.desktop.get_info()
        return info

    def get_working_directory(self) -> Path:
        """需要默认工作目录时才创建一次临时目录，本次任务的工具共用它。"""
        if self.working_directory is None:
            # 使用 mkdtemp 而不是自动清理的 TemporaryDirectory，保留任务产物。
            self.working_directory = Path(tempfile.mkdtemp(prefix="nitty-agent-")).resolve()
            print(f"[工作目录] 已创建临时目录：{self.working_directory}")
        return self.working_directory

    def resolve_path(self, path: str) -> Path:
        """绝对路径直接访问；相对路径按工作目录解析，也允许 .. 访问其他目录。"""
        target = Path(os.path.expandvars(path)).expanduser()
        if not target.is_absolute():
            target = self.get_working_directory() / target
        return target.resolve()

    def read_file(self, path: str) -> dict:
        """读取 UTF-8 文本，限制返回长度，避免一次占用过多模型上下文。"""
        with self.resolve_path(path).open(encoding="utf-8") as file:
            content = file.read(20001)
        return {"content": content[:20000], "truncated": len(content) > 20000}

    def write_file(self, path: str, content: str) -> str:
        """写入 UTF-8 文本；父目录不存在时创建，文件存在时覆盖。"""
        target = self.resolve_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"已写入 {target}"

    def exec_command(self, cmd: str, workdir: str | None = None, timeout: int = 30) -> dict:
        """执行一条 Shell 命令，支持创建目录、列目录、运行程序和测试。"""
        if not cmd.strip():
            raise ValueError("命令不能为空。")
        if type(timeout) is not int or not 1 <= timeout <= 300:
            raise ValueError("timeout 必须是 1 到 300 之间的整数秒数。")
        # 单次调用可以指定已有目录，不改变其他工具的默认工作目录。
        # 绝对 workdir 不需要临时目录；省略时按需创建并复用默认目录。
        directory = self.resolve_path(workdir) if workdir is not None else self.get_working_directory()

        # PyCharm 直接启动解释器时可能没有激活 Conda。
        # 将当前解释器所在环境放到 PATH 前面，让 python 和 pip 使用同一环境。
        environment = os.environ.copy()
        environment["PYTHONIOENCODING"] = "utf-8"
        environment["PYTHONUTF8"] = "1"
        prefix = Path(sys.prefix)
        paths = [Path(sys.executable).parent]
        if os.name == "nt":
            paths += [prefix / "Scripts", prefix / "Library" / "bin"]
        environment["PATH"] = os.pathsep.join(map(str, paths)) + os.pathsep + environment.get("PATH", "")
        if (prefix / "conda-meta").is_dir():
            environment["CONDA_PREFIX"] = str(prefix)
            environment["CONDA_DEFAULT_ENV"] = prefix.name

        if os.name == "nt":
            # Windows 固定使用 PowerShell；设置 UTF-8，避免中文输出乱码。
            # Cmdlet 报错应终止；运行外部程序时保留最后一个程序的退出码。
            script = (
                "$ErrorActionPreference = 'Stop'\n"
                # 禁用模块加载的进度信息，避免 PowerShell 向 stderr 写入 CLIXML。
                "$ProgressPreference = 'SilentlyContinue'\n"
                "$OutputEncoding = [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)\n"
                "$LASTEXITCODE = 0\n"
                + cmd + "\nexit $LASTEXITCODE\n"
            )
            # 编码传参避免多行命令、中文和引号被启动参数重新解释。
            encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
            shell = str(Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe")
            command = [shell, "-NoLogo", "-NoProfile", "-NonInteractive", "-OutputFormat", "Text",
                       "-EncodedCommand", encoded]
        else:
            command = ["/bin/sh", "-c", cmd]

        # 每次调用启动新 Shell；cd 和变量不会保留到下一次调用。
        # Windows 隐藏进程窗口；其他系统用独立进程组，便于超时后停止子进程。
        process = subprocess.Popen(
            command,
            cwd=directory,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            start_new_session=os.name != "nt",
        )
        timed_out = False
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            # 停止整个进程树，避免只杀死 Shell 却留下正在运行的 Python 等程序。
            if os.name == "nt":
                subprocess.run(
                    [str(Path(os.environ["SystemRoot"]) / "System32/taskkill.exe"),
                     "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW,
                )
            else:
                os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()

        # 非零退出码、超时和截断都明确返回，供模型决定是否修正或继续。
        return {
            "exit_code": process.returncode,
            "stdout": stdout[:20000],
            "stderr": stderr[:20000],
            "timed_out": timed_out,
            "truncated": len(stdout) > 20000 or len(stderr) > 20000,
            "working_directory": str(directory),
        }
