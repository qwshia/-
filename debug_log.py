"""调试日志：把每一步的完整内容全量落盘，供测试排查。

用法：
    from debug_log import log_thread, read_thread
    log_thread(thread_id, "act_input", {"msg": msg})
    log_thread(thread_id, "npc_raw:npc_1", {"prompt": p, "raw_output": full})
    content = read_thread(thread_id)   # 按需读取全文（按钮触发），不自动刷屏

日志只写文件 ./debug_logs/{thread_id}.log（追加），不再打印控制台，避免刷屏。
"""
import json
import os
from datetime import datetime

_LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "debug_logs")


def _dump(obj):
    try:
        return json.dumps(obj, ensure_ascii=False, default=str, indent=2)
    except Exception:
        return str(obj)


def _path_for(thread_id: str) -> str:
    tid = (thread_id or "_global").strip() or "_global"
    return os.path.join(_LOG_DIR, f"{tid}.log")


def log_thread(thread_id: str, stage: str, payload) -> None:
    """全量记录一条日志到文件。thread_id 为空时用 '_global' 兜底。"""
    path = _path_for(thread_id)
    try:
        os.makedirs(_LOG_DIR, exist_ok=True)
    except Exception:
        pass
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    text = _dump(payload)
    header = f"\n========== [{ts}] {stage} ==========\n"
    footer = "\n"
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(header + text + footer)
    except Exception:
        pass


def log_snapshot(thread_id: str, text: str) -> None:
    """追加一条世界快照叙述（纯文本，按时间顺序），供"日志"按钮查看。"""
    path = _path_for(thread_id)
    try:
        os.makedirs(_LOG_DIR, exist_ok=True)
    except Exception:
        pass
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {text}\n"
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def read_thread(thread_id: str, tail_lines: int = 0) -> str:
    """读取指定线程的日志全文；tail_lines>0 时只返回末尾 N 行。"""
    path = _path_for(thread_id)
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception:
        return ""
    if tail_lines > 0:
        lines = content.splitlines()
        return "\n".join(lines[-tail_lines:])
    return content


def list_threads() -> list:
    """列出所有已记录的日志线程 id（按文件名，不含 .log）。"""
    try:
        return sorted(
            [f[:-4] for f in os.listdir(_LOG_DIR) if f.endswith(".log")],
            key=lambda t: os.path.getmtime(os.path.join(_LOG_DIR, f"{t}.log")),
            reverse=True,
        )
    except Exception:
        return []
