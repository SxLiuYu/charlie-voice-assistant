#!/usr/bin/env python3
"""Round 10 real API validation: _chat_lite_stream with 5 casual prompts.

Run: .venv/bin/python scripts/validate_lite_api.py
"""
import os
import sys
import time

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

# Load .env before importing modules
from pathlib import Path
env_path = Path(PROJECT_DIR) / ".env"
if env_path.exists():
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())

# Ensure fresh module state
import importlib
import agent.llm as _llm_mod
importlib.reload(_llm_mod)

from agent.llm import _chat_lite_stream, is_agnes_configured

PROMPTS = [
    "你今天过得怎么样",
    "讲个笑话",
    "帮我推荐个电影",
    "你觉得人工智能会取代人类吗",
    "明天穿什么衣服好",
]


def main():
    print(f"Agnes configured: {is_agnes_configured()}")
    print(f"Lite endpoint: {_llm_mod._lite_endpoint()}")
    print(f"Constants: MAX_TOKENS={_llm_mod._LITE_MAX_TOKENS}, HISTORY_TURNS={_llm_mod._LITE_HISTORY_TURNS}, FIRST_CONTENT_TIMEOUT={_llm_mod._LITE_FIRST_CONTENT_TIMEOUT}")
    print()

    successes = 0
    ttfts = []

    for prompt in PROMPTS:
        print(f">> Prompt: {prompt}")
        t0 = time.time()
        first_at = None
        full_text = ""
        try:
            for sentence, full in _chat_lite_stream(prompt, session_id="round10-validation"):
                if first_at is None:
                    first_at = time.time() - t0
                full_text = full
            elapsed = time.time() - t0
            if full_text.strip():
                successes += 1
                ttfts.append(first_at)
                print(f"   SUCCESS ({elapsed:.2f}s, TTFT {first_at:.2f}s): {full_text[:80]}")
            else:
                print(f"   EMPTY ({elapsed:.2f}s) — fell back to brain")
        except Exception as e:
            elapsed = time.time() - t0
            print(f"   ERROR ({elapsed:.2f}s): {e}")

    print()
    print(f"Results: {successes}/{len(PROMPTS)} succeeded")
    if ttfts:
        print(f"TTFT stats: min={min(ttfts):.2f}s, max={max(ttfts):.2f}s, avg={sum(ttfts)/len(ttfts):.2f}s")


if __name__ == "__main__":
    main()
