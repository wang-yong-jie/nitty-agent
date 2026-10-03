# Codex 工具源码阅读入口

本目录保存 OpenAI 官方 `openai/codex` 仓库中的原始 Rust 源码，供对照学习。
这些文件是阅读用的源码快照，不是可独立编译的完整项目。
本项目的 Python Agent 不导入或执行这些文件。

- 官方仓库：https://github.com/openai/codex
- 固定提交：`86a54b051c08f34f373c507ae16a91915ab08700`
- 原始目录：`codex-rs/core/src/tools/`
- 许可：Apache-2.0，原始 `LICENSE` 和仓库中存在的 `NOTICE` 已一并保留。
- Rust 源文件未修改；本 README 是为本项目编写的中文阅读说明。

## 与我们的代码如何对应

| 我们的 Python 代码 | Codex 中对应的组织方式 | 先看哪个文件 |
| --- | --- | --- |
| `tool_schema(...)` | 构建工具名称、描述、参数 Schema | `shell_spec.rs` 第 25 行附近 |
| `TOOLS` | 按模型、环境和配置组装可见的工具集合 | `spec_plan.rs` 第 556 行附近 |
| `TOOL_FUNCTIONS` | 注册工具执行器，由名称找到实际执行逻辑 | `spec_plan.rs` 第 1062 行、`registry.rs` 第 319 行附近 |
| `run_python(...)` | 通用命令执行器 `ExecCommandHandler` | `exec_command.rs` 第 112 行附近 |
| `json.loads(arguments)` | JSON 参数反序列化和校验 | `handlers_mod.rs` 第 86 行附近 |
| `tool_functions[name](**arguments)` | 注册表分派到执行器 | `registry.rs` 第 526 行附近 |

## 推荐阅读顺序

1. `shell_spec.rs`：搜索 `create_exec_command_tool_with_environment_id`。
   `cmd`、`workdir` 等参数都由代码明确描述，与你截图里的工具描述对应。
2. `spec_plan.rs`：搜索 `registry.add(ExecCommandHandler::new(options))`。
   这里根据配置注册执行器；也会注册 MCP 和动态工具，因此工具集合不只是固定数组。
3. `exec_command.rs`：看 `ToolExecutor` 实现。
   `tool_name()` 返回名称，`spec()` 返回描述，`handle()` 调用真正的执行逻辑。
4. `registry.rs`：看 `dispatch_any_with_state()`，理解模型返回调用后怎样路由执行。
5. `dynamic.rs`：看外部提供的工具描述怎样转成可注册的工具，而不用为每个工具写一个内置处理器。

## 一段真实的注册代码

以下摘自 `spec_plan.rs`，该分支在启用 UnifiedExec 时执行：

```rust
registry.add(ExecCommandHandler::new(options));
registry.add(WriteStdinHandler);
```

这里的意义是：注册“执行命令”和“向已有进程写入输入”两个通用能力。
创建目录、列出文件、运行测试等许多任务可以通过命令工具完成，并不要求每个任务都有单独工具。

## 原始路径

- `shell_spec.rs`：`codex-rs/core/src/tools/handlers/shell_spec.rs`
- `exec_command.rs`：`codex-rs/core/src/tools/handlers/unified_exec/exec_command.rs`
- `unified_exec.rs`：`codex-rs/core/src/tools/handlers/unified_exec.rs`
- `handlers_mod.rs`：`codex-rs/core/src/tools/handlers/mod.rs`（本地重命名，以免与其他 mod.rs 混淆）
- `dynamic.rs`：`codex-rs/core/src/tools/handlers/dynamic.rs`
- `spec_plan.rs`：`codex-rs/core/src/tools/spec_plan.rs`
- `registry.rs`：`codex-rs/core/src/tools/registry.rs`
- `router.rs`：`codex-rs/core/src/tools/router.rs`

例如工具 Schema 的固定版本源码：
[shell_spec.rs](https://github.com/openai/codex/blob/86a54b051c08f34f373c507ae16a91915ab08700/codex-rs/core/src/tools/handlers/shell_spec.rs#L25)。
