"""仅供浏览器离线验收；使用内存工具，不读取真实文件或调用真实模型。"""

import argparse
from pathlib import Path
import uvicorn

from api.app import create_app
from bootstrap import ROOT
from service.manager import TaskManager
from tests.service_workers import fixture_worker


def create_fixture_app(data_dir):
    return create_app(
        data_dir=data_dir, trace_dir=data_dir / "logs",
        manager_factory=lambda data, trace: TaskManager(data, trace, worker_target=fixture_worker),
        validate_options=lambda options: options.validate(),
        settings_provider=lambda: dict(providers={"deepseek": True, "openai": True, "claude": True}, desktop_available=False),
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--data-dir", type=Path, default=ROOT / ".agent-data" / "ui-validation")
    args = parser.parse_args()
    print("Offline UI fixture: [fail] simulates failure; [slow] waits for cancellation.")
    uvicorn.run(create_fixture_app(args.data_dir), host="127.0.0.1", port=args.port)
