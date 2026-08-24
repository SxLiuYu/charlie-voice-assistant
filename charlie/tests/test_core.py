"""Charlie core module tests.

Tests: magic-memory (bigram similarity, extraction, correction, dedup),
magic-decisions (evaluation, cooldown, feedback, pending confirmation),
magic-scenes (matching, execution, new step types),
magic-evolution (pattern extraction, optimization).
"""
import os, sys, json, time, importlib.util, tempfile, shutil

# Test setup: load modules from parent dir
PARENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PARENT_DIR)
os.chdir(PARENT_DIR)

# Temp data dir for tests
TMP_DIR = tempfile.mkdtemp(prefix="charlie_test_")
os.environ["ASSISTANT_KID_DATA_DIR"] = TMP_DIR
os.environ.pop("CHARLIE_USER_ID", None)


def _load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(PARENT_DIR, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ===== magic-memory tests =====

class TestMagicMemory:
    def setup_method(self):
        self.mem = _load_module("magic_memory_test", "magic-memory.py")

    def teardown_method(self):
        for f in os.listdir(TMP_DIR):
            os.remove(os.path.join(TMP_DIR, f))

    def test_bigram_extraction(self):
        v = self.mem._text_to_bigrams("项目截止")
        assert "项目" in v
        assert "目截" in v
        assert "截止" in v
        assert len(v) == 3

    def test_cosine_similarity_identical(self):
        v = self.mem._text_to_bigrams("测试文本")
        sim = self.mem._cosine_similarity(v, v)
        assert abs(sim - 1.0) < 0.01

    def test_cosine_similarity_different(self):
        v1 = self.mem._text_to_bigrams("项目截止")
        v2 = self.mem._text_to_bigrams("今天天气")
        assert self.mem._cosine_similarity(v1, v2) == 0.0

    def test_cosine_similarity_partial(self):
        v1 = self.mem._text_to_bigrams("项目截止日期")
        v2 = self.mem._text_to_bigrams("项目进度")
        sim = self.mem._cosine_similarity(v1, v2)
        assert sim > 0  # "项目" bigram overlaps

    def test_extract_events_deadline(self):
        events = self.mem._extract_events("项目周五截止，我还没做完", "知道了")
        assert len(events) == 1
        assert "deadline" in events[0]["tags"]
        assert "bigrams" in events[0]

    def test_extract_events_skip_short(self):
        events = self.mem._extract_events("嗯", "嗯")
        assert len(events) == 0

    def test_extract_events_skip_greeting(self):
        events = self.mem._extract_events("你好", "你好")
        assert len(events) == 0

    def test_remember_and_retrieve(self):
        self.mem.remember_conversation("项目周五截止，我还没做完", "知道了，注意进度")
        self.mem.remember_conversation("我打算下周去北京出差", "好的，注意行程")
        results = self.mem.get_relevant_memories("项目怎么样了", limit=2)
        assert len(results) > 0
        # The deadline memory should rank higher than the travel memory
        assert "截止" in results[0].get("summary", "") or "项目" in results[0].get("summary", "")

    def test_memory_correction(self):
        self.mem.remember_conversation("项目周五截止", "注意进度")
        found = self.mem.correct_memory("项目周五截止", "项目下周三截止")
        assert found
        results = self.mem.get_relevant_memories("项目", limit=1)
        assert "下周三" in results[0].get("summary", "")

    def test_memory_dedup(self):
        # remember_conversation 应在写入时做 bigram 去重，重复文本不应二次入库
        n1 = self.mem.remember_conversation("我在做项目", "加油")
        n2 = self.mem.remember_conversation("我在做项目", "加油")
        assert n1 >= 1
        assert n2 == 0  # 重复内容被去重
        removed = self.mem.dedup_memories()
        assert removed == 0  # 无残留重复


# ===== magic-decisions tests =====

class TestMagicDecisions:
    def setup_method(self):
        self.dec = _load_module("magic_decisions_test", "magic-decisions.py")

    def teardown_method(self):
        for f in os.listdir(TMP_DIR):
            os.remove(os.path.join(TMP_DIR, f))

    def test_rules_count(self):
        assert len(self.dec._DECISION_RULES) == 12  # expanded rules: morning_wakeup/weather_alert/meeting_reminder/leaving_reminder/lunch_reminder/evening_wind_down/sedentary_break/evening_wrapup/arrive_home/deadline_reminder/casual_checkin

    def test_evaluate_home_awake_morning(self):
        # Should not trigger (cooldown or time mismatch)
        results = self.dec.evaluate({"state": "home_awake", "confidence": 0.7})
        # Morning rules might fire, depends on current time
        assert isinstance(results, list)

    def test_evaluate_away_state(self):
        # away state during daytime
        results = self.dec.evaluate({"state": "away"})
        assert isinstance(results, list)

    def test_cooldown(self):
        history = {}
        assert self.dec._check_cooldown("test_rule", history)  # Never triggered
        history["test_rule"] = {"last_trigger": time.time()}
        assert not self.dec._check_cooldown("test_rule", history)  # Just triggered

    def test_feedback_recording(self):
        self.dec.record_feedback("lunch_reminder", True)
        self.dec.record_feedback("lunch_reminder", True)
        self.dec.record_feedback("lunch_reminder", False)
        score = self.dec._get_feedback_score("lunch_reminder")
        assert abs(score - 0.667) < 0.1  # 2/3 positive

    def test_feedback_skip_rule(self):
        for _ in range(5):
            self.dec.record_feedback("evening_wind_down", False)
        assert self.dec._should_skip_rule("evening_wind_down")

    def test_pending_confirmation(self):
        self.dec.set_pending_confirmation("lunch_reminder", "到午饭时间了")
        pending = self.dec.get_pending_confirmation()
        assert pending is not None
        assert pending["rule_id"] == "lunch_reminder"
        self.dec.clear_pending_confirmation()
        assert self.dec.get_pending_confirmation() is None

    def test_meeting_reminder_rule_exists(self):
        ids = [r["id"] for r in self.dec._DECISION_RULES]
        assert set(["morning_wakeup","weather_alert","meeting_reminder","leaving_reminder","lunch_reminder","evening_wind_down","sedentary_break","evening_wrapup","arrive_home","deadline_reminder","casual_checkin"]).issubset(set(ids))


# ===== magic-scenes tests =====

class TestMagicScenes:
    def setup_method(self):
        self.sc = _load_module("magic_scenes_test", "magic-scenes.py")

    def teardown_method(self):
        for f in os.listdir(TMP_DIR):
            os.remove(os.path.join(TMP_DIR, f))

    def test_builtin_protocols_count(self):
        assert len(self.sc._BUILTIN_PROTOCOLS) == 4

    def test_match_protocol_goodnight(self):
        assert self.sc.match_protocol("晚安") == "goodnight"
        assert self.sc.match_protocol("睡觉了") == "goodnight"

    def test_match_protocol_none(self):
        assert self.sc.match_protocol("今天天气怎么样") is None

    def test_execute_goodnight(self):
        result = self.sc.execute_protocol("goodnight")
        assert "晚安" in result

    def test_ac_sleep_hot_night_keeps_ac(self):
        """热天夜间(>=24℃)不关空调，保留助眠。"""
        def hot_forecast():
            return {"dayweather": "晴", "nightweather": "晴", "daytemp": "34", "nighttemp": "28"}
        calls = []
        orig = self.sc._ac_control
        self.sc._get_weather_forecast = hot_forecast
        self.sc._ac_control = lambda action: calls.append(action) or "空调已off"
        try:
            msg = self.sc._ac_sleep()
        finally:
            self.sc._ac_control = orig
        assert calls == []  # 没有关空调
        assert "保持开启" in msg
        assert "28" in msg

    def test_ac_sleep_cool_night_turns_off(self):
        """凉爽夜间(<24℃)不再自动关空调，只播报建议。"""
        def cool_forecast():
            return {"dayweather": "多云", "nightweather": "多云", "daytemp": "22", "nighttemp": "18"}
        calls = []
        self.sc._get_weather_forecast = cool_forecast
        self.sc._ac_control = lambda action: calls.append(action) or "空调已off"
        try:
            msg = self.sc._ac_sleep()
        finally:
            self.sc._ac_control = lambda action: "空调已off"
        assert calls == []  # 不再自动关空调
        assert "18" in msg
        assert "关空调" in msg  # 提示用户手动关

    def test_ac_sleep_no_weather_keeps_ac(self):
        """天气数据不可用时保守保留空调，不误关。"""
        self.sc._get_weather_forecast = lambda: {}
        calls = []
        self.sc._ac_control = lambda action: calls.append(action) or "空调已off"
        try:
            msg = self.sc._ac_sleep()
        finally:
            self.sc._ac_control = lambda action: "空调已off"
        assert calls == []
        assert "保持开启" in msg

    def test_wait_step(self):
        result = self.sc._wait_step({"seconds": 1})
        assert "等待" in result

    def test_if_condition_weather(self):
        result = self.sc._if_condition_step({
            "condition": "hour_after=0",
            "then": [{"action": "tts", "params": {"template": "条件满足"}}],
            "else": [{"action": "tts", "params": {"template": "条件不满足"}}],
        })
        assert "条件满足" in result

    def test_keyword_parsing(self):
        steps = self.sc._parse_steps_keyword("打开空调, 等待5秒, 播放音乐")
        assert len(steps) == 3
        assert steps[0]["action"] == "ac_control"
        assert steps[1]["action"] == "wait"
        assert steps[2]["action"] == "tts"

    def test_learn_protocol(self):
        result = self.sc.learn_protocol("测试场景", "测试场景,开始测试", "打开空调, 播放提示")
        assert "已学习" in result

    def test_list_protocols(self):
        result = self.sc.list_protocols()
        assert "内置" in result
        assert "晚安" in result


# ===== magic-evolution tests =====

class TestMagicEvolution:
    def setup_method(self):
        self.evo = _load_module("magic_evolution_test", "magic-evolution.py")

    def teardown_method(self):
        for f in os.listdir(TMP_DIR):
            os.remove(os.path.join(TMP_DIR, f))

    def test_load_empty(self):
        data = self.evo._load_evolution_data()
        assert "learned_preferences" in data
        assert "usage_patterns" in data

    def test_pattern_extraction(self):
        patterns = self.evo._extract_patterns(["天气怎么样", "播放音乐", "几点了", "天气怎么样"])
        assert patterns["total_messages"] == 4
        assert "天气" in patterns["topic_distribution"]
        assert "音乐" in patterns["topic_distribution"]

    def test_evolution_status(self):
        status = self.evo._load_evolution_data()
        assert "adaptation_state" in status


# Cleanup
def teardown_module():
    shutil.rmtree(TMP_DIR, ignore_errors=True)
