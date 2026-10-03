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

## 架构与文件职责

```text
User → Agent.run(task)
           │
           ▼
     AgentRuntime
       ├── Agent Loop
       ├── AgentState / ContextBuilder
       ├── ModelAdapter → DeepSeek / OpenAI / Claude
       └── ToolRegistry → ToolExecutor → Environment
                              ▲             │
                              └─ Observation┘
                         反馈进入状态，下一轮继续决策
```

| 层 | 文件 | 职责 |
| --- | --- | --- |
| Agent API | `agent.py` | `Agent.run(task)` 提交任务，返回最终回答 |
| Runtime / Loop | `runtime.py` | 协调上下文、模型决策、工具执行和循环结束 |
| State | `state.py` | 保存任务、历史、轮次、Observation、状态和最终回答 |
| Context | `context.py` | 将指令、最新环境快照与历史组装为模型输入 |
| Model Adapter | `model.py` | 转换 OpenAI 兼容或 Claude 原生协议 |
| Tool Registry / Executor | `tools.py` | 注册描述和处理函数，解析参数，调度并反馈错误 |
| Environment | `environment.py` | `LocalEnvironment` 负责本机路径、文件和 Shell 操作 |
| 公共接口与数据 | `contracts.py` | 定义模型/环境接口及消息、调用、Observation |
| 配置入口 | `main.py` | 读取配置和输入，将各模块装配起来 |

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
New-Item -ItemType Directory -Force -Path './examples'
Get-ChildItem -LiteralPath '.'
python examples/fibonacci.py
python -m unittest -v
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

from agent import Agent
from environment import LocalEnvironment
from model import OpenAICompatibleModel
from tools import create_default_registry

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
`last_state.status` 分为 `running`、`completed`、`limit_reached`、`failed`。
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
Claude 适配器支持文本与客户端工具调用，不启用思考、服务端工具或多模态。
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
)
# 创建 Agent 时传入 registry，Runtime 无需修改。
```

**替换环境**：实现 `contracts.py` 中的 Environment 接口
（`get_info / exec_command / read_file / write_file`），再传给 `Agent(environment=...)`。
例如容器、远程或内存环境，既提供自己的环境快照，也负责实际操作。
本机文件和 Shell 代码不会被 Runtime 隐式调用。

本机环境目前提供 filesystem 和 shell；安装了 Git 时可通过命令使用 Git。
图中的 browser、computer 等能力可通过新增工具和相应环境实现接入，目前未内置。

## 本地验证

```powershell
conda run --no-capture-output -n agent python -m unittest -v
```

测试不联网，覆盖自定义模型和工具、内存环境替换、协议转换、错误恢复、
状态隔离、动态环境上下文，以及本机目录、命令退出码、中文输出和超时处理。
OpenAI/Claude 适配器使用模拟服务响应；实际联网验证使用已配置的 DeepSeek。

当前实现为同步单 Agent，默认最多 10 轮；未实现交互式 Shell、自动加载 `AGENTS.md`、
历史压缩或持久化恢复。文件和命令在本机以当前用户权限执行。
