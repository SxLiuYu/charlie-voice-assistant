#!/usr/bin/env python3
"""ASR 评测工具：遍历 data/asr_dump/*.wav，对每个文件分别跑各 provider，
与同名 .txt（若有标注）算 CER；无标注时只输出各 provider 识别文本对照 + 耗时。

输出 markdown 表格到 stdout。
"""
import os
import sys
import re
import time
import wave
from pathlib import Path
from dotenv import load_dotenv

# ── 路径 ──────────────────────────────────────────────────────────────
PROJECT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_DIR / ".env")
os.environ.setdefault("ASSISTANT_KID_DATA_DIR", str(PROJECT_DIR / "data"))
sys.path.insert(0, str(PROJECT_DIR))

import logging
logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

# ── CER 计算（字符级编辑距离，不依赖第三方库）─────────────────────────

def _cer(reference: str, hypothesis: str) -> float:
    """计算字符错误率（Character Error Rate）。返回 0~1 之间的浮点数。"""
    if not reference:
        return 0.0 if not hypothesis else 1.0
    ref_len = len(reference)
    hyp_len = len(hypothesis)
    # 动态规划计算最小编辑距离
    dp = [[0] * (hyp_len + 1) for _ in range(ref_len + 1)]
    for i in range(ref_len + 1):
        dp[i][0] = i
    for j in range(hyp_len + 1):
        dp[0][j] = j
    for i in range(1, ref_len + 1):
        for j in range(1, hyp_len + 1):
            if reference[i - 1] == hypothesis[j - 1]:
                dp[i][j] = dp[i - 1][j - 1]
            else:
                dp[i][j] = 1 + min(
                    dp[i - 1][j],      # delete
                    dp[i][j - 1],      # insert
                    dp[i - 1][j - 1],  # substitute
                )
    distance = dp[ref_len][hyp_len]
    return distance / ref_len


# ── WAV 工具 ──────────────────────────────────────────────────────────

def read_wav(path: Path) -> bytes:
    """读取 WAV 文件，返回原始 bytes（含头）"""
    return path.read_bytes()


# ── ASR Provider 包装 ─────────────────────────────────────────────────

def run_sensevoice(audio_bytes: bytes) -> tuple[str, float]:
    """运行本地 SenseVoice，返回 (text, elapsed_ms)"""
    from agent.asr_tts import _asr_sense_voice
    t0 = time.perf_counter()
    try:
        text = _asr_sense_voice(audio_bytes, "wav")
    except Exception as e:
        return f"[ERROR] {e}", (time.perf_counter() - t0) * 1000
    return text or "", (time.perf_counter() - t0) * 1000


def run_stepfun(audio_bytes: bytes) -> tuple[str, float]:
    """运行云端 StepFun ASR，返回 (text, elapsed_ms)"""
    from agent.asr_tts import _asr_stepfun
    t0 = time.perf_counter()
    try:
        text = _asr_stepfun(audio_bytes, "wav")
    except Exception as e:
        return f"[ERROR] {e}", (time.perf_counter() - t0) * 1000
    return text or "", (time.perf_counter() - t0) * 1000


def run_baidu(audio_bytes: bytes) -> tuple[str, float]:
    """运行百度 ASR，返回 (text, elapsed_ms)"""
    from agent.asr_tts import _asr_baidu
    t0 = time.perf_counter()
    try:
        text = _asr_baidu(audio_bytes, "wav")
    except Exception as e:
        return f"[ERROR] {e}", (time.perf_counter() - t0) * 1000
    return text or "", (time.perf_counter() - t0) * 1000


# ── 主流程 ────────────────────────────────────────────────────────────

def main():
    dump_dir = PROJECT_DIR / "data" / "asr_dump"
    if not dump_dir.exists():
        print(f"# ASR 评测结果\n\n未找到目录: {dump_dir}")
        sys.exit(0)

    wav_files = sorted(dump_dir.glob("*.wav"))
    if not wav_files:
        print(f"# ASR 评测结果\n\n{dump_dir} 下无 .wav 文件")
        sys.exit(0)

    providers = [
        ("sensevoice", run_sensevoice),
        ("stepfun", run_stepfun),
        ("baidu", run_baidu),
    ]

    rows = []
    has_annotation = False

    for wav_path in wav_files:
        txt_path = wav_path.with_suffix(".txt")
        reference = ""
        if txt_path.exists():
            reference = txt_path.read_text(encoding="utf-8").strip()
            has_annotation = True

        audio_bytes = read_wav(wav_path)
        row = {"file": wav_path.name, "ref": reference}
        for name, fn in providers:
            try:
                text, elapsed = fn(audio_bytes)
            except Exception as e:
                text, elapsed = f"[ERROR] {e}", 0.0
            row[name] = text
            row[f"{name}_ms"] = f"{elapsed:.0f}"
            if reference:
                row[f"{name}_cer"] = f"{_cer(reference, text):.3f}"
        rows.append(row)

    # ── 输出 Markdown ─────────────────────────────────────────────────
    if has_annotation:
        headers = ["文件", "标注"] + [p for p, _ in providers] + [f"{p}_CER" for p, _ in providers]
    else:
        headers = ["文件"] + [p for p, _ in providers] + [f"{p}_耗时" for p, _ in providers]

    col_widths = [max(len(h), max((len(str(r.get(h.lower(), ""))) for r in rows), default=0)) for h in headers]
    line = "  ".join("-" * w for w in col_widths)

    print("# ASR 评测结果\n")
    print(f"*样本数: {len(rows)}*  \n")
    header_row = "  ".join(h.ljust(col_widths[i]) for i, h in enumerate(headers))
    print(header_row)
    print(line)
    for row in rows:
        cells = []
        for h in headers:
            if h == "文件":
                cells.append(row.get("file", ""))
            elif h == "标注":
                cells.append(row.get("ref", ""))
            elif h.endswith("_CER"):
                cells.append(row.get(h.lower(), ""))
            elif h.endswith("_耗时"):
                cells.append(row.get(h.lower(), ""))
            else:
                cells.append(row.get(h, ""))
        print("  ".join(str(c).ljust(col_widths[i]) for i, c in enumerate(cells)))


if __name__ == "__main__":
    main()
