import asyncio
import json
import os
from datetime import datetime
from pathlib import Path

LOG_DIR = Path(__file__).parent / "logs"
LOG_FILE = LOG_DIR / "query_trace.jsonl"

# 确保日志目录存在
os.makedirs(LOG_DIR, exist_ok=True)


def _build_trace_data(
    trace_id: str,
    user_message: str,
    parsed_intent: dict,
    found_count: int,
    final_reply: str,
    context: dict = None,
    error: str = None,
) -> dict:
    """构建 trace 日志数据结构。"""
    trace_data = {
        "timestamp": datetime.now().isoformat(),
        "level": "ERROR" if error else "INFO",
        "traceId": trace_id,
        "query": user_message,
        "intent": parsed_intent,
        "results_count": found_count,
        "reply": final_reply,
        "context": context,
    }
    if error:
        trace_data["error"] = error
    return trace_data


def _write_trace_sync(trace_data: dict):
    """同步写入单条 trace 到 JSONL 文件（在线程池中执行时不阻塞 Event Loop）。"""
    line = json.dumps(trace_data, ensure_ascii=False)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


async def log_chat_trace_async(
    trace_id: str,
    user_message: str,
    parsed_intent: dict,
    found_count: int,
    final_reply: str,
    context: dict = None,
    error: str = None,
):
    """异步版 trace 日志写入（通过线程池避免阻塞 Event Loop）。"""
    trace_data = _build_trace_data(trace_id, user_message, parsed_intent, found_count, final_reply, context, error)
    await asyncio.to_thread(_write_trace_sync, trace_data)


def log_chat_trace(
    trace_id: str,
    user_message: str,
    parsed_intent: dict,
    found_count: int,
    final_reply: str,
    context: dict = None,
    error: str = None,
):
    """同步版 trace 日志写入（供测试和非异步上下文使用）。"""
    trace_data = _build_trace_data(trace_id, user_message, parsed_intent, found_count, final_reply, context, error)
    _write_trace_sync(trace_data)


def read_traces(limit: int = 20) -> list[dict]:
    """读取最近 N 条 trace 日志（倒序，最新在前）。"""
    if not LOG_FILE.exists():
        return []
    traces = []
    with open(LOG_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    traces.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return traces[-limit:][::-1]


def find_trace(trace_id: str) -> dict | None:
    """按 trace_id 查找单条 trace。"""
    if not LOG_FILE.exists():
        return None
    with open(LOG_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                if entry.get("traceId") == trace_id:
                    return entry
            except json.JSONDecodeError:
                continue
    return None
