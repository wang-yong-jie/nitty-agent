"""运行真实桌面任务，独立验收产物，输出可比较的结果；不提供验收状态给模型。"""

import argparse
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import time

from bootstrap import AgentOptions, agent_session
from evaluation.desktop_fixture import TASKS
from tracing import JsonlTrace


def read_state(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def summarize(results):
    count = len(results)
    claimed = [result for result in results if result["agent_completed"]]
    return {"tasks": count, "task_success_rate": sum(result["success"] for result in results) / count if count else 0,
            "completion_precision": sum(result["success"] for result in claimed) / len(claimed) if claimed else None,
            "average_seconds": sum(result["seconds"] for result in results) / count if count else 0,
            "average_steps": sum(result["steps"] for result in results) / count if count else 0,
            "recovery_successes": sum(result["success"] and result["no_progress_steps"] > 0 for result in results)}


def run_case(name, options, directory, *, geometry="900x650+80+80"):
    case_dir = directory / name
    case_dir.mkdir(parents=True)
    state_path = case_dir / "fixture-state.json"
    process = subprocess.Popen([sys.executable, "-m", "evaluation.desktop_fixture", "--scenario", name,
                                "--state", str(state_path), "--geometry", geometry])
    started = time.perf_counter()
    answer, error, verification, completed = None, None, None, False
    try:
        deadline = time.monotonic() + 10
        while not read_state(state_path).get("ready"):
            if process.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError("评测窗口未能启动。")
            time.sleep(0.1)
        with JsonlTrace(case_dir / "run.trace.jsonl") as trace, agent_session(
            replace(options, record_desktop=True), trace=trace, verbose=False, artifact_dir=case_dir / "frames",
        ) as session:
            try:
                answer = session.agent.run(TASKS[name])
                completed = True
            finally:
                verification = session.agent.last_state.verification if session.agent.last_state else None
    except Exception as failure:
        error = f"{type(failure).__name__}: {failure}"
    finally:
        process.terminate()
        process.wait(timeout=10)
    state = read_state(state_path)
    success = state.get("success", False)
    if name == "save":
        output = case_dir / "saved-note.txt"
        success = success and output.is_file() and output.read_text(encoding="utf-8") == "保存测试：中文与 English"
    trajectory = case_dir / "frames" / "trajectory.jsonl"
    steps = [json.loads(line) for line in trajectory.read_text(encoding="utf-8").splitlines()] if trajectory.exists() else []
    return {"scenario": name, "success": bool(success), "agent_completed": completed, "verification": verification,
            "seconds": round(time.perf_counter() - started, 2), "steps": len(steps), "answer": answer, "error": error,
            "no_progress_steps": sum(step["recovery"]["no_progress_steps"] > 0 for step in steps), "geometry": geometry}


def main():
    parser = argparse.ArgumentParser(description="Nitty 真实 Windows 桌面评测（会打开独立测试窗口）")
    parser.add_argument("--cases", nargs="+", choices=TASKS, default=list(TASKS))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--provider", choices=["deepseek", "openai", "claude"], default="deepseek")
    parser.add_argument("--model")
    parser.add_argument("--max-turns", type=int, default=40)
    parser.add_argument("--geometry", default="900x650+80+80")
    args = parser.parse_args()
    options = AgentOptions(provider=args.provider, model=args.model, desktop=True, max_turns=args.max_turns)
    if args.output.exists():
        parser.error("结果文件已存在，请使用新路径保留对照结果。")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    directory = args.output.parent / f"{args.output.stem}.artifacts"
    if directory.exists():
        parser.error("评测产物目录已存在，请使用新结果路径。")
    results = []
    for name in args.cases:
        result = run_case(name, options, directory, geometry=args.geometry)
        results.append(result)
        print(json.dumps({key: result[key] for key in ("scenario", "success", "seconds", "steps")}, ensure_ascii=False), flush=True)
    report = {"schema_version": 1, "results": results, "summary": summarize(results)}
    if args.baseline:
        baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
        report["baseline_summary"] = baseline["summary"]
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False))


if __name__ == "__main__":
    main()
