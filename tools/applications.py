"""应用查询工具：仅依赖显式传入的应用目录，不依赖桌面或 Shell 工具名称。"""

from validation import validate_app_name
from contracts import ApplicationCatalog, ToolRejected
from core.tooling import ToolRegistry


APPLICATION_INSTRUCTIONS = (
    "打开应用优先使用开始菜单或任务栏；界面找不到或需要核实安装信息时，使用应用查询工具。"
    "found 表示发现了注册记录或快捷方式，不保证程序能启动；启动后仍需观察窗口确认。"
    "多个来源可能指向同一应用；存在不同候选时应结合名称和界面核实，不擅自选择。"
    "not_found 仅表示已检查来源未匹配，不能断言未安装；便携应用可能不在这些来源中。"
    "incomplete 或 complete=false 表示有来源查询失败或截断，必须说明未完成的范围。"
    "查询无结果或失败时，回到开始菜单搜索并观察，不要连续猜安装路径或全盘扫描。"
    "不要将已发现但启动失败的应用说成未安装；报告实际证据，不要未经请求下载安装。"
)


def register_application_tools(registry: ToolRegistry, catalog: ApplicationCatalog) -> None:
    if catalog is None:
        raise ValueError("注册应用查询工具必须提供 ApplicationCatalog。")

    def find(_environment, name: str):
        try:
            name = validate_app_name(name)
        except ValueError as error:
            raise ToolRejected("INVALID_ARGUMENTS", str(error)) from error
        return catalog.find(name)

    registry.register(
        "app_find", "只读查询 Windows 应用注册、AppX 包、快捷方式和安装信息；返回候选及检查范围，不启动应用。",
        {"name": {"type": "string", "minLength": 1, "maxLength": 200, "description": "应用名称或其有辨识度的部分"}},
        ["name"], find, instructions=APPLICATION_INSTRUCTIONS, effect="read",
    )
