"""LLM 配置解析 — ARK vs Ollama adapter（深 module）

从 voice_agent._build_brain 抽出。负责：
- 探测 ARK_KEY 是否配置
- 探测 Ollama 服务是否在线
- 返回 llm_cfg dict（ARK 或 Ollama）
"""
import os
import logging

log = logging.getLogger("magic")

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3.5:2b")
OLLAMA_OPENAI_BASE = OLLAMA_HOST + "/v1"

ARK_BASE = os.getenv("ARK_BASE", "https://ark.cn-beijing.volces.com/api/plan/v3")
ARK_KEY = os.getenv("ARK_KEY", "")
ARK_MODEL = os.getenv("ARK_MODEL", "ark-code-latest")

# 智谱 GLM 免费大脑（glm-4.7-flash 永久免费，OpenAI 兼容，无需 ARK）
GLM_BASE = os.getenv("GLM_BASE", "https://open.bigmodel.cn/api/paas/v4")
GLM_KEY = os.getenv("GLM_KEY", "")
GLM_MODEL = os.getenv("GLM_MODEL", "glm-4.7-flash")

# 429 限流 fallback 链：按顺序尝试，免费模型轮换。
# GLM_MODEL 始终作为首选，其余按 GLM_MODELS 顺序补位。
GLM_MODELS = [m.strip() for m in os.getenv("GLM_MODELS",
    "glm-4.7-flash,glm-4-flash,glm-4.5-flash").split(",") if m.strip()]
if GLM_MODEL and GLM_MODEL not in GLM_MODELS:
    GLM_MODELS.insert(0, GLM_MODEL)
# 当前使用的模型下标（429 时由 rotate_glm_model 推进）
_glm_idx = 0


def current_glm_model() -> str:
    """当前轮到的 GLM 模型名"""
    return GLM_MODELS[_glm_idx] if GLM_MODELS else GLM_MODEL


def rotate_glm_model() -> str:
    """429 限流时轮换到下一个 GLM 模型，返回新模型名。"""
    global _glm_idx
    if len(GLM_MODELS) <= 1:
        return GLM_MODEL
    _glm_idx = (_glm_idx + 1) % len(GLM_MODELS)
    log.warning(f"[llm] GLM 429 限流，轮换到 {GLM_MODELS[_glm_idx]}")
    return GLM_MODELS[_glm_idx]


def reload() -> None:
    """在不重启进程的前提下，从 os.environ 重新读取所有 LLM 配置。

    用于 /welcome 引导页保存 Key 后即时生效：
    1. voice_server 先 load_dotenv(override=True) 把新 .env 写入 os.environ
    2. 调用本函数刷新本模块的模块级全局变量（resolve() 直接引用它们）
    3. voice_agent.reload_brain_config() 清除缓存大脑，下次请求用新配置重建
    """
    global OLLAMA_HOST, OLLAMA_MODEL, OLLAMA_OPENAI_BASE
    global ARK_BASE, ARK_KEY, ARK_MODEL
    global GLM_BASE, GLM_KEY, GLM_MODEL, GLM_MODELS, _glm_idx

    OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
    OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3.5:2b")
    OLLAMA_OPENAI_BASE = OLLAMA_HOST + "/v1"

    ARK_BASE = os.getenv("ARK_BASE", "https://ark.cn-beijing.volces.com/api/plan/v3")
    ARK_KEY = os.getenv("ARK_KEY", "")
    ARK_MODEL = os.getenv("ARK_MODEL", "ark-code-latest")

    GLM_BASE = os.getenv("GLM_BASE", "https://open.bigmodel.cn/api/paas/v4")
    GLM_KEY = os.getenv("GLM_KEY", "")
    GLM_MODEL = os.getenv("GLM_MODEL", "glm-4.7-flash")

    GLM_MODELS = [m.strip() for m in os.getenv("GLM_MODELS",
        "glm-4.7-flash,glm-4-flash,glm-4.5-flash").split(",") if m.strip()]
    if GLM_MODEL and GLM_MODEL not in GLM_MODELS:
        GLM_MODELS.insert(0, GLM_MODEL)
    _glm_idx = 0
    log.info(f"[llm] 配置已热重载: GLM={'已配置' if GLM_KEY else '未配置'}, "
             f"ARK={'已配置' if ARK_KEY else '未配置'}, model={GLM_MODEL}")


def verify_glm_key(api_key: str = "", model: str = "") -> tuple[bool, str]:
    """验证智谱 GLM Key 是否有效（发一个最小的 chat/completions 请求）。

    返回 (ok, message)。max_tokens=1 保持轻量；网络错误不阻断保存。
    """
    key = (api_key or GLM_KEY).strip()
    mdl = (model or GLM_MODEL or "glm-4-flash").strip()
    if not key:
        return False, "请填入 GLM Key"
    try:
        import requests as _req
        r = _req.post(f"{GLM_BASE.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": mdl, "messages": [{"role": "user", "content": "hi"}],
                  "max_tokens": 1, "extra_body": {"thinking": {"type": "disabled"}}},
            timeout=10)
        if r.status_code == 200:
            return True, f"GLM Key 验证通过（{mdl}）"
        try:
            err = r.json()
            desc = err.get("error", {}).get("message") if isinstance(err.get("error"), dict) else err.get("error")
            desc = desc or r.text[:120]
        except Exception:
            desc = r.text[:120]
        # 429/模型繁忙不等于 Key 无效：Key 本身有效，实际使用时有模型轮换兜底
        if r.status_code == 429 or "访问量过大" in str(desc) or "rate" in str(desc).lower() or "429" in str(desc):
            return True, f"GLM Key 有效（{mdl} 当前繁忙，将自动切换备用模型）"
        # 401/403/key 错误才是真正的 Key 无效
        return False, f"GLM Key 无效：{desc}"
    except Exception as e:
        return False, f"无法连接智谱验证服务器（{e}），Key 已保存，可稍后重试"


def is_glm_configured() -> bool:
    """智谱 GLM 免费 Key 是否已配置"""
    from app.env_catalog import is_configured
    return is_configured("GLM_KEY")


def active_chat_endpoint() -> tuple[str, str, str]:
    """当前激活的 OpenAI 兼容对话端点 (api_base, api_key, model)。

    优先级：ARK > 智谱 GLM 免费。意图分类等原生 HTTP 调用走这里。
    都没配时返回空串（调用方应回退到关键词匹配 / Ollama）。
    """
    if is_ark_configured():
        return ARK_BASE, ARK_KEY, ARK_MODEL
    if is_glm_configured():
        return GLM_BASE, GLM_KEY, current_glm_model()
    return "", "", ""


def is_ark_configured() -> bool:
    """ARK_KEY 是否已配置 — 委托到 env_catalog（#5 修复：单一来源）"""
    from app.env_catalog import is_configured
    return is_configured("ARK_KEY")


def demo_mode_active() -> bool:
    """当前是否处于 Demo 模式 — 委托到 env_catalog"""
    from app.env_catalog import demo_mode_active as _env_demo
    return _env_demo()


def ollama_online() -> bool:
    """探测 Ollama 服务是否在线 + 模型存在"""
    try:
        import requests as _req
        r = _req.get(OLLAMA_HOST + "/api/tags", timeout=1.5)
        if r.status_code != 200:
            return False
        models = {m.get("name", "") for m in r.json().get("models", [])}
        if OLLAMA_MODEL in models or any(m.startswith(OLLAMA_MODEL.split(":")[0]) for m in models):
            return True
        log.warning(f"[ollama] 服务在线但模型 {OLLAMA_MODEL} 未拉取")
        return False
    except Exception as e:
        log.warning(f"[ollama] 探测失败（{e}）")
        return False


def resolve() -> dict:
    """返回 llm_cfg dict（ARK > 智谱 GLM 免费 > Ollama 本地可选）

    优先级:
    - ARK_KEY 已配 → 火山引擎 ARK
    - GLM_KEY 已配 → 智谱 GLM 免费大脑（glm-4.7-flash 永久免费，云端无需本地硬件）
    - 都没配 → Ollama 本地（仅当用户明确启用且服务在线时），否则引导用户注册免费 GLM Key

    开箱即用版推荐: GLM免费Key + 百度免费ASR/TTS额度，无需本地GPU。
    """
    if is_ark_configured():
        return {
            'model': ARK_MODEL, 'model_type': 'oai',
            'api_base': ARK_BASE, 'api_key': ARK_KEY,
            'generate_cfg': {'use_raw_api': True, 'extra_body': {'extra_body': {'enable_thinking': False}}, 'max_tokens': 1024},
        }
    if is_glm_configured():
        _m = current_glm_model()
        log.info(f"[llm] ━━━ 免费 LLM ━━━ 智谱 {_m}")
        # 关闭思考(thinking):glm-4.5/4.7-flash 是推理模型,返回 reasoning_content
        # 会导致 Qwen-Agent 取不到 content。thinking:disabled 让其只返回纯 content。
        return {
            'model': _m, 'model_type': 'oai',
            'api_base': GLM_BASE, 'api_key': GLM_KEY,
            'generate_cfg': {'use_raw_api': True, 'max_tokens': 1024,
                             'extra_body': {'thinking': {'type': 'disabled'}}},
        }
    # 无任何云端 LLM Key → 尝试 Ollama 本地（需用户自行安装，对硬件有要求）
    if os.getenv("OLLAMA_ENABLED", "0") == "1" and ollama_online():
        log.warning(f"[llm] ━━━ 本地模式 ━━━ Ollama {OLLAMA_MODEL}（需本地硬件支持）")
        return {
            'model': OLLAMA_MODEL, 'model_type': 'oai',
            'api_base': OLLAMA_OPENAI_BASE, 'api_key': 'ollama',
            'generate_cfg': {'use_raw_api': True, 'extra_body': {'extra_body': {'enable_thinking': False}}, 'max_tokens': 1024},
        }
    # 无可用 LLM → 引导用户注册免费 GLM Key
    raise RuntimeError(
        "未配置 LLM。请注册智谱 GLM 免费 Key（注册即送，glm-4.7-flash 永久免费）：\n"
        "  1. 打开 https://open.bigmodel.cn 注册账号\n"
        "  2. 「API Keys」→ 添加新 Key → 复制\n"
        "  3. 在 .env 或 /welcome 引导页填入 GLM_KEY=你的Key\n"
        "  4. 重启 Charlie 即可使用免费 AI 大脑\n"
        "（如需纯本地离线运行，安装 Ollama 后设置 OLLAMA_ENABLED=1）"
    )
