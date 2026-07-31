"""
助手小子 - pytest配置
设置sys.path + 公共fixtures
"""
import sys, os
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
