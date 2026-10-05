"""真实 Tk 桌面测试窗口；所有产物位于评测的专用目录。"""

import argparse
import json
from pathlib import Path
import tkinter as tk
import time


TASKS = {
    "chinese": "在 Nitty 桌面评测窗口输入『你好，Nitty Agent！』，点击『验证输入』，确认显示『任务完成』。",
    "save": "在 Nitty 桌面评测窗口输入『保存测试：中文与 English』，点击『保存文件』，确认显示『保存成功』。",
    "dialog": "在 Nitty 桌面评测窗口点击『打开确认窗口』，在弹窗点击『确认完成』，确认主窗口显示『任务完成』。",
    "scroll": "在 Nitty 桌面评测窗口向下滚动列表，找到并点击『列表底部：完成任务』，确认显示『任务完成』。",
    "small_target": "在 Nitty 桌面评测窗口找到右上角的绿色 ✓ 小按钮并点击，确认显示『任务完成』。",
    "recovery": "在 Nitty 桌面评测窗口点击『开始加载』，等待加载结束，然后点击『确认完成』，确认显示『任务完成』。",
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", choices=TASKS, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--geometry", default="900x650+80+80")
    args = parser.parse_args()
    root = tk.Tk()
    root.title(f"Nitty 桌面评测 — {args.scenario}")
    root.geometry(args.geometry)
    state = {"scenario": args.scenario, "ready": False, "success": False, "events": []}
    target = None

    def save():
        if target is not None:
            state["target_bbox"] = [target.winfo_rootx(), target.winfo_rooty(),
                                    target.winfo_rootx() + target.winfo_width(), target.winfo_rooty() + target.winfo_height()]
        temporary = args.state.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        temporary.replace(args.state)

    status = tk.Label(root, text="等待执行", font=("Microsoft YaHei", 15), fg="#445599")
    tk.Label(root, text="Nitty 真实桌面评测", font=("Microsoft YaHei", 19)).pack(pady=16)
    tk.Label(root, text=TASKS[args.scenario], wraplength=820, font=("Microsoft YaHei", 12)).pack(pady=12)
    status.pack(pady=12)

    def complete(message="任务完成"):
        state["success"] = True
        state["events"].append({"type": "complete", "time": time.time()})
        status.config(text=message, fg="green")
        save()

    if args.scenario in {"chinese", "save"}:
        text = tk.Text(root, height=8, font=("Microsoft YaHei", 14))
        text.pack(padx=25, pady=15, fill="x")

        def submit():
            value = text.get("1.0", "end-1c")
            wanted = "你好，Nitty Agent！" if args.scenario == "chinese" else "保存测试：中文与 English"
            if value != wanted:
                status.config(text="内容不匹配，请检查输入", fg="red")
                return
            if args.scenario == "save":
                (args.state.parent / "saved-note.txt").write_text(value, encoding="utf-8")
            complete("任务完成" if args.scenario == "chinese" else "保存成功")

        tk.Button(root, text="验证输入" if args.scenario == "chinese" else "保存文件", command=submit).pack(pady=15)
    elif args.scenario == "dialog":
        def dialog():
            window = tk.Toplevel(root)
            window.title("确认窗口")
            window.geometry("440x220+240+180")
            tk.Label(window, text="点击下方按钮完成评测", font=("Microsoft YaHei", 14)).pack(pady=30)
            tk.Button(window, text="确认完成", command=lambda: (complete(), window.destroy())).pack()
            window.transient(root)
            window.grab_set()
        tk.Button(root, text="打开确认窗口", command=dialog).pack(pady=30)
    elif args.scenario == "scroll":
        canvas = tk.Canvas(root, height=360)
        scrollbar = tk.Scrollbar(root, command=canvas.yview)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(fill="both", expand=True, padx=20)
        canvas.configure(yscrollcommand=scrollbar.set)
        inner = tk.Frame(canvas)
        canvas.create_window((0, 0), window=inner, anchor="nw")
        for index in range(30):
            tk.Label(inner, text=f"列表项 {index + 1:02d}", width=90, height=2).pack()
        tk.Button(inner, text="列表底部：完成任务", command=complete).pack(pady=12)
        inner.bind("<Configure>", lambda event: canvas.configure(scrollregion=canvas.bbox("all")))
        root.bind_all("<MouseWheel>", lambda event: canvas.yview_scroll(-int(event.delta / 120), "units"))
    elif args.scenario == "small_target":
        toolbar = tk.Frame(root)
        toolbar.pack(fill="x", padx=24)
        for index in range(14):
            tk.Button(toolbar, text="○", width=2).pack(side="left", padx=2)
        target = tk.Button(toolbar, text="✓", width=2, bg="lightgreen", command=complete)
        target.pack(side="right")
    else:
        confirm = tk.Button(root, text="确认完成", state="disabled", command=complete)
        def load():
            status.config(text="加载中，请等待…")
            start.config(state="disabled")
            root.after(1600, lambda: (status.config(text="加载完成，可以确认"), confirm.config(state="normal")))
        start = tk.Button(root, text="开始加载", command=load)
        start.pack(pady=20)
        confirm.pack(pady=20)

    def ready():
        state["ready"] = True
        save()
        root.lift()
        root.focus_force()
    root.after(300, ready)
    root.mainloop()


if __name__ == "__main__":
    main()
