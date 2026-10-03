"""模型适配器：统一 Runtime 接口，隔离 OpenAI 兼容协议和 Claude 协议。"""

import json
from urllib.request import Request, urlopen

from contracts import Message, ModelReply, ToolCall, ToolSpec


class OpenAICompatibleModel:
    """支持 DeepSeek 和 OpenAI 兼容 Chat Completions 服务，客户端由入口配置。"""

    def __init__(self, client, model: str, max_tokens: int = 4096, extra_body: dict | None = None):
        self.client = client
        self.model = model
        self.max_tokens = max_tokens
        # 厂商扩展参数只在适配器中处理，不能写进通用循环。
        self.extra_body = extra_body

    def generate(self, messages: list[Message], tools: list[ToolSpec]) -> ModelReply:
        """把统一消息转为 OpenAI 格式，再把响应转回统一决策。"""
        history = []
        for message in messages:
            item = {"role": message.role, "content": message.content}
            if message.tool_calls:
                item["tool_calls"] = [{
                    "id": call.id, "type": "function",
                    "function": {"name": call.name, "arguments": call.arguments},
                } for call in message.tool_calls]
            if message.tool_call_id is not None:
                item["tool_call_id"] = message.tool_call_id
            history.append(item)
        request = {"model": self.model, "messages": history, "max_tokens": self.max_tokens}
        if tools:
            request["tools"] = [{"type": "function", "function": {
                "name": tool.name, "description": tool.description, "parameters": tool.parameters,
            }} for tool in tools]
        if self.extra_body is not None:
            request["extra_body"] = self.extra_body
        choice = self.client.chat.completions.create(**request).choices[0]
        if choice.finish_reason == "length":
            raise RuntimeError("模型输出被截断，请增大 max_tokens 后重试。")
        message = choice.message
        return ModelReply(message.content, [
            ToolCall(call.id, call.function.name, call.function.arguments)
            for call in message.tool_calls or []
        ])


class ClaudeModel:
    """通过 Claude Messages API 实现相同接口；这里只支持文本和客户端工具。"""

    def __init__(
        self, api_key: str, model: str, base_url: str = "https://api.anthropic.com",
        max_tokens: int = 4096, timeout: int = 60,
    ):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.max_tokens = max_tokens
        self.timeout = timeout

    def generate(self, messages: list[Message], tools: list[ToolSpec]) -> ModelReply:
        """Claude 将工具结果放进 user 内容块；这个差异不进入 Runtime。"""
        systems = []
        history = []
        for message in messages:
            if message.role == "system":
                systems.append(message.content or "")
                continue
            role = "user" if message.role == "tool" else message.role
            if message.role == "tool":
                blocks = [{
                    "type": "tool_result", "tool_use_id": message.tool_call_id,
                    "content": message.content or "", "is_error": message.is_error,
                }]
            else:
                blocks = [{"type": "text", "text": message.content}] if message.content else []
                blocks += [{
                    "type": "tool_use", "id": call.id, "name": call.name,
                    "input": json.loads(call.arguments),
                } for call in message.tool_calls]
            # 同一轮多个工具结果合并为一个 user 消息，紧跟对应的 assistant 调用。
            if history and history[-1]["role"] == role:
                history[-1]["content"].extend(blocks)
            else:
                history.append({"role": role, "content": blocks})
        payload = {
            "model": self.model, "max_tokens": self.max_tokens,
            "system": "\n\n".join(systems), "messages": history,
        }
        if tools:
            payload["tools"] = [{
                "name": tool.name, "description": tool.description, "input_schema": tool.parameters,
            } for tool in tools]
        # 使用标准库 HTTP 客户端，避免仅为这个适配器引入额外 SDK 依赖。
        request = Request(
            self.base_url + "/v1/messages",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"x-api-key": self.api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=self.timeout) as response:
            data = json.load(response)
        if data.get("stop_reason") == "max_tokens":
            raise RuntimeError("模型输出被截断，请增大 max_tokens 后重试。")
        # 服务端工具或思考模式需要额外状态处理，不能将其误当作普通最终回答。
        if data.get("stop_reason") not in {"end_turn", "tool_use", "stop_sequence", "refusal"}:
            raise RuntimeError(f"暂不支持的 Claude 结束原因：{data.get('stop_reason')}")
        texts, calls = [], []
        for block in data["content"]:
            if block["type"] == "text":
                texts.append(block["text"])
            elif block["type"] == "tool_use":
                calls.append(ToolCall(block["id"], block["name"], json.dumps(block["input"], ensure_ascii=False)))
            else:
                raise RuntimeError(f"暂不支持的 Claude 内容类型：{block['type']}")
        return ModelReply("\n".join(texts) or None, calls)
