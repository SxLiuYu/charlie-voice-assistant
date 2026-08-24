#!/usr/bin/env python3
"""Charlie Provider 性能基准测试

用法:
  python bench_providers.py llm     # 测 LLM 各 provider 延迟
  python bench_providers.py asr     # 测 ASR 各 provider 延迟
  python bench_providers.py tts     # 测 TTS 各 provider 延迟
  python bench_providers.py all     # 测全部
"""
import time, sys, os, json, struct, wave, tempfile
from pathlib import Path
from dotenv import load_dotenv

# ── 路径 ──────────────────────────────────────────────
PROJECT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_DIR / ".env")
os.environ.setdefault("ASSISTANT_KID_DATA_DIR", str(PROJECT_DIR / "data"))
sys.path.insert(0, str(PROJECT_DIR))

import logging
logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

# ── 工具函数 ──────────────────────────────────────────
def median(nums: list[float]) -> float:
    if not nums:
        return 0.0
    s = sorted(nums)
    n = len(s)
    return s[n // 2] if n % 2 == 1 else (s[n // 2 - 1] + s[n // 2]) / 2


def make_test_wav(path: str = "/tmp/test_speech.wav", duration_s: float = 3.0, sample_rate: int = 16000) -> str:
    """生成一段静音+正弦波的测试 WAV（如果不存在）"""
    if os.path.exists(path) and os.path.getsize(path) > 1000:
        return path
    n_samples = int(sample_rate * duration_s)
    # 440Hz 正弦波 + 静音背景
    import math
    data = bytearray()
    for i in range(n_samples):
        t = i / sample_rate
        sample = int(16000 * 0.3 * math.sin(2 * math.pi * 440 * t))
        sample = max(-32768, min(32767, sample))
        data += struct.pack("<h", sample)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(bytes(data))
    return path


def print_table(title: str, headers: list[str], rows: list[list[str]]) -> None:
    col_widths = [max(len(h), max((len(str(r[i])) for r in rows), default=0)) for i, h in enumerate(headers)]
    line = "  ".join("-" * w for w in col_widths)
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")
    header_row = "  ".join(h.ljust(col_widths[i]) for i, h in enumerate(headers))
    print(header_row)
    print(line)
    for row in rows:
        print("  ".join(str(cell).ljust(col_widths[i]) for i, cell in enumerate(row)))
    print()


# ════════════════════════════════════════════════════════
# LLM 测试
# ════════════════════════════════════════════════════════
def bench_llm(rounds: int = 5) -> None:
    from app.llm_config import (
        is_agnes_configured, is_sagnes_configured,
        is_stepfun_configured, is_glm_configured, is_ark_configured,
        PROVIDERS, _get_provider_cfg,
    )
    import requests as req

    PROVIDER_CHECKS = [
        ("agnes",  is_agnes_configured),
        ("sagnes", is_sagnes_configured),
        ("stepfun", is_stepfun_configured),
        ("glm",    is_glm_configured),
        ("ark",    is_ark_configured),
    ]

    # 场景1: 裸短句（轻量通道形态）；场景2: 带system prompt（大脑形态）
    SYSTEM_PROMPT = (
        "你是 Charlie，一个智能语音助手。回复简洁有力，适合语音播报。"
        "最多2句、40字以内，第一句就是答案，禁止列表和markdown。"
    )
    scenarios = [
        ("裸短句", None, "你能干什么？", 60),
        ("带system", SYSTEM_PROMPT, "你能干什么？", 100),
    ]
    for name, check_fn in PROVIDER_CHECKS:
        if not check_fn():
            continue
        cfg = _get_provider_cfg(name)
        base, key, model = cfg["base"], cfg["key"], cfg["model"]
        headers = {"Authorization": f"Bearer {key}"}
        extra = {}
        if "stepfun" in base.lower():
            extra = {"thinking": {"type": "disabled"}, "reasoning_effort": "low"}
        else:
            extra = {"thinking": {"type": "disabled"}}

        print(f"\n── {name} ({model}) ──")
        for scen_label, sysmsg, user_q, max_tok in scenarios:
            messages = []
            if sysmsg:
                messages.append({"role": "system", "content": sysmsg})
            messages.append({"role": "user", "content": user_q})
            first_token_times, total_times, replies, errors = [], [], [], []
            for _ in range(rounds):
                t0 = time.time()
                try:
                    resp = req.post(
                        f"{base}/chat/completions",
                        json={"model": model, "messages": messages,
                              "stream": True, "max_tokens": max_tok,
                              "temperature": 0, **({"extra_body": extra} if False else extra)},
                        headers=headers,
                        timeout=(2, 30),
                        stream=True,
                    )
                    resp.raise_for_status()
                    ft_time, full = None, ""
                    for line in resp.iter_lines():
                        if not line or line.startswith(b"data: [DONE]"):
                            continue
                        if line.startswith(b"data: "):
                            try:
                                chunk = json.loads(line[6:])
                                choices = chunk.get("choices", [])
                                delta = (choices[0].get("delta", {}) if choices else {})
                                c = delta.get("content") or ""
                                if c and ft_time is None:
                                    ft_time = time.time() - t0
                                full += c
                            except json.JSONDecodeError:
                                pass
                    total = time.time() - t0
                    first_token_times.append(ft_time or total)
                    total_times.append(total)
                    replies.append(full.strip())
                except Exception as e:
                    errors.append(str(e)[:50])
            ok_n = len(total_times)
            med_ft = f"{median(first_token_times)*1000:.0f}ms" if first_token_times else "—"
            all_total = f"{min(total_times)*1000:.0f}/{median(total_times)*1000:.0f}/{max(total_times)*1000:.0f}ms" if total_times else "—"
            sample = replies[0][:36].replace("\n", " ") if replies else (errors[0] if errors else "")
            print(f"  [{scen_label}] 首Token中位 {med_ft} | 总延迟 最小/中位/最大 {all_total} | 成功{ok_n}/{rounds}")
            print(f"      样例: {sample}")


# ════════════════════════════════════════════════════════
# ASR 测试
# ════════════════════════════════════════════════════════
def bench_asr(rounds: int = 3) -> None:
    from agent.asr_tts import _ASR_PROVIDERS, _load_sense_voice, ASR_PRIORITY
    import requests as req

    make_test_wav()
    with open("/tmp/test_speech.wav", "rb") as f:
        wav_bytes = f.read()

    # 找出有 key 的 provider
    providers = {}
    for pname in list(_ASR_PROVIDERS.keys()) + ["sensevoice"]:
        if pname == "sensevoice":
            r = _load_sense_voice()
            providers["sensevoice"] = r is not None
        elif pname == "baidu":
            from agent.asr_tts import BAIDU_APP_ID, BAIDU_API_KEY
            providers["baidu"] = bool(BAIDU_APP_ID and BAIDU_API_KEY)
        elif pname == "stepfun":
            providers["stepfun"] = bool(os.getenv("STEPFUN_KEY", ""))
        else:
            providers[pname] = False

    results = []
    for pname, configured in providers.items():
        if not configured:
            continue
        fn = _ASR_PROVIDERS.get(pname)
        if fn is None and pname != "sensevoice":
            continue

        times: list[float] = []
        texts: list[str] = []
        errors: list[str] = []
        for _ in range(rounds):
            t0 = time.time()
            try:
                if pname == "sensevoice":
                    from agent.asr_tts import _asr_sense_voice
                    text = _asr_sense_voice(wav_bytes)
                else:
                    text = fn(wav_bytes)
                times.append(time.time() - t0)
                texts.append(text[:40])
            except Exception as e:
                errors.append(str(e)[:50])
                times.append(None)

        ok = len(errors) == 0
        rows = [t for t in times if t is not None]
        med = f"{median(rows)*1000:.0f}ms" if rows else "—"
        sample = texts[0][:30] if texts else "—"
        status = "✅" if ok else f"❌ {errors[0]}"
        results.append([pname, med, sample, f"{len(rows)}/{rounds}", status])

    print_table("ASR Provider 性能", ["Provider", "中位延迟", "识别结果(首次)", "成功/总次数", "状态"], results)


# ════════════════════════════════════════════════════════
# TTS 测试
# ════════════════════════════════════════════════════════
def bench_tts(rounds: int = 3) -> None:
    from agent.asr_tts import _TTS_PROVIDERS, TTS_PRIORITY

    text = "你好，我是查理"
    # 找出有 key 的 provider
    providers = {}
    for pname in list(_TTS_PROVIDERS.keys()):
        if pname == "baidu":
            from agent.asr_tts import BAIDU_APP_ID, BAIDU_API_KEY
            providers["baidu"] = bool(BAIDU_APP_ID and BAIDU_API_KEY)
        elif pname == "finna":
            providers["finna"] = bool(os.getenv("FINNA_API_KEY", ""))
        elif pname == "stepfun":
            providers["stepfun"] = bool(os.getenv("STEPFUN_KEY", ""))
        else:
            providers[pname] = False

    results = []
    for pname, configured in providers.items():
        if not configured:
            continue
        fn = _TTS_PROVIDERS[pname]

        times: list[float] = []
        sizes: list[int] = []
        errors: list[str] = []
        for _ in range(rounds):
            t0 = time.time()
            try:
                audio = fn(text)
                elapsed = time.time() - t0
                times.append(elapsed)
                sizes.append(len(audio))
            except Exception as e:
                errors.append(str(e)[:50])
                times.append(None)

        ok = len(errors) == 0
        rows = [t for t in times if t is not None]
        med = f"{median(rows)*1000:.0f}ms" if rows else "—"
        med_size = f"{median([s for s in sizes]):.0f}B" if sizes else "—"
        status = "✅" if ok else f"❌ {errors[0]}"
        results.append([pname, med, med_size, f"{len(rows)}/{rounds}", status])

    print_table("TTS Provider 性能", ["Provider", "中位延迟", "音频大小", "成功/总次数", "状态"], results)


# ════════════════════════════════════════════════════════
# 入口
# ════════════════════════════════════════════════════════
if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] == "all":
        bench_llm()
        bench_asr()
        bench_tts()
    elif sys.argv[1] == "llm":
        bench_llm()
    elif sys.argv[1] == "asr":
        bench_asr()
    elif sys.argv[1] == "tts":
        bench_tts()
    else:
        print(f"未知命令: {sys.argv[1]}\n用法: {sys.argv[0]} [llm|asr|tts|all]")
        sys.exit(1)
