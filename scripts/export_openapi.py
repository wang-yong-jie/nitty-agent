"""离线导出接口契约，不启动服务或模型。"""

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from api.app import create_app


if __name__ == "__main__":
    output = ROOT / "frontend" / "openapi.json"
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(create_app().openapi(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"已导出 {output}")
