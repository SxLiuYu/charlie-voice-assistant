"""Shared LLM state: connection pool, brain cache, intent cache, config constants.

Extracted from voice_agent.py so that both voice_agent.py and agent/llm.py
can import the same mutable state objects without circular dependencies.

All objects here are created once at import time and shared by reference
(原地修改, 不重绑) across modules.
"""
import os, re, time, threading, logging
from collections import OrderedDict

import requests
import requests.adapters

log = logging.getLogger("magic")

# ===== LLM endpoints =====
FINNA = os.getenv("FINNA_BASE", "https://www.finna.com.cn/v1")
ARK_BASE = os.getenv("ARK_BASE", "https://ark.cn-beijing.volces.com/api/plan/v3")
ARK_KEY = os.getenv("ARK_KEY", "")
ARK_MODEL = os.getenv("ARK_MODEL", "ark-code-latest")

# ===== Constants =====
EMPTY_ASR_TEXT = "(未识别到语音)"
EMPTY_ASR_REPLY = "抱歉，我没听清，请再说一遍。"
INTENT_FAILURE_THRESHOLD = int(os.getenv("ASSISTANT_KID_INTENT_FAILURE_THRESHOLD", "2"))
INTENT_FAILURE_COOLDOWN = float(os.getenv("ASSISTANT_KID_INTENT_FAILURE_COOLDOWN", "30"))

_MAX_BRAIN_FAILURES = 5   # 连续失败5次后自动重建大脑(429限流单独阈值10)
_INTENT_CACHE_MAX = 100
_INTENT_CACHE_TTL = 3600  # 1小时

# ===== HTTP connection pool (调优: max_connections=10, keep_alive=30s) =====
session = requests.Session()
session.headers.update({"Connection": "keep-alive"})
_adapter = requests.adapters.HTTPAdapter(
    pool_connections=10,
    pool_maxsize=10,
    max_retries=0,
    pool_block=False,
)
session.mount("https://", _adapter)
session.mount("http://", _adapter)

# ===== Regex patterns =====
UNKNOWN_KWARG_RE = re.compile(r"got an unexpected keyword argument '([^']+)'")
SENTENCE_END = re.compile(r'[。！？；\n]')
COMMA_SOFT = re.compile(r'[，,]')
MIN_CHUNK = 15
MAX_CHUNK = 80

# ===== Brain cache state =====
brains: dict = {}              # key=mcp_set, value=Assistant实例
brain_build_time: float = 0
current_user_id: str = "default"
brain_failures: int = 0
brain_total_failures: int = 0
brain_last_failure: float = 0
brain_last_success: float = 0
brain_lock = threading.Lock()

# ===== Intent classifier state =====
intent_failures: int = 0
intent_disabled_until: float = 0.0
intent_cache_lock = threading.Lock()
intent_cache: OrderedDict = OrderedDict()

# ===== Ollama fallback system message =====
OLLAMA_SIMPLE_SYSTEM_MSG = (
    "你是Charlie，搭档级AI助理。直接、偶尔幽默、不废话。\n"
    "回复规则：\n"
    "1. 直接说答案，别输出占用语，第一句就是答案。\n"
    "2. 简洁但不冷漠——偶尔可以带一句人话。\n"
    "3. 不知道的就说不知道，别编造。\n"
    "4. 你觉得有问题就说出来，别盲目执行。\n"
    "当前时间：{time}。"
)
