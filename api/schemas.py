"""浏览器接口契约；OpenAPI 是前端生成类型的唯一来源。"""

from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

from bootstrap import AgentOptions


class RunOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: Literal["deepseek", "openai", "claude"] = "deepseek"
    model: str | None = Field(default=None, max_length=200)
    base_url: str | None = Field(default=None, max_length=2048)
    workdir: str | None = Field(default=None, max_length=4096)
    desktop: bool = False
    with_local_tools: bool = False
    vision_mode: Literal["direct", "separate"] = "direct"
    vision_provider: Literal["deepseek", "openai", "claude"] | None = None
    vision_model: str | None = Field(default=None, max_length=200)
    vision_base_url: str | None = Field(default=None, max_length=2048)
    max_turns: int | None = Field(default=None, ge=1, le=1000, strict=True)
    screenshot_size: int = Field(default=1600, ge=640, le=3840, strict=True)

    def to_options(self) -> AgentOptions:
        return AgentOptions(**self.model_dump())

    @model_validator(mode="after")
    def valid_options(self):
        self.to_options().validate()
        return self


class TaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task: str = Field(min_length=1, max_length=50000)
    options: RunOptions | None = None
    session_id: str | None = Field(default=None, min_length=1, max_length=64)

    @model_validator(mode="after")
    def nonempty(self):
        self.task = self.task.strip()
        if not self.task:
            raise ValueError("任务不能为空。")
        return self


class ErrorDetail(BaseModel):
    code: str
    phase: str
    message: str
    side_effects: Literal["none", "possible"]
    exception_type: str | None = None


class TaskRecord(BaseModel):
    id: str
    session_id: str | None = None
    task: str
    options: RunOptions
    status: Literal["queued", "running", "stopping", "completed", "failed", "cancelled", "interrupted"]
    created_at: str
    started_at: str | None
    finished_at: str | None
    answer: str | None
    error: str | None
    error_info: ErrorDetail | None
    run_id: str | None
    turn: int
    stop_reason: str | None


class SessionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(default="新会话", min_length=1, max_length=80)


class SessionRecord(BaseModel):
    id: str
    title: str
    created_at: str
    updated_at: str
    options: RunOptions | None
    working_directory: str | None
    omitted_messages: int


class TaskEvent(BaseModel):
    id: int
    task_id: str
    data: dict[str, Any]


class Capabilities(BaseModel):
    providers: dict[str, bool]
    desktop_available: bool
    single_task: bool = True
    default_provider: str = "deepseek"
    default_model: str = "deepseek-flash"


class ApiError(BaseModel):
    detail: str | list[dict[str, Any]]
