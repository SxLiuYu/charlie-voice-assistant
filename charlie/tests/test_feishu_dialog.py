"""飞书双向对话测试 — check_feedback 确认/拒绝/无匹配 + 确认窗口"""
import os, sys, json, time, importlib.util, tempfile
from unittest.mock import patch, MagicMock

PARENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PARENT_DIR)

_TMP_DIR = tempfile.mkdtemp(prefix="charlie_feishu_test_")
_ORIG_DATA_DIR = os.environ.get("ASSISTANT_KID_DATA_DIR", "")


def _load_decisions():
    """加载 magic-decisions.py（文件名带横线，不能直接 import）。"""
    spec = importlib.util.spec_from_file_location(
        "magic_decisions_feishu_test", os.path.join(PARENT_DIR, "magic-decisions.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestCheckFeedback:
    """check_feedback 是飞书双向对话的核心：将用户回复匹配到待确认决策。"""

    def setup_method(self):
        os.environ["ASSISTANT_KID_DATA_DIR"] = _TMP_DIR
        self.dec = _load_decisions()
        for f in os.listdir(_TMP_DIR):
            os.remove(os.path.join(_TMP_DIR, f))

    def teardown_method(self):
        os.environ["ASSISTANT_KID_DATA_DIR"] = _ORIG_DATA_DIR

    def _set_pending(self, rule_id="late_night_sleep", text="已经很晚了，要帮你执行晚安场景吗？"):
        """创建一个 pending_confirmation.json 用于测试。"""
        self.dec.set_pending_confirmation(rule_id, text)

    def test_no_pending_returns_none(self):
        """无待确认决策时返回 None，交给正常 brain 流程。"""
        assert self.dec.check_feedback("好的") is None
        assert self.dec.check_feedback("随便说点什么") is None

    def test_positive_confirmation_executes_protocol(self):
        """用户回复确认词 → 执行协议 + 清除 pending + 记录正面反馈。"""
        self._set_pending()

        mock_scene = MagicMock()
        mock_scene.execute_protocol.return_value = "晚安场景已执行：空调睡眠模式+电视关闭+设好闹钟"
        with patch("app.load_magic_module", return_value=mock_scene):
            result = self.dec.check_feedback("好的，执行吧")

        assert result is not None
        assert "晚安场景已执行" in result

        # pending 应被清除
        assert self.dec.get_pending_confirmation() is None

        # 反馈应被记录为正面
        feedback = self.dec._load_feedback()
        assert feedback.get("late_night_sleep", {}).get("positive", 0) == 1

    def test_negative_rejection_records_feedback(self):
        """用户回复拒绝词 → 不执行协议 + 清除 pending + 记录负面反馈。"""
        self._set_pending()

        mock_scene = MagicMock()
        with patch("app.load_magic_module", return_value=mock_scene):
            result = self.dec.check_feedback("不用了")

        assert result is not None
        assert "取消" in result
        mock_scene.execute_protocol.assert_not_called()

        # pending 应被清除
        assert self.dec.get_pending_confirmation() is None

        # 反馈应被记录为负面
        feedback = self.dec._load_feedback()
        assert feedback.get("late_night_sleep", {}).get("negative", 0) == 1

    def test_unmatched_text_returns_none(self):
        """待确认存在但回复不匹配确认/拒绝 → 返回 None 交给正常对话。"""
        self._set_pending()

        # 不匹配任何确认/拒绝词的文本
        assert self.dec.check_feedback("今天天气怎么样") is None

        # pending 不应被清除（等待用户后续回复）
        pending = self.dec.get_pending_confirmation()
        assert pending is not None
        assert pending["rule_id"] == "late_night_sleep"

    def test_long_greeting_not_mistaken_as_confirmation(self):
        """>7字的长句（如"好的，今天天气怎么样"=10字）不应被误判为确认，直接交 brain。"""
        self._set_pending()

        # 10字 > 7字阈值 → 直接返回 None，且不包含动作词 → 不触发确认
        result = self.dec.check_feedback("好的，今天天气怎么样")
        assert result is None, f"10字长句不应匹配确认，实际返回: {result}"

        # pending 不应被清除
        pending = self.dec.get_pending_confirmation()
        assert pending is not None
        assert pending["rule_id"] == "late_night_sleep"

    def test_short_confirmation_still_works(self):
        """4字以内的真确认词仍应正常匹配。"""
        self._set_pending()

        mock_scene = MagicMock()
        mock_scene.execute_protocol.return_value = "已执行"
        with patch("app.load_magic_module", return_value=mock_scene):
            result = self.dec.check_feedback("好的")

        assert result is not None
        assert "已执行" in result
        assert self.dec.get_pending_confirmation() is None

    def test_cross_user_pending_isolation(self):
        """用户 A 的 pending 不应被用户 B 的消息触发。"""
        # 为用户 A（ou_aaa）设置 pending
        self.dec.set_pending_confirmation("late_night_sleep", "已经很晚了？", user_id="ou_aaa")

        # 用户 B（ou_bbb）发"好的" → 不应匹配 ou_aaa 的 pending
        result = self.dec.check_feedback("好的", user_id="ou_bbb")
        assert result is None, f"用户B不应触发用户A的pending，实际返回: {result}"

        # 用户 A 的 pending 依然存在
        pending = self.dec.get_pending_confirmation(user_id="ou_aaa")
        assert pending is not None
        assert pending["rule_id"] == "late_night_sleep"

    def test_expired_pending_treated_as_none(self):
        """过期的 pending（超过 _CONFIRMATION_WINDOW）应被自动清除，返回 None。"""
        self._set_pending()

        # 把 pending 文件的 timestamp 改成 10 分钟前（已过期）
        pending_file = os.path.join(_TMP_DIR, "pending_confirmation.json")
        with open(pending_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        data["timestamp"] = time.time() - 600
        with open(pending_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)

        assert self.dec.check_feedback("好的") is None
        assert self.dec.get_pending_confirmation() is None


class TestConfirmationWindow:
    """确认窗口应为 300 秒（5 分钟），给用户足够时间看到飞书消息并回复。"""

    def test_window_is_300s(self):
        dec = _load_decisions()
        assert dec._CONFIRMATION_WINDOW == 300


class TestExecuteDecisionConfirmed:
    """execute_decision 的 confirmed=True 参数应跳过 safe 检查。"""

    def setup_method(self):
        os.environ["ASSISTANT_KID_DATA_DIR"] = _TMP_DIR
        self.dec = _load_decisions()
        for f in os.listdir(_TMP_DIR):
            os.remove(os.path.join(_TMP_DIR, f))

    def teardown_method(self):
        os.environ["ASSISTANT_KID_DATA_DIR"] = _ORIG_DATA_DIR

    def test_confirmed_bypasses_safe_check(self):
        """confirmed=True 时，protocol 类型不再返回"需要确认"而是直接执行。"""
        rule = {
            "id": "test_rule",
            "action": {"type": "protocol", "name": "test_scene"},
            "condition": {"states": ["home"]},
        }

        mock_executor = MagicMock(return_value="场景已执行")

        # 未确认 → 返回"需要确认"
        result = self.dec.execute_decision(rule, mock_executor, confirmed=False)
        assert "需要确认" in result
        mock_executor.assert_not_called()

        # 已确认 → 直接执行
        result = self.dec.execute_decision(rule, mock_executor, confirmed=True)
        assert result == "场景已执行"
        mock_executor.assert_called_once_with("test_scene")
