"""
Charlie - pytest配置
设置sys.path + 公共fixtures
"""
import sys, os, tempfile
# 确保能导入项目模块
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

# 设置环境变量(测试用, 避免加载真实密钥)
os.environ.setdefault("FINNA_BASE", "https://test.example.com/v1")
os.environ.setdefault("GLM_KEY", "test-glm-key")
os.environ.setdefault("TTS_KEY", "test-tts-key")
os.environ.setdefault("ASR_KEY", "test-asr-key")
os.environ.setdefault("TTS_VOICE", "Cherry")
os.environ.setdefault("SKIP_BACKGROUND", "1")  # 跳过后台调度器

# 测试必须使用独立数据/日志目录，避免覆盖真实对话、提醒、偏好和运行日志。
_TEST_DATA_DIR = tempfile.mkdtemp(prefix="charlie-test-data-")
_TEST_LOG_DIR = tempfile.mkdtemp(prefix="charlie-test-logs-")
os.makedirs(_TEST_DATA_DIR, exist_ok=True)
os.makedirs(_TEST_LOG_DIR, exist_ok=True)
os.environ["ASSISTANT_KID_DATA_DIR"] = _TEST_DATA_DIR
os.environ["ASSISTANT_KID_LOG_DIR"] = _TEST_LOG_DIR


def pytest_sessionfinish(session, exitstatus):
    """测试结束后清理临时数据和日志目录，避免pytest垃圾堆积。"""
    import shutil
    for path in (_TEST_DATA_DIR, _TEST_LOG_DIR):
        shutil.rmtree(path, ignore_errors=True)
