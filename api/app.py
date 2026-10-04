"""FastAPI 只管理接口；Agent 循环在 service 的工作进程中运行。"""

import asyncio
from contextlib import asynccontextmanager
import os
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.sse import EventSourceResponse, ServerSentEvent
from fastapi.staticfiles import StaticFiles

from api.schemas import ApiError, Capabilities, SessionCreate, SessionRecord, TaskCreate, TaskEvent, TaskRecord
from bootstrap import AgentOptions, PROVIDER_KEYS, ROOT, load_settings, model_settings
from service.manager import TaskBusy, TaskManager
from service.store import ACTIVE


def capabilities() -> dict:
    load_settings()
    return dict(providers={name: bool(os.getenv(key)) for name, key in PROVIDER_KEYS.items()},
                desktop_available=os.name == "nt")


def create_app(*, data_dir: Path | None = None, trace_dir: Path | None = None,
               manager_factory=TaskManager, validate_options=model_settings, settings_provider=capabilities,
               frontend_dir: Path | None = None) -> FastAPI:
    data_dir = data_dir or ROOT / ".agent-data"
    trace_dir = trace_dir or ROOT / ".agent-logs"
    frontend_dir = frontend_dir or ROOT / "frontend" / "dist"

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.manager = manager_factory(data_dir, trace_dir)
        try:
            yield
        finally:
            await asyncio.to_thread(app.state.manager.close)

    app = FastAPI(title="Nitty Agent 本地控制台", version="1.0.0", lifespan=lifespan,
                  responses={status: {"model": ApiError} for status in (400, 403, 404, 409, 422, 500)})
    app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
                       allow_methods=["GET", "POST"], allow_headers=["Content-Type", "Last-Event-ID"])

    @app.middleware("http")
    async def local_access(request: Request, call_next):
        # 即便监听在回环地址也检查 Host，避免其他域名解析到本机后访问控制接口。
        if request.url.hostname not in {"localhost", "127.0.0.1", "::1"}:
            return JSONResponse({"detail": "仅允许通过本机地址访问。"}, status_code=403)
        origin = request.headers.get("origin")
        if origin:
            parsed = urlsplit(origin)
            own_origin = f"{request.url.scheme}://{request.headers.get('host', '')}"
            if origin not in {own_origin, "http://localhost:5173", "http://127.0.0.1:5173"} or parsed.username:
                return JSONResponse({"detail": "不允许此来源访问本地 Agent。"}, status_code=403)
        if request.method == "POST" and request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
            return JSONResponse({"detail": "请求必须使用 application/json。"}, status_code=415)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store" if request.url.path.startswith("/api/") else "no-cache"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    def manager() -> TaskManager:
        return app.state.manager

    def get_task(task_id: str) -> dict:
        try:
            return manager().store.get(task_id)
        except KeyError:
            raise HTTPException(404, "任务不存在。") from None

    def get_session(session_id: str) -> dict:
        try:
            return manager().store.get_session(session_id)
        except KeyError:
            raise HTTPException(404, "会话不存在。") from None

    @app.get("/api/sessions", response_model=list[SessionRecord], operation_id="list_sessions")
    def list_sessions(limit: int = Query(default=100, ge=1, le=500)):
        return manager().store.list_sessions(limit)

    @app.post("/api/sessions", response_model=SessionRecord, status_code=201, operation_id="create_session")
    def create_session(body: SessionCreate):
        return manager().store.create_session(body.title.strip() or "新会话")

    @app.get("/api/sessions/{session_id}", response_model=SessionRecord, operation_id="get_session")
    def session_detail(session_id: str):
        return get_session(session_id)

    @app.get("/api/sessions/{session_id}/tasks", response_model=list[TaskRecord], operation_id="list_session_tasks")
    def session_tasks(session_id: str):
        get_session(session_id)
        return manager().store.session_tasks(session_id)

    @app.get("/api/capabilities", response_model=Capabilities, operation_id="get_capabilities")
    def get_capabilities():
        return settings_provider()

    @app.get("/api/tasks", response_model=list[TaskRecord], operation_id="list_tasks")
    def list_tasks(limit: int = Query(default=100, ge=1, le=500)):
        return manager().store.list(limit)

    @app.post("/api/tasks", response_model=TaskRecord, status_code=202, operation_id="create_task")
    def create_task(body: TaskCreate):
        previous = get_session(body.session_id) if body.session_id else None
        options = body.options.to_options() if body.options is not None else AgentOptions(**(
            previous["options"] if previous and previous["options"] else {}))
        try:
            validate_options(options)
            if options.desktop and os.name != "nt":
                raise ValueError("桌面模式仅支持 Windows。")
            return manager().submit(body.task, options, body.session_id)
        except TaskBusy as error:
            raise HTTPException(409, str(error)) from None
        except (ValueError, RuntimeError) as error:
            raise HTTPException(422, str(error)) from None

    @app.get("/api/tasks/{task_id}", response_model=TaskRecord, operation_id="get_task")
    def task_detail(task_id: str):
        return get_task(task_id)

    @app.post("/api/tasks/{task_id}/stop", response_model=TaskRecord, operation_id="stop_task")
    def stop_task(task_id: str):
        get_task(task_id)
        return manager().stop(task_id)

    @app.get("/api/tasks/{task_id}/events", response_model=list[TaskEvent], operation_id="get_events")
    def events(task_id: str, after: int = Query(default=0, ge=0), limit: int = Query(default=200, ge=1, le=500)):
        get_task(task_id)
        return manager().store.events(task_id, after, limit)

    def stream_cursor(task_id: str, after: int = Query(default=0, ge=0),
                      last_event_id: str | None = Header(default=None)) -> int:
        # 在发送 SSE 响应头之前校验，否则未知任务会表现为已建立后断开的流。
        get_task(task_id)
        if last_event_id is not None:
            try:
                after = max(after, int(last_event_id))
            except ValueError:
                raise HTTPException(400, "Last-Event-ID 必须是整数。") from None

        return after

    @app.get("/api/tasks/{task_id}/events/stream", response_class=EventSourceResponse, operation_id="stream_events")
    async def stream_events(task_id: str, request: Request, cursor: int = Depends(stream_cursor)):
        while not await request.is_disconnected():
            batch = await asyncio.to_thread(manager().store.events, task_id, cursor)
            for event in batch:
                cursor = event["id"]
                yield ServerSentEvent(data=event, event="task_event", id=str(cursor))
            if not batch:
                record = await asyncio.to_thread(manager().store.get, task_id)
                if record["status"] not in ACTIVE:
                    pending = await asyncio.to_thread(manager().store.events, task_id, cursor)
                    if pending:
                        continue
                    yield ServerSentEvent(data={"status": record["status"]}, event="stream_end")
                    return
                await asyncio.sleep(0.2)

    if (frontend_dir / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=frontend_dir / "assets"), name="assets")

    @app.get("/", include_in_schema=False)
    def index():
        if not (frontend_dir / "index.html").is_file():
            raise HTTPException(503, "前端尚未构建，请先在 frontend 目录执行 npm install 和 npm run build。")
        return FileResponse(frontend_dir / "index.html")

    return app
