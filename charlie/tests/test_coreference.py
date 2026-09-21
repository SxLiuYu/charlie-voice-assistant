"""多轮指代消解（agent/coreference.py）单测。

覆盖：
  - 就近还原：歌曲/设备/提醒/地点 代词 → 字面实体；
  - 零指代（"再放一遍"）→ 歌曲；
  - 历史空 / 无候选 → 原句透传；
  - 不编造：候选池缺类目时保留原词；
  - 异常兜底：任意失败返回 (current, False, {}) 不抛错；
  - 会话封装 resolve_for_session 拉不到历史时透传。
"""
import os
import sys

import pytest

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

import agent.coreference as cf  # noqa: E402


def test_resolve_song_pronoun():
    cur = "那首歌再放一遍"
    hist = ["放一首周杰伦的歌", "好的，正在播放晴天"]
    out, changed, mp = cf.resolve_references(cur, hist)
    assert changed and "晴天" in out and "那首歌" not in out
    assert mp


def test_resolve_device_pronoun_nearest():
    cur = "把它关了"
    hist = ["打开客厅的灯", "开一下卧室空调"]
    out, changed, mp = cf.resolve_references(cur, hist)
    assert changed
    # 最近优先 → 应命中"卧室空调"而非"客厅的灯"
    assert "卧室空调" in out


def test_resolve_zero_anaphora_song():
    cur = "再放一遍"
    hist = ["放首周杰伦的歌", "好的，播放晴天"]
    out, changed, mp = cf.resolve_references(cur, hist)
    assert changed and "晴天" in out


def test_resolve_reminder_pronoun():
    cur = "那个提醒改到明天"
    hist = ["提醒我交电费", "已设提醒：交电费"]
    out, changed, mp = cf.resolve_references(cur, hist)
    assert changed and "交电费" in out


def test_no_candidates_passthrough():
    cur = "把它关了"
    hist = ["今天天气不错"]
    out, changed, mp = cf.resolve_references(cur, hist)
    assert not changed and out == cur and mp == {}


def test_empty_history_passthrough():
    out, changed, mp = cf.resolve_references("那首歌再放一遍", [])
    assert not changed and out == "那首歌再放一遍"


def test_no_fabrication_when_missing_category():
    # 当前句是地点代词，但历史里只有歌曲 → 不应拿歌曲替地点
    cur = "那个地方再看一下"
    hist = ["放首周杰伦的歌"]
    out, changed, mp = cf.resolve_references(cur, hist)
    assert not changed and out == cur


def test_exception_safe():
    # 故意传非字符串触发内部异常路径，确认降级透传不抛
    out, changed, mp = cf.resolve_references(None, ["x"])  # type: ignore
    assert out is None and changed is False and mp == {}


def test_resolve_for_session_passthrough_when_no_history(monkeypatch):
    """拉不到历史 → 原句透传，不崩。"""
    import agent.history as hist_mod

    class _Stub:
        def __call__(self, sid="default"):
            return []

    monkeypatch.setattr(hist_mod, "_history_snapshot", _Stub())
    out, changed, mp = cf.resolve_for_session("那首歌再放一遍", "default")
    assert out == "那首歌再放一遍" and changed is False


def test_resolve_for_session_draws_from_assistant(monkeypatch):
    """会话封装从历史 assistant 回复里捞实体并消解。"""
    import agent.history as hist_mod

    class _Stub:
        def __call__(self, sid="default"):
            return [
                {"role": "user", "content": "放首周杰伦的歌"},
                {"role": "assistant", "content": "好的，正在播放晴天"},
                {"role": "user", "content": "那首歌再放一遍"},
            ]

    monkeypatch.setattr(hist_mod, "_history_snapshot", _Stub())
    out, changed, mp = cf.resolve_for_session("那首歌再放一遍", "default")
    assert changed and "晴天" in out
