"""用户画像门面（agent/user_profile.py）单测。

覆盖：
  - get_user_profile 聚合视图：结构完整、空库降级不崩、uid 正确；
  - reply_style 读写往返 + 归一化（非法值回退默认）；
  - record_explicit 显式通道：回复风格/位置/喜好/泛指记忆 各自路由；
  - 敏感过滤：身份证/银行卡/手机号 命中即拦；
  - format_profile_for_prompt：稳定字段成段、空画像返回空；
  - uid 分片隔离：A 的画像不泄漏给 B。

全程默认开关下运行（get/set 不依赖开关；开关只门控 brain 注入/自动沉淀）。
"""
import json
import os
import sys

import pytest

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

import agent.user_profile as up  # noqa: E402


@pytest.fixture
def iso(monkeypatch, tmp_path):
    """每个用例隔离 DATA_DIR，并清 memory_store 的 sqlite/热缓存。"""
    monkeypatch.setattr(up, "DATA_DIR", str(tmp_path))
    yield tmp_path


def test_get_user_profile_shape_and_defaults(iso, monkeypatch):
    monkeypatch.delenv("CHARLIE_USER_ID", raising=False)
    prof = up.get_user_profile("default")
    assert set(prof.keys()) == set(up.PROFILE_KEYS)
    assert prof["uid"]  # 有 uid
    assert prof["reply_style"]["source"] == "default"  # 未声明→默认
    assert prof["preferences"] == {} or isinstance(prof["preferences"], dict)
    assert isinstance(prof["interests"], list)
    assert isinstance(prof["conversation_background"], dict)
    assert "sources" in prof["meta"]


def test_reply_style_roundtrip_and_normalize(iso):
    up.set_reply_style("default", {"tone": "playful", "length": "short", "humor": True})
    rs = up.get_reply_style("default")
    assert rs["tone"] == "playful"
    assert rs["length"] == "short"
    assert rs["humor"] is True
    assert rs["source"] == "explicit"
    assert rs["declared_at"]
    # 非法枚举回退默认
    up.set_reply_style("default", {"tone": "LOUD", "length": "huge"})
    rs2 = up.get_reply_style("default")
    assert rs2["tone"] == up._DEFAULT_STYLE["tone"]
    assert rs2["length"] == up._DEFAULT_STYLE["length"]


def test_record_explicit_style(iso):
    done = up.record_explicit("default", "以后说话简短点")
    assert any(d.startswith("reply_style") for d in done)
    assert up.get_reply_style("default")["length"] == "short"


def test_record_explicit_location(iso, monkeypatch):
    # 隔离 preferences 数据目录（preferences 用同一 ASSISTANT_KID_DATA_DIR=DATA_DIR）
    monkeypatch.setenv("ASSISTANT_KID_DATA_DIR", str(iso))
    import importlib
    import agent.preferences as pref
    importlib.reload(pref)  # 让 preferences 读到隔离目录
    monkeypatch.setattr(pref, "DATA_DIR", str(iso))
    done = up.record_explicit("default", "我住在上海浦东")
    assert any(d.startswith("location") for d in done)
    assert pref.get_preference("location", "default") == "上海浦东"


def test_record_explicit_like(iso, monkeypatch):
    monkeypatch.setenv("ASSISTANT_KID_DATA_DIR", str(iso))
    import agent.memory_store as ms
    importlib_reload = __import__("importlib").reload
    importlib_reload(ms)
    done = up.record_explicit("default", "我喜欢喝美式咖啡")
    assert any(d.startswith("interest") for d in done)
    row = ms.get_fact("喜欢:喝美式咖啡")
    assert row and "美式咖啡" in row["value"]


def test_record_explicit_sensitive_blocked(iso, monkeypatch):
    monkeypatch.setenv("ASSISTANT_KID_DATA_DIR", str(iso))
    import agent.memory_store as ms
    __import__("importlib").reload(ms)
    # 喜好里带身份证号 → 不落库
    done = up.record_explicit("default", "我喜欢110101199001011234")
    assert not any(d.startswith("interest") for d in done)


def test_is_sensitive():
    assert up.is_sensitive("我身份证110101199001011234") == "id"
    assert up.is_sensitive("银行卡号 6222021234567890") in ("bank", "cardno")
    assert up.is_sensitive("我手机号13800138000") == "phone"
    assert up.is_sensitive("今天天气不错，去公园走走") is None


def test_format_profile_for_prompt(iso):
    prof = {
        "location": "北京",
        "interests": ["美式咖啡", "猫"],
        "preferences": {"咖啡": "美式"},
        "reply_style": {"tone": "playful", "length": "short", "humor": True,
                        "source": "explicit"},
    }
    frag = up.format_profile_for_prompt(prof)
    assert frag.startswith("【用户画像】")
    assert "北京" in frag and "美式咖啡" in frag
    assert "回复风格" in frag
    # 空画像 → 空串
    assert up.format_profile_for_prompt({}) == ""
    # 只有默认风格、无其它字段 → 不注入风格行，可能整体为空
    assert up.format_profile_for_prompt(
        {"reply_style": {"source": "default"}}) == ""


def test_uid_isolation(iso, monkeypatch):
    # A 声明风格
    up.set_reply_style("sessA", {"tone": "formal"})
    # 让 B 会话解析成另一个 uid，读不到 A 的
    monkeypatch.setattr(up, "_resolve_uid", lambda sid=None: "userB")
    assert up.get_reply_style("sessB")["source"] == "default"
    # 文件确实分片
    assert up._profile_file("userA") != up._profile_file("userB")


def test_apply_extracted_gated_off(monkeypatch):
    monkeypatch.delenv("USER_PROFILE_AUTOSTORE", raising=False)
    r = up.apply_extracted("default", "你好", "你好呀")
    assert r.get("skipped") is True


def test_apply_extracted_sensitive(monkeypatch):
    monkeypatch.setenv("USER_PROFILE_AUTOSTORE", "1")
    r = up.apply_extracted("default", "我银行卡号6222021234567890", "已记录")
    assert r.get("skipped") is True and r.get("sensitive") in ("bank", "cardno")


# ===== brain() 里的画像捕获钩子（voice_agent._capture_profile）=====
def _wait_capture():
    import time as _t
    _t.sleep(0.4)  # 守护线程异步落库，等一等


def test_capture_profile_gated_off_no_write(monkeypatch, iso):
    """默认两个开关都关 → 钩子静默返回，reply_style 不落库（零行为变更）。"""
    monkeypatch.delenv("USER_PROFILE_ENABLED", raising=False)
    monkeypatch.delenv("USER_PROFILE_AUTOSTORE", raising=False)
    monkeypatch.setenv("ASSISTANT_KID_DATA_DIR", str(iso))
    import voice_agent
    voice_agent._capture_profile("以后说话简短点", "default", "好的")
    _wait_capture()
    assert up.get_reply_style("default")["source"] == "default"


def test_capture_profile_explicit_write(monkeypatch, iso):
    """USER_PROFILE_ENABLED=1 → 显式指令落库（reply_style）。"""
    monkeypatch.setenv("USER_PROFILE_ENABLED", "1")
    monkeypatch.delenv("USER_PROFILE_AUTOSTORE", raising=False)
    monkeypatch.setenv("ASSISTANT_KID_DATA_DIR", str(iso))
    import voice_agent
    voice_agent._capture_profile("以后说话简短点", "default", "好的，我尽量简短")
    _wait_capture()
    assert up.get_reply_style("default")["length"] == "short"
