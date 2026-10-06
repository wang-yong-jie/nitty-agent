"""可重复运行的只读验收；明确区分失败和证据不足。"""

import hashlib
from contracts import AgentCancelled


def verify_checks(checks, environment, observations):
    results = []
    for check in checks:
        try:
            kind = check["kind"]
            if kind == "file_absent":
                try:
                    environment.read_file(check["path"])
                    status, evidence = "unmet", "文件仍存在。"
                except (FileNotFoundError, KeyError):
                    status, evidence = "achieved", "文件不存在，与执行前状态一致。"
            elif kind in {"file_contains", "file_equals", "file_sha256"}:
                data = environment.read_file(check["path"])
                content = data["content"]
                if data.get("truncated"):
                    status, evidence = "uncertain", "文件读取被截断，不能完成验收。"
                else:
                    value = hashlib.sha256(content.encode("utf-8")).hexdigest() if kind == "file_sha256" else content
                    achieved = check["expected"] in value if kind == "file_contains" else check["expected"] == value
                    status, evidence = ("achieved", "文件内容符合验收条件。") if achieved else ("unmet", "文件内容不符合验收条件。")
            elif kind == "command_succeeded":
                item = next((item for item in reversed(observations) if item.tool_call_id == check["call_id"]
                             and item.tool_name == "exec_command"), None)
                data = item.result if item and isinstance(item.result, dict) else None
                if data is None:
                    status, evidence = "uncertain", "没有对应命令的实际执行结果。"
                else:
                    achieved = item.status == "succeeded" and data.get("exit_code") == 0 and not data.get("timed_out")
                    status, evidence = ("achieved", "命令已实际完成且退出码为 0。") if achieved else ("unmet", "命令失败或超时。")
            else:
                status, evidence = "uncertain", "不支持的验收类型。"
        except AgentCancelled:
            raise
        except (FileNotFoundError, KeyError):
            status, evidence = "unmet", "未找到验收文件或执行结果。"
        except Exception as error:
            status, evidence = "uncertain", f"验收未完成：{type(error).__name__}"
        # 条件全文已在计划中保存；观察只带索引，避免大文件期望内容重复进入上下文。
        results.append({"status": status, "evidence": evidence,
                        "check": {key: value for key, value in check.items() if key != "expected"}})
    status = "unmet" if any(item["status"] == "unmet" for item in results) else (
        "uncertain" if not results or any(item["status"] == "uncertain" for item in results) else "achieved")
    return {"status": status, "evidence": "；".join(item["evidence"] for item in results) or "尚无可验收证据。", "checks": results}
