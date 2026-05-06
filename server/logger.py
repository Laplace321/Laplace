import json
import logging
import os
from datetime import datetime
from pathlib import Path

LOG_DIR = Path(__file__).parent / "logs"
LOG_FILE = LOG_DIR / "query_trace.jsonl"

# 确保日志目录存在
os.makedirs(LOG_DIR, exist_ok=True)

# 配置 logger
logger = logging.getLogger("LaplaceTracer")
logger.setLevel(logging.INFO)

# 如果还没有 handler，则添加
if not logger.handlers:
    # 写入 JSONL 文件
    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    
    class JsonlFormatter(logging.Formatter):
        def format(self, record):
            # 将 record.msg 假设为 dict，并转成 json
            log_obj = {
                "timestamp": datetime.now().isoformat(),
                "level": record.levelname
            }
            if isinstance(record.msg, dict):
                log_obj.update(record.msg)
            else:
                log_obj["message"] = str(record.msg)
            return json.dumps(log_obj, ensure_ascii=False)
            
    file_handler.setFormatter(JsonlFormatter())
    logger.addHandler(file_handler)

def log_chat_trace(trace_id: str, user_message: str, parsed_intent: dict, found_count: int, final_reply: str, context: dict = None, error: str = None):
    """
    记录完整的查询链路信息。
    """
    trace_data = {
        "traceId": trace_id,
        "query": user_message,
        "intent": parsed_intent,
        "results_count": found_count,
        "reply": final_reply,
        "context": context
    }
    if error:
        trace_data["error"] = error
        logger.error(trace_data)
    else:
        logger.info(trace_data)


def read_traces(limit: int = 20) -> list[dict]:
    """读取最近 N 条 trace 日志（倒序，最新在前）。"""
    if not LOG_FILE.exists():
        return []
    traces = []
    with open(LOG_FILE, "r", encoding="utf-8") as f:
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
    with open(LOG_FILE, "r", encoding="utf-8") as f:
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
