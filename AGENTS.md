# 项目 Python 环境

- 本项目使用 Conda 环境 `agent`，Python 3.12。
- 解释器路径：`C:\Users\wangy\miniconda3\envs\agent\python.exe`。
- 运行 Python、测试及安装依赖时，使用 `conda run -n agent ...`，或先执行 `conda activate agent` 并确认激活成功。
- 安装 Python 包使用 `conda run -n agent python -m pip ...`，确保依赖安装到项目环境中。

## 编辑器

- 本项目默认使用 PyCharm，编辑器配置和操作说明优先面向 PyCharm。
- PyCharm 的项目解释器选择已有 Conda 环境 `agent`，路径为上述 `python.exe`。

## 重大改动的设计与迭代记录

- 对架构、能力模块、持久化格式、恢复机制或用户操作流程的大改动，必须同步维护设计文档和迭代记录。
- 动手前先检查 `docs/README.md` 中的现有设计；新的模块在 `docs/` 中建立设计文档，说明问题、目标、方案、关键取舍、边界、接口和验收方法。
- 每次重大改动在 `docs/changes/` 新建日期加主题命名的记录，并更新 `docs/README.md` 索引。记录背景、最终实现、使用方法、兼容与恢复影响、实际验证结果、局限和后续迭代。
- 以 `docs/changes/TEMPLATE.md` 为写作模板；已有设计随实现更新，历史记录保留当次事实。未执行的检查和计划中的能力必须明确标注，不能写成已经完成。
- 涉及使用方式时同步更新项目 `README.md`；涉及依赖、配置或数据格式时同步更新对应声明与示例。低影响的文案或格式修改可只更新相关文档，无需独立迭代记录。
