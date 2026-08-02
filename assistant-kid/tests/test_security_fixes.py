"""
Charlie - 安全修复与bug修复测试
测试: eval安全替代、MCP进程清理、brain_status无死代码、请求体大小限制
"""
import pytest
import os, sys, ast

# 确保能导入项目模块
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)


class TestSafeMathEval:
    """测试安全数学求值(替代eval)"""

    def setup_method(self):
        """导入_safe_math_eval函数"""
        # baize_skills_mcp.py导入需要mock环境
        os.environ.setdefault("TAVILY_API_KEY", "test")
        os.environ.setdefault("ALIYUN_API_KEY", "test")
        from baize_skills_mcp import _safe_math_eval
        self._eval = _safe_math_eval

    def test_simple_addition(self):
        """简单加法"""
        assert self._eval("1+2") == 3

    def test_complex_expression(self):
        """复杂表达式"""
        assert self._eval("2*3+4") == 10

    def test_parentheses(self):
        """括号优先级"""
        assert self._eval("(2+3)*4") == 20

    def test_power(self):
        """幂运算"""
        assert self._eval("2**10") == 1024

    def test_division(self):
        """除法"""
        assert self._eval("10/4") == 2.5

    def test_floor_division(self):
        """整除"""
        assert self._eval("10//3") == 3

    def test_modulo(self):
        """取模"""
        assert self._eval("10%3") == 1

    def test_unary_minus(self):
        """一元负号"""
        assert self._eval("-5") == -5

    def test_float(self):
        """浮点数"""
        assert self._eval("3.14*2") == 6.28

    def test_reject_string_injection(self):
        """拒绝字符串注入攻击"""
        # eval("__import__('os').system('ls')") 应返回None
        result = self._eval("__import__('os').system('ls')")
        assert result is None

    def test_reject_attribute_access(self):
        """拒绝属性访问"""
        result = self._eval("1 .__class__")
        assert result is None

    def test_reject_name_lookup(self):
        """拒绝变量名查找"""
        result = self._eval("open('/etc/passwd')")
        assert result is None

    def test_reject_lambda(self):
        """拒绝lambda表达式"""
        result = self._eval("(lambda: 42)()")
        assert result is None

    def test_reject_comprehension(self):
        """拒绝列表推导式"""
        result = self._eval("[x for x in range(10)]")
        assert result is None

    def test_none_for_invalid_syntax(self):
        """无效语法返回None"""
        assert self._eval("hello world") is None
        assert self._eval("1++2+") is None
        assert self._eval("") is None


class TestBrainStatusNoDeadCode:
    """验证brain_status没有死代码"""

    def test_brain_status_returns_dict(self):
        """brain_status返回正确结构的dict"""
        from voice_agent import brain_status
        result = brain_status()
        assert isinstance(result, dict)
        assert "ready" in result
        assert "consecutive_failures" in result
        assert "max_failures_before_rebuild" in result

    def test_brain_status_has_no_unreachable_code(self):
        """验证brain_status函数体内return之后没有死代码"""
        import voice_agent
        source = open(voice_agent.__file__).read()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "brain_status":
                # body是函数体语句列表, 最后一个应该是return
                body = node.body
                assert isinstance(body[-1], ast.Return), \
                    f"brain_status最后一句应该是return, 实际是{type(body[-1]).__name__}"
                # body应该只有3条: docstring, import, return
                assert len(body) == 3, \
                    f"brain_status应有3条语句, 实际{len(body)}条(可能有死代码)"


class TestMcpProcessCleanup:
    """测试MCP进程清理函数"""

    def test_cleanup_none_brain(self):
        """传入None不崩溃"""
        from voice_agent import _cleanup_brain_processes
        _cleanup_brain_processes(None)  # 不应抛异常

    def test_cleanup_simple_object(self):
        """传入普通对象不崩溃"""
        from voice_agent import _cleanup_brain_processes
        _cleanup_brain_processes(object())  # 不应抛异常

    def test_restart_brain_returns_message(self):
        """restart_brain返回正确消息"""
        from voice_agent import restart_brain
        msg = restart_brain()
        assert isinstance(msg, str)
        assert "重启" in msg

    def test_restart_brain_sets_none(self):
        """restart_brain后所有缓存大脑被清空"""
        import voice_agent
        voice_agent._brains["test"] = "fake_brain"
        voice_agent.restart_brain()
        assert voice_agent._brains == {}

    def test_record_failure_cleans_up_on_rebuild(self):
        """连续失败达阈值时清理旧大脑"""
        import voice_agent
        # 设置一个假大脑
        voice_agent._brains["test"] = MagicMock_brain()
        voice_agent._brain_failures = voice_agent._MAX_BRAIN_FAILURES - 1
        # 再失败一次应该触发重建
        voice_agent._record_brain_failure("test error")
        assert voice_agent._brains == {}
        assert voice_agent._brain_failures == 0


class MagicMock_brain:
    """简单的mock大脑对象"""
    _function_list = [{"mcpServers": {"test": {}}}]
    _mcp_clients = {}


import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client():
    """创建测试客户端(不触发lifespan, 避免后台线程冲突)"""
    os.environ["SKIP_BACKGROUND"] = "1"
    os.environ.setdefault("GLM_KEY", "test")
    os.environ.setdefault("TTS_KEY", "test")
    os.environ.setdefault("ASR_KEY", "test")
    os.environ.setdefault("AMAP_KEY", "test")
    import voice_server
    c = TestClient(voice_server.app)
    yield c


class TestRequestSizeLimit:
    """测试请求体大小限制中间件"""

    def test_normal_request_passes(self, client):
        """正常大小的请求通过"""
        response = client.post("/api/chat", json={"message": "hello"})
        # 可能是200或降级响应, 不应是413
        assert response.status_code != 413

    def test_oversized_request_rejected(self, client):
        """超大请求体被拒绝(413)"""
        # 构造一个>15MB的请求(通过设置超大Content-Length)
        response = client.post(
            "/api/chat",
            content=b'{"message": "test"}',
            headers={"Content-Type": "application/json", "Content-Length": str(20 * 1024 * 1024)},
        )
        assert response.status_code == 413
        data = response.json()
        assert "过大" in data.get("error", "")


class TestAuthProxySpoofing:
    """认证不能信任客户端伪造的代理头。"""

    @staticmethod
    def _request(headers=None, client=("203.0.113.10", 51234)):
        from starlette.requests import Request

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/api/status",
            "headers": [(name.lower().encode(), value.encode()) for name, value in (headers or [])],
            "client": client,
        }
        return Request(scope)

    def test_spoofed_forwarded_for_does_not_bypass_auth(self, monkeypatch):
        from app import auth

        monkeypatch.setattr(auth, "AUTH_TOKEN", "secret-token")

        request = self._request([
            ("X-Forwarded-For", "127.0.0.1"),
            ("X-Real-IP", "127.0.0.1"),
        ])

        assert auth._check_auth(request) is False

    def test_loopback_peer_without_proxy_headers_is_local(self, monkeypatch):
        from app import auth

        monkeypatch.setattr(auth, "AUTH_TOKEN", "secret-token")
        request = self._request(client=("127.0.0.1", 51234))

        assert auth._check_auth(request) is True

    def test_valid_bearer_token_passes_for_external_peer(self, monkeypatch):
        from app import auth

        monkeypatch.setattr(auth, "AUTH_TOKEN", "secret-token")
        request = self._request([("Authorization", "Bearer secret-token")])

        assert auth._check_auth(request) is True


class TestSanitizeTextCompiledPatterns:
    """输入清洗应复用模块级预编译正则，避免每次请求重复编译。"""

    def test_sanitize_reuses_compiled_patterns(self, monkeypatch):
        import re
        from app import auth

        compiled_patterns = [
            value
            for name, value in vars(auth).items()
            if name.endswith("_RE") and isinstance(value, re.Pattern)
        ]
        assert len(compiled_patterns) >= 4

        calls = []

        def tracked_sub(pattern, repl, string, *args, **kwargs):
            calls.append(pattern)
            return original_sub(pattern, repl, string, *args, **kwargs)

        original_sub = re.sub
        monkeypatch.setattr(re, "sub", tracked_sub)

        result = auth._sanitize_text(
            '<b></b>onclick = javascript:\x00正常文字'
        )

        assert result == '正常文字'
        assert calls == []


class TestEnvExampleCompleteness:
    """验证.env.example包含所有使用的环境变量"""

    def test_all_env_vars_documented(self):
        """所有代码中使用的env变量都在.env.example中有文档"""
        env_example = open(
            os.path.join(PROJECT_DIR, ".env.example")
        ).read()

        # 收集代码中所有os.getenv调用的变量名
        import re
        env_vars_used = set()
        for pyfile in ["voice_agent.py", "voice_server.py", "baize_skills_mcp.py", "mcp_server.py"]:
            path = os.path.join(PROJECT_DIR, pyfile)
            if os.path.exists(path):
                content = open(path).read()
                # 匹配 os.getenv("VAR_NAME") 和 os.environ.get("VAR_NAME")
                for m in re.finditer(r'os\.(?:getenv|environ\.get)\(["\'](\w+)["\']', content):
                    env_vars_used.add(m.group(1))

        # 检查每个变量在.env.example中出现(注释也算)
        undocumented = []
        for var in env_vars_used:
            if var not in env_example:
                undocumented.append(var)

        # 允许别名变量(如AMAP_MAPS_API_KEY是AMAP_KEY的别名)不在example中
        # 但核心变量必须文档化
        aliases = {"AMAP_MAPS_API_KEY"}  # AMAP_KEY的别名, 在mcp_server.py中使用
        truly_undocumented = [v for v in undocumented if v not in aliases]
        assert truly_undocumented == [], f"未文档化的环境变量: {truly_undocumented}"
