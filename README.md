# 通用单 Agent

使用 Python 3.12。默认模型为 DeepSeek `deepseek-flash`，接口采用 OpenAI 兼容格式。
架构为 **单 Agent + 模块化 Runtime + 可插拔 Model / Tool / Environment**。

## 在 PyCharm 中运行

1. 打开本项目，解释器选择已有 Conda 环境 `agent`：
   `C:\Users\wangy\miniconda3\envs\agent\python.exe`。
2. 安装依赖：

   ```powershell
   conda run -n agent python -m pip install -r requirements.txt
   ```

3. 本机的 `.env` 已配置 DeepSeek 密钥。其他电脑复制 `.env.example` 为 `.env`，
   填写 `DEEPSEEK_API_KEY`。文件已被 Git 忽略，已有环境变量优先。
4. 右键运行 `main.py`，输入任务。
   如需指定工作目录，在“运行 → 编辑配置 → 脚本参数”中填写
   `--workdir "C:\Users\wangy\Desktop"`。

终端运行：

```powershell
conda run --no-capture-output -n agent python main.py
conda run --no-capture-output -n agent python main.py --workdir "C:\Users\wangy\Desktop"
```

PyCharm 选择根目录的 `main.py` 作为脚本，项目根目录作为工作目录；
无需额外配置 `PYTHONPATH` 或将子目录标记为 Sources Root。
`.env` 始终从项目根目录读取，不随工作目录变化；日志相对路径仍按启动目录解析。

## 目录结构与依赖方向

```text
nitty-agent/
├── main.py                    # PyCharm / 终端启动入口
├── cli.py                     # 参数、配置与依赖装配
├── contracts.py               # 共享接口和消息类型
├── validation.py              # 工具与适配器共享的校验
├── tracing.py                 # 日志、脱敏与追踪输出
├── core/
│   ├── agent.py               # 对外调用 API
│   ├── runtime.py             # Agent Loop
│   ├── context.py             # 上下文组装
│   ├── state.py               # 运行状态
│   └── tooling.py             # 通用工具注册与执行
├── adapters/
│   ├── models.py              # OpenAI 兼容、Claude 与文本模型包装
│   ├── vision.py              # 按需视觉问答
│   ├── local.py               # 本机文件与 Shell 环境
│   ├── windows_desktop.py     # Windows 截图与输入
│   └── windows_apps.py        # Windows 应用查询
├── tools/
│   ├── local.py               # 文件 / Shell 工具定义
│   ├── desktop.py             # 桌面工具定义
│   └── applications.py        # 应用查询工具定义
├── tests/                     # 离线测试；共享替身位于 fakes.py
├── requirements.txt
├── .env.example
└── AGENTS.md
```

业务目录直接放在项目根目录。`core` 负责通用决策循环，`tools` 定义暴露给模型的能力，
`adapters` 实现外部系统的具体操作。工具通过 `contracts.py` 中的接口接受依赖；
模型和 Windows 实现由 `cli.py` 或 Python 调用方装配。
`core` 与 `tools` 不导入具体适配器；共享校验放在 `validation.py`。
日志和接口各自保留一个共享模块。测试替身集中管理，测试模块之间不相互导入。

在项目根目录调用 Python API，使用 `from core.agent import Agent`，其他导入见下文示例。
程序通过 `python main.py ...` 启动。

## 架构与文件职责

```text
User → Agent.run(task)
           │
           ▼
     AgentRuntime
       ├── Agent Loop
       ├── AgentState / ContextBuilder
       ├── ModelAdapter → DeepSeek / OpenAI / Claude
       └── ToolRegistry → ToolExecutor
                              ├── Environment（文件 / Shell）
                              ├── DesktopController（桌面）
                              └── ApplicationCatalog（只读应用查询）
                     desktop_ask → VisionAdapter → VLM（分离模式）
                    Observation → 状态 → 下一轮决策
```

| 层 | 文件 | 职责 |
| --- | --- | --- |
| Agent API | `core/agent.py` | `Agent.run(task)` 提交任务，返回最终回答 |
| Runtime / Loop | `core/runtime.py` | 协调上下文、模型决策、工具执行和循环结束 |
| State | `core/state.py` | 保存任务、历史、轮次、Observation、状态和最终回答 |
| Context | `core/context.py` | 收集工具附带的使用规则，将指令、环境快照与历史组装为模型输入 |
| Model Adapter | `adapters/models.py` | 转换 OpenAI 兼容或 Claude 原生协议 |
| Vision Adapter | `adapters/vision.py` | 根据具体问题解释截图，校验结构化回答和目标坐标 |
| Tool Registry / Executor | `core/tooling.py` | 注册描述、使用规则和处理函数，解析参数，调度并反馈错误 |
| 本机工具定义 | `tools/local.py` | 将文件和 Shell 能力注册为工具 |
| Environment | `adapters/local.py` | `LocalEnvironment` 负责本机路径、文件和 Shell 操作 |
| Desktop | `adapters/windows_desktop.py` / `tools/desktop.py` | Windows 截图、输入、坐标映射与桌面工具注册 |
| Applications | `adapters/windows_apps.py` / `tools/applications.py` | 查询 Windows 应用目录，返回候选、来源和不完整检查信息 |
| Tracing | `tracing.py` | 控制台参数/耗时日志、脱敏及可选 JSONL 事件文件 |
| 公共接口与数据 | `contracts.py` | 定义模型/环境接口及消息、调用、Observation |
| 共享校验 | `validation.py` | 校验应用名称、视觉问题和视觉结果 |
| 配置入口 | `main.py` → `cli.py` | 启动、读取配置和输入，将各模块装配起来 |

Runtime 中没有厂商 SDK、`subprocess` 或文件读写代码。
模块通过统一数据类型交互，模型看不到处理函数或环境对象，只获得描述和真实反馈。

环境信息的流向是：
`Environment.get_info() → ContextBuilder → ModelAdapter`。
每轮重新读取快照；首次创建临时目录后，下一轮模型能看到实际路径。
工具执行结果的流向是：
`ToolExecutor → Observation → AgentState.messages → ContextBuilder → ModelAdapter`。

## 三个默认工具

- `exec_command(cmd, workdir=None, timeout=30)`：创建目录、列目录、运行程序、安装依赖和测试。
- `read_file(path)`：读取 UTF-8 文本，最多返回 20000 个字符并标记截断。
- `write_file(path, content)`：创建或覆盖 UTF-8 文件，编辑已有文件前先读取。

工具实现只委托给注入的 Environment。本机环境在 Windows 使用 PowerShell，在其他系统使用 `/bin/sh`。
Windows 下，下面这些操作均通过 `exec_command` 执行：

```powershell
New-Item -ItemType Directory -Force -Path './work'
Get-ChildItem -LiteralPath '.'
python -c "print(sum(range(1, 101)))"
python -m unittest discover -s tests -t . -v
```

每次调用启动新的 Shell，变量和 `cd` 不会跨调用保留。
命令返回 `exit_code`、`stdout`、`stderr`、`working_directory`、`timed_out` 和 `truncated`。
stdout、stderr 各最多返回 20000 个字符；默认超时 30 秒，可设置为 1～300 秒。
超时会停止命令及其子进程并保留已产生的输出。
本机命令的 PATH 优先使用启动 Agent 的 Python 环境，因此 PyCharm 直接运行时，
`python` 也会使用 `agent` 解释器。安装依赖使用 `python -m pip`。

## 工作目录与访问范围

- `--workdir`（简写 `-C`）指定默认目录，不存在时创建。
- 未指定：首次使用相对文件路径，或执行命令且未提供绝对 `workdir` 时，
  在系统临时目录下创建 `nitty-agent-*` 目录。任务产物保留。
- 纯问答、环境快照以及使用绝对路径的文件操作，不会创建临时工作目录。
- 工作目录只决定默认位置，可以访问其他目录。支持绝对路径、`..`、`~` 和环境变量。
- `exec_command` 的 `workdir` 指定单次命令使用的已有目录，不改变文件工具的默认目录。
- 工作目录属于 Environment 实例，不再使用全局变量；不同实例互不影响。

## 从 Python 调用 Agent API

```python
import os
from dotenv import load_dotenv
from openai import OpenAI

from core.agent import Agent
from adapters.local import LocalEnvironment
from adapters.models import OpenAICompatibleModel
from tools.local import create_default_registry

load_dotenv()
with OpenAI(
    api_key=os.environ["DEEPSEEK_API_KEY"],
    base_url="https://api.deepseek.com",
) as client:
    model = OpenAICompatibleModel(
        client, "deepseek-flash",
        extra_body={"thinking": {"type": "disabled"}},
    )
    agent = Agent(
        model=model,
        registry=create_default_registry(),
        environment=LocalEnvironment(),
    )
    print(agent.run("使用 Python 计算 1 到 100 的和。"))
    # 可在 PyCharm 调试器中查看状态，而无需解析控制台日志。
    print(agent.last_state.status)
    print(agent.last_state.turn)
    print(agent.last_state.observations)
```

每次 `run()` 创建独立任务历史；同一 Agent 的环境实例继续保留，可复用已有文件。
`last_state.status` 分为 `running`、`completed`、`limit_reached`、`failed`、`cancelled`。
工具错误反馈给模型以便修正；模型通信失败与轮次耗尽会抛出异常，同时保留任务状态。
调用方若需要完全独立的环境，可创建新的 Agent 和 Environment 实例。

## 替换模型、工具和环境

**替换模型**：实现 `ModelAdapter.generate(messages, tools) → ModelReply`，
然后传给 `Agent(model=...)`。模型名称、认证、厂商参数和协议转换留在适配器中。

当前提供两个实际适配器：`OpenAICompatibleModel` 和 `ClaudeModel`。
后者根据 [Claude 官方工具协议](https://platform.claude.com/docs/en/agents-and-tools/tool-use/handle-tool-calls)
转换 `tool_use` 和 `tool_result`，使用标准库发送 HTTP 请求。
CLI 可通过 `--provider deepseek|openai|claude` 选择。
OpenAI 使用 `OPENAI_API_KEY`，Claude 使用 `ANTHROPIC_API_KEY`；这两种模式须额外提供
`--model "你的模型名称"`。可用 `--base-url` 配置服务地址。
Claude 适配器支持文本、图片与客户端工具调用，不启用思考或服务端工具。
DeepSeek 扩展参数不会发送给 OpenAI 或 Claude。

**替换或新增工具**：用独立的注册表，或者向默认注册表追加能力：

```python
registry = create_default_registry()

# environment 由 Executor 注入；纯计算工具也接收此参数，但不必使用它。
def greet(environment, name: str) -> str:
    return f"你好，{name}！"

registry.register(
    "greet", "向某人问好。",
    {"name": {"type": "string", "description": "姓名"}}, ["name"],
    handler=greet,
    instructions="用户未提供姓名时先询问，不要猜测姓名。",
)
# 创建 Agent 时传入 registry，Runtime 无需修改。
```

`instructions` 是可选的工具使用规则，保存在 `ToolSpec` 中，由 `ContextBuilder`
按工具顺序收集；相同规则只加入一次。规则跟随传入上下文的工具定义，不按工具名称选择。
工具组可为成员提供同一段规则，例如桌面工具的观察与操作约定。
模型适配器只将名称、描述和参数定义转换为厂商工具协议，使用规则通过系统消息传递。

**替换环境**：实现 `contracts.py` 中的 Environment 接口
（`get_info / exec_command / read_file / write_file`），再传给 `Agent(environment=...)`。
例如容器、远程或内存环境，既提供自己的环境快照，也负责实际操作。
本机文件和 Shell 代码不会被 Runtime 隐式调用。

本机环境提供 filesystem 和 shell；安装了 Git 时可通过命令使用 Git。
可选桌面模式提供截图和鼠标键盘能力，可操作浏览器及其他 Windows 应用；没有内置 DOM 或 UI Automation 路由。

## Windows 桌面与跨应用操作

安装更新后的依赖，然后在已登录、解锁的 Windows 10/11 桌面中运行：

```powershell
conda run -n agent python -m pip install -r requirements.txt
conda run --no-capture-output -n agent python main.py --desktop
```

PyCharm：项目解释器选 Conda `agent`，运行配置的**脚本参数**填写
`--desktop --max-turns 60`，然后运行 `main.py`。
启动后输入任务，例如：

> 打开记事本，输入“你好，这是桌面 Agent 测试”，保存到桌面的 nitty-desktop-demo.txt，
> 再打开文件管理器，确认该文件出现。若同名文件已存在，另取一个新名字。

桌面模式默认使用 `direct`，提供 9 个桌面工具和只读 `app_find`，共 10 个工具，默认最多 50 轮。
`separate` 模式额外提供 `desktop_ask`，共 11 个工具。
要同时提供原来的文件和 Shell 工具，可加 `--with-local-tools`：

```powershell
conda run --no-capture-output -n agent python main.py --desktop --with-local-tools --max-turns 60
```

| 工具 | 用途 |
| --- | --- |
| `app_find(name)` | 查询开始菜单应用注册、AppX 包、快捷方式和卸载注册信息；不启动应用 |
| `desktop_screenshot()` | 获取主屏幕截图、尺寸、前台窗口和 `frame_id` |
| `desktop_click(frame_id, x, y, button="left", clicks=1)` | 单击、双击或右击 |
| `desktop_move(frame_id, x, y)` | 鼠标悬停 |
| `desktop_drag(frame_id, from_x, from_y, to_x, to_y, button="left", duration=0.5)` | 拖拽 |
| `desktop_scroll(frame_id, x, y, amount)` | 在指定位置滚动；正数向上，负数向下 |
| `desktop_press_key(frame_id, key)` | 功能键，例如 `enter`、`tab`、`esc` |
| `desktop_hotkey(frame_id, keys)` | 组合键，例如 `["ctrl", "s"]`、`["alt", "tab"]` |
| `desktop_type_text(frame_id, text)` | Unicode 文本、中文、换行，最多 2000 字符 |
| `desktop_wait(seconds)` | 等待 0～10 秒并重新截图 |
| `desktop_ask(question)` | 仅分离模式：重新截图并向 VLM 提出具体问题，返回文字观察、目标坐标和 `frame_id` |

执行流程：`截图 → 模型选择一个动作 → 输入 → 等待界面响应 → 新截图 → 模型继续`。
首次模型请求前先检查截图能否取得；检查失败会直接报错。
默认动作后等待 0.4 秒；页面仍在加载时，模型可调用 `desktop_wait`。

### 应用查找与操作回退

打开图形应用优先从开始菜单或任务栏操作；界面找不到或需要核实安装信息时，使用 `app_find`。
即使开启 `--with-local-tools`，也不应连续猜安装路径。Shell 查询无结果或失败时，
回到开始菜单搜索并观察；启动命令成功后仍需通过界面确认目标窗口。

`app_find` 会分别查询当前用户的开始菜单应用、AppX/MSIX 包、用户/公共开始菜单与桌面快捷方式、
HKCU/HKLM 的普通应用卸载信息（含 32 位路径）。名称按文字和数字匹配，忽略空格与标点，
因此 `Telegram Desktop` 也能匹配包名中的 `TelegramDesktop`。不会执行全盘扫描。
每个来源默认最多等待 10 秒，并在各来源之间检查取消；每个来源最多返回 30 个候选，超出时标记不完整。
查询通过显式绑定的执行器运行固定只读脚本，即使没有向模型开放 `exec_command` 也可使用。

返回值包含 `status`、`matches`、`checked_sources`、`attempted_sources`、`complete`、`errors` 和 `scope`：

- `found`：至少发现一条注册记录或快捷方式；不保证应用仍能启动。其他来源失败时，仍保留候选并返回 `complete=false`。
- `not_found`：所有来源检查完成，但没有匹配项；仅表示该范围未找到，不能推断“没有安装”。便携应用可能不在应用目录中。
- `incomplete`：没有候选，且存在超时、权限错误、无效输出或截断；不能当作完整的空结果。

`checked_sources` 只列出完整检查的来源；`attempted_sources` 列出尝试过的来源。
不同来源可能指向同一应用，同名不同路径也可能代表不同应用；工具保留这些候选，不自行启动或选择。
该状态由查询代码确定，最终回答与 GUI 回退仍由模型遵循工具规则完成，提示词不保证模型绝不误判。

从 Python 单独注册应用查询，不需要让文件环境或桌面控制器增加新属性：

```python
from adapters.windows_apps import WindowsApplicationCatalog
from tools.applications import register_application_tools

catalog = WindowsApplicationCatalog(environment.exec_command)
register_application_tools(registry, catalog)
```

### 运行日志与排查

控制台会显示工具参数（包括实际 Shell 命令）、结果和耗时；超时和截断仍保留在结果中。
`timed_out=true` 即使同时出现 `exit_code=0` 也表示未完成，空输出不能作为目标不存在的证明。

需要保留运行过程时，指定一个尚不存在的日志文件：

```powershell
conda run --no-capture-output -n agent python main.py --desktop --with-local-tools --trace-file .agent-logs/desktop-run-01.jsonl
```

在 PyCharm 的脚本参数中填写相同选项即可。相对日志路径以启动目录为基准，与 `--workdir` 分开；
文件独占新建，不覆盖已有日志。每条 JSONL 事件立即写入，包含运行 ID、时间、轮次、调用 ID、参数、
结果和耗时；取消、拒绝执行的批次及运行失败有独立标记。模型事件仅记录调用时间及工具数量，
不保存完整提示词、用户任务或推理内容。`completed` 表示模型已结束本轮任务，不能视为业务目标经外部验证成功。

控制台和 JSONL 共用脱敏逻辑：遮蔽已知环境凭证、常见密码/令牌字段及赋值；
`desktop_type_text.text` 和 `write_file.content` 默认全部遮蔽。图片仅记录类型和编码长度。
脱敏不改变真正执行的参数、模型反馈或内存中的 `last_state`。
任意自然语言或经过变换的秘密无法保证自动识别，命令输出也可能含业务内容，分享日志前应检查。
默认建议的 `.agent-logs/` 和 `*.trace.jsonl` 已加入 Git 忽略；其他自定义路径需自行管理。

自定义工具可以通过 `registry.register(..., sensitive_parameters=("text",))` 指定敏感参数，
这些元数据仅用于日志，不传给模型。Python 调用方可在 `with JsonlTrace(path) as trace:` 中将
`trace=trace` 传给 `Agent`，或注入实现 `EventSink.emit(event)` 的其他接收器。

### 两种视觉模式

**一体模式（默认）**：主模型需要支持图片和工具调用，直接理解截图并决定动作。

```powershell
conda run --no-capture-output -n agent python main.py --desktop --vision-mode direct
```

**分离模式**：主模型只需支持文本和工具调用；独立 VLM 负责回答截图问题。
LLM 按需调用 `desktop_ask(question)`，例如“找到保存按钮，返回位置”或
“检查是否保存成功，说明可见证据”。每次提问都会获取一张新截图，VLM 只接收本次问题和图片，
不共享主模型的完整任务历史；需要前情时，LLM 应在问题中明确说明。
普通截图、等待和动作不会自动调用 VLM，也不会自动生成泛泛的屏幕描述。

```powershell
conda run --no-capture-output -n agent python main.py --desktop --vision-mode separate --vision-model deepseek-flash
```

上例沿用默认 DeepSeek 提供方和现有密钥，为主模型与 VLM 分别创建适配器。
主模型也可以通过 `--provider / --model / --base-url` 换成纯文本模型。
PyCharm 的运行配置中填写相同脚本参数，例如：
`--desktop --vision-mode separate --vision-model deepseek-flash`。

| 分离模式配置 | 说明 |
| --- | --- |
| `--vision-provider` / `VISION_PROVIDER` | `deepseek`、`openai` 或 `claude`；未设置时与主模型提供方相同 |
| `--vision-model` / `VISION_MODEL` | 必须明确指定支持图片输入的视觉模型名称 |
| `--vision-base-url` / `VISION_BASE_URL` | 独立视觉服务地址；未设置时使用视觉提供方的默认地址，不继承主模型的自定义地址 |
| `VISION_API_KEY` | 视觉服务密钥；未设置时使用视觉提供方对应的 `DEEPSEEK_API_KEY`、`OPENAI_API_KEY` 或 `ANTHROPIC_API_KEY` |

CLI 参数优先于相应环境变量；`.env.example` 包含可选视觉配置。
自建 OpenAI 兼容 VLM 服务使用 `--vision-provider openai`、自己的服务地址和模型名称。
`direct` 模式忽略 `.env` 中的可选视觉配置；显式传入视觉模型参数却未启用 `separate` 时会报错。
两种模式不会自动切换或在请求失败时回退到另一提供方。

分离模式的执行闭环是：
`LLM 提出视觉问题 → 新截图 → VLM 回答 → LLM 选择动作 → 桌面工具执行 → 按需再次提问验证`。
`TextOnlyModel` 在主模型边界移除所有标准图片附件；原始截图继续保留在任务状态中供调试。
主模型接收的视觉反馈形如：

```json
{
  "frame_id": "本次截图编号",
  "image_width": 1600,
  "image_height": 900,
  "question": "找到保存按钮",
  "vision": {
    "status": "answered",
    "answer": "保存按钮位于对话框右下方。",
    "targets": [{"label": "保存", "x": 1200, "y": 700}]
  }
}
```

以上是结构示例，坐标不能直接用于真实操作。`frame_id` 和图片尺寸由控制器提供，不能由 VLM 伪造。
`answered` 仅表示问题可回答，不等于任务完成；LLM 必须读取 `answer` 中的证据。
未找到目标、目标有歧义、无法确定时分别返回 `not_found / ambiguous / uncertain`，这些状态不允许提供动作坐标。
VLM 返回的坐标必须是截图范围内的整数；它不能发出工具调用，也不能直接控制桌面。
视觉请求完成后会重新检查截图时效、前台窗口、屏幕尺寸及急停状态，失效结果不会交给 LLM 执行动作。
任何新截图都会替换旧编号，之后不能继续使用旧视觉回答中的坐标。

### 截图、输入与急停

截图最长边默认缩放至 1600 像素，可用 `--screenshot-size 1920` 调整（640～3840）。
模型的坐标始终基于**实际发送的图片尺寸**；控制器负责映射到物理屏幕坐标，
并在 Windows 输入线程启用 Per-Monitor V2 DPI awareness。
所有输入动作要求最新 `frame_id`。截图超过 120 秒、前台窗口变化、分辨率变化或编号过期时，
拒绝输入并要求重新截图。动作开始后消费该编号，截图反馈失败也不会允许直接重放旧动作。
同一轮若包含多个调用且其中有桌面工具，整批调用均不执行，反馈给模型逐次决策。

底层使用 MSS 截图、PyAutoGUI 鼠标键盘操作；中文通过 Win32 `SendInput / KEYEVENTF_UNICODE`
输入，不占用剪贴板。拖拽和组合键退出时会释放已按下的鼠标或键。
F8 是全局急停键：一旦检测到就锁存，停止后续输入；若模型请求正在进行，会在请求返回后退出。
终端 Ctrl+C 可中断当前运行；鼠标移至主屏幕角落也会在检查时触发急停。
中止异常会穿过工具执行器，将任务标记为 `cancelled`，不会交给模型重试。

第一版只支持主显示器上的可见界面，需要保持桌面解锁，操作期间避免人工同时输入。
管理员窗口、UAC 安全桌面、锁屏、断开的远程桌面或特殊输入控件可能无法操作。
输入返回成功只代表发送了事件，最终效果仍需模型通过截图确认。
同一窗口内的弹窗、动画和布局变化也可能影响坐标，复杂跨应用任务仍需实际验收。

### 图片与环境的接入方式

- `register_desktop_tools(registry, desktop, vision=None)` 显式绑定 `DesktopController`；控制器为必填参数。
  工具执行不读取 `environment.desktop`，可配合没有桌面属性的 Environment 使用。
- 传入 `vision=ModelVisionAdapter(vlm_model)` 启用按需视觉工具；VLM 可复用任意支持图片的现有模型适配器。
- 自定义视觉后端实现 `VisionAdapter.answer(question, image, width=..., height=...) → VisionAnswer`。
  自定义桌面控制器须实现 `validate_frame(frame_id)`，用于视觉请求后的无输入复核。
- `LocalEnvironment(desktop=desktop)` 仅将桌面信息纳入环境快照；CLI 将同一控制器传给环境和工具注册函数。
- 桌面使用规则随工具注册，通用上下文模块不包含桌面工具名称判断；Runtime 不导入 Windows 库。
- `ToolResult(data, images)` → `Observation.images` → `Message.images`，图片与 JSON 文本分开传递。
- OpenAI 兼容适配器在整组 tool 结果之后追加带图片的 user 观察消息；Claude 放在对应 `tool_result` 内容块里。
- `ContextBuilder(max_images=2)` 默认仅保留模型上下文中最近两张图片；分离模式在主模型边界继续移除这些附件。
  原始状态保留本次任务全部观察供调试。
- 图片保存在进程内存中，不自动保存到磁盘，也不把 Base64 内容打印到控制台。
  一体模式将截图发送给主模型服务；分离模式仅在提问时将本次截图发送给视觉服务，文字观察返回主模型。

一体模式的主模型需要支持视觉输入，参见 [DeepSeek 图像理解文档](https://api-docs.deepseek.com/guides/vision/)。
分离模式中，主模型需要支持工具调用，VLM 需要支持图片输入和按要求输出 JSON，不要求 VLM 支持工具调用。
其他底层参考：[MSS](https://python-mss.readthedocs.io/latest/usage.html)、
[PyAutoGUI](https://pyautogui.readthedocs.io/en/latest/)、
[Windows Unicode 输入](https://learn.microsoft.com/en-us/windows/win32/api/winuser/ns-winuser-keybdinput)。

Python API 中，桌面控制器的生命周期和急停检查由调用方接入：

```python
from adapters.windows_desktop import WindowsDesktop
from tools.desktop import register_desktop_tools
from core.tooling import ToolRegistry

# model 使用前文已经配置好的支持图片输入的适配器。
with WindowsDesktop() as desktop:
    registry = ToolRegistry()
    register_desktop_tools(registry, desktop)
    agent = Agent(
        model, registry, LocalEnvironment(desktop=desktop), max_turns=50,
        cancel_check=desktop.check_cancelled,
    )
    print(agent.run("打开记事本并输入一段中文。"))
```

分离模式的 Python API：

```python
from adapters.models import TextOnlyModel
from adapters.vision import ModelVisionAdapter

# planner_model 与 vlm_model 是调用方分别创建并管理的模型适配器。
with WindowsDesktop() as desktop:
    registry = ToolRegistry()
    register_desktop_tools(registry, desktop, vision=ModelVisionAdapter(vlm_model))
    agent = Agent(
        TextOnlyModel(planner_model), registry, LocalEnvironment(desktop=desktop),
        max_turns=50, cancel_check=desktop.check_cancelled,
    )
    print(agent.run("找到保存按钮，保存后检查界面是否显示成功。"))
```

Python 调用方须搭配 `TextOnlyModel`，确保纯文本主模型不接收图片附件；CLI 会自动完成该装配。

## 本地验证

```powershell
conda run --no-capture-output -n agent python -m unittest discover -s tests -t . -v
```

测试不联网，覆盖自定义模型和工具、内存环境替换、协议转换、错误恢复、
状态隔离、动态环境上下文，以及本机目录、命令退出码、中文输出和超时处理。
桌面测试使用替身后端，不移动真实鼠标或向应用输入内容；覆盖缩放、过期观察、
多调用拦截、图片历史裁剪、协议序列化、急停和输入释放。
另覆盖桌面控制器显式绑定、工具规则随定义传递及共享规则去重。
`tests/test_vision.py` 覆盖纯文本主模型隔离、按需定位与验证、视觉结果校验、过期截图、
VLM 请求期间急停、不同模型独立配置及两种模式的 CLI 装配。
OpenAI/Claude 适配器使用模拟服务响应。
单独运行一组测试，例如 `conda run --no-capture-output -n agent python -m unittest tests.test_vision -v`。
在 PyCharm 中也可为 `tests` 目录创建 unittest 运行配置，工作目录设为项目根目录。

当前实现为同步单 Agent，普通模式默认最多 10 轮、桌面模式 50 轮；未实现交互式 Shell、自动加载 `AGENTS.md`、
历史压缩或持久化恢复。文件和命令在本机以当前用户权限执行。
