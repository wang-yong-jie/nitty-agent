"""共享参数与视觉结果校验；工具和适配器均可使用，无平台依赖。"""

from contracts import VisionAnswer, VisualTarget


def validate_app_name(name: str) -> str:
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 200 or not any(c.isalnum() for c in name):
        raise ValueError("应用名称须为 1～200 字符，并包含文字或数字。")
    return name.strip()


def validate_question(question: str) -> None:
    if not isinstance(question, str) or not question.strip() or len(question) > 2000:
        raise ValueError("视觉问题必须是 1 到 2000 字符的非空文本。")


def validate_vision_answer(result: VisionAnswer, *, width: int, height: int) -> None:
    """适用于内置或外部视觉适配器；错误结果不交给规划模型用于动作。"""
    if not isinstance(result, VisionAnswer):
        raise ValueError("视觉适配器必须返回 VisionAnswer。")
    if result.status not in ("answered", "not_found", "ambiguous", "uncertain"):
        raise ValueError("视觉回答的 status 无效。")
    if not isinstance(result.answer, str) or not result.answer.strip() or len(result.answer) > 4000:
        raise ValueError("视觉回答必须是 1 到 4000 字符的非空文本。")
    if not isinstance(result.targets, list) or len(result.targets) > 20:
        raise ValueError("视觉目标必须是最多 20 项的列表。")
    if result.status != "answered" and result.targets:
        raise ValueError("未找到、歧义或不确定的视觉回答不能提供动作坐标。")
    for target in result.targets:
        if not isinstance(target, VisualTarget):
            raise ValueError("视觉目标必须为 VisualTarget。")
        if not isinstance(target.label, str) or not target.label.strip() or len(target.label) > 200:
            raise ValueError("视觉目标名称必须是 1 到 200 字符的非空文本。")
        if (type(target.x) is not int or type(target.y) is not int
                or not 0 <= target.x < width or not 0 <= target.y < height):
            raise ValueError(f"视觉目标坐标必须是截图范围内的整数：宽 {width}，高 {height}。")
