"""用生成的测试图片验证 DeepSeek 工具图片反馈；不截图、不操作真实桌面。

从项目根目录运行：conda run --no-capture-output -n agent python -m examples.vision_smoke
此脚本会调用配置的 DeepSeek 服务，产生少量 API 用量。
"""

import base64
import os
import secrets
from io import BytesIO
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI
from PIL import Image, ImageDraw, ImageFont

from agent import Agent
from contracts import ImageContent, ToolResult
from environment import LocalEnvironment
from model import OpenAICompatibleModel
from tools import ToolRegistry


def main():
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    code = str(secrets.randbelow(900000) + 100000)
    picture = Image.new("RGB", (640, 240), "white")
    font = ImageFont.load_default(size=80)
    ImageDraw.Draw(picture).text((60, 65), code, font=font, fill="black")
    output = BytesIO()
    picture.save(output, format="PNG")
    attachment = ImageContent(base64.b64encode(output.getvalue()).decode("ascii"))
    registry = ToolRegistry()
    registry.register(
        "test_image", "获取本次测试图片。", {}, [],
        lambda environment: ToolResult({"width": 640, "height": 240}, [attachment]),
        requires_single_call=True,
    )
    with OpenAI(api_key=os.environ["DEEPSEEK_API_KEY"], base_url="https://api.deepseek.com",
                timeout=60, max_retries=0) as client:
        model = OpenAICompatibleModel(client, "deepseek-flash", max_tokens=256,
                                      extra_body={"thinking": {"type": "disabled"}})
        agent = Agent(model, registry, LocalEnvironment(), max_turns=3, verbose=False)
        answer = agent.run("先调用 test_image 工具获取图片，然后读取图中的六位数字。最终只回复六位数字。")
    if answer.strip() != code:
        raise RuntimeError(f"图片识别未通过：预期 {code}，实际 {answer!r}")
    print(f"PASS：模型通过工具图片反馈读取了随机数字；共 {agent.last_state.turn} 轮。")


if __name__ == "__main__":
    main()
