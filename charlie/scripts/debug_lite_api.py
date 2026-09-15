#!/usr/bin/env python3
"""Debug: check actual content arrival time for full payload."""
import os
import sys
import json
import time

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

from pathlib import Path
env_path = Path(PROJECT_DIR) / ".env"
if env_path.exists():
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())

import requests
from app.llm_config import AGNES_BASE, AGNES_KEY, AGNES_MODEL

# Build the same payload as _chat_lite_stream
import agent.llm as _llm_mod
import importlib
importlib.reload(_llm_mod)

# Capture what _chat_lite_stream would send
_llm_mod._session = requests
captured = {}
orig_post = requests.post

def capture_post(url, **kwargs):
    captured['url'] = url
    captured['kwargs'] = kwargs
    return orig_post(url, **kwargs)

requests.post = capture_post

# Run with timeout disabled to see actual timing
import agent.llm as llm_mod
orig_timeout = llm_mod._LITE_FIRST_CONTENT_TIMEOUT
llm_mod._LITE_FIRST_CONTENT_TIMEOUT = 10.0  # Disable timeout for debug

print("=== Running _chat_lite_stream with 10s timeout ===")
start = time.time()
results = list(llm_mod._chat_lite_stream("讲个笑话", session_id="debug"))
elapsed = time.time() - start

llm_mod._LITE_FIRST_CONTENT_TIMEOUT = orig_timeout

print(f"Results count: {len(results)}")
print(f"Elapsed: {elapsed:.2f}s")
if results:
    print(f"First sentence: {results[0][0][:100]}")
    print(f"Full reply: {results[-1][1][:200]}")
