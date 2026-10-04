"""本地浏览器控制台入口；使用 Conda agent 环境启动。"""

import argparse
from pathlib import Path
import uvicorn

from api.app import create_app


def main():
    parser = argparse.ArgumentParser(description="Nitty Agent 本地控制台")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", type=Path, help="任务数据目录；默认 .agent-data")
    args = parser.parse_args()
    print(f"请在浏览器打开 http://127.0.0.1:{args.port}")
    # 单个 API 进程管理桌面互斥；不要通过 uvicorn --workers 创建多个调度器。
    uvicorn.run(create_app(data_dir=args.data_dir), host="127.0.0.1", port=args.port, timeout_graceful_shutdown=10)


if __name__ == "__main__":
    main()
