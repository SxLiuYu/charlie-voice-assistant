"""LLM 配置解析 — Provider Registry 模式

负责：
- 按 LLM_PRIORITY 优先级链选择 LLM 节点
- 返回 llm_cfg dict（供 qwen_agent 使用）
- 提供 provider registry，新增 provider 只需在 PROVIDERS dict 加条目

对外兼容接口（保持不变，旧 import 无需改动）：
  resolve(), reload(), active_chat_endpoint()
  demo_mode_active(), llm_available()
  is_agnes_configured(), is_sagnes_configured(), is_stepfun_configured()
  AGNES_BASE, AGNES_KEY, AGNES_MODEL, SAGNES_BASE, SAGNES_KEY, SAGNES_MODEL
  STEPFUN_LLM_BASE, STEPFUN_KEY, STEPFUN_LLM_MODEL, LLM_PRIORITY
"""
import os
import logging
from typing import Optional

log = logging.getLogger("magic")

# ======================================================================
# Provider Registry
# ======================================================================
# 每个 provider 的描述字段：
#   base_env        — base URL 环境变量名
#   key_env         — API Key 环境变量名
#   model_env       — 模型名环境变量名
#   default_base    — base URL 默认值
#   default_key     — Key 默认值（通常为空）
#   default_model   — 模型名默认值
#   max_tokens      — generate_cfg 里用到的 max_tokens 默认值
#   thinking_disabled — generate_cfg 里 thinking.type 固定值
#   label           — 中文名称（日志/错误提示用）
#   models_env      — （可选）轮换模型链环境变量名，未配则不轮换
# ======================================================================

PROVIDERS: dict[str, dict] = {
    "agnes": {
        "base_env": "AGNES_BASE",
        "key_env": "AGNES_KEY",
        "model_env": "AGNES_MODEL",
        "default_base": "https://apihub.agnes-ai.com/v1",
        "default_key": "",
        "default_model": "agnes-2.0-flash",
        "max_tokens": 512,
        "thinking_disabled": True,
        "label": "Agnes 中国节点",
    },
    "sagnes": {
        "base_env": "SAGNES_BASE",
        "key_env": "SAGNES_KEY",
        "model_env": "SAGNES_MODEL",
        "default_base": "https://api-sg.agnes-ai.com/v1",
        "default_key": "",
        "default_model": "agnes-2.0-flash",
        "max_tokens": 512,
        "thinking_disabled": True,
        "label": "Agnes 新加坡节点",
    },
    "stepfun": {
        "base_env": "STEPFUN_LLM_BASE",
        "key_env": "STEPFUN_KEY",
        "model_env": "STEPFUN_LLM_MODEL",
        "default_base": "https://api.stepfun.com/step_plan/v1",
        "default_key": "",
        "default_model": "step-3.5-flash-2603",
        "max_tokens": 512,
        "thinking_disabled": True,
        "label": "StepFun",
        # step-3.5-flash-2603 原生支持 reasoning_effort=low，可进一步压缩 reasoning 字数
    },
    "glm": {
        "base_env": "GLM_BASE",
        "key_env": "GLM_KEY",
        "model_env": "GLM_MODEL",
        "default_base": "https://open.bigmodel.cn/api/paas/v4",
        "default_key": "",
        "default_model": "glm-4.7-flash",
        "max_tokens": 1024,
        "thinking_disabled": True,
        "label": "智谱 GLM",
        "models_env": "GLM_MODELS",
    },
    "ark": {
        "base_env": "ARK_BASE",
        "key_env": "ARK_KEY",
        "model_env": "ARK_MODEL",
        "default_base": "https://ark.cn-beijing.volces.com/api/plan/v3",
        "default_key": "",
        "default_model": "ark-code-latest",
        "max_tokens": 1024,
        "thinking_disabled": True,
        "label": "火山引擎 ARK",
    },
}

# ======================================================================
# 模块级兼容变量（保持旧 import 可用）
# 从环境变量读取（与原始行为一致，importlib.reload 后自动反映 env 变化）
# ======================================================================

AGNES_BASE: str = os.getenv("AGNES_BASE", PROVIDERS["agnes"]["default_base"])
AGNES_KEY: str = os.getenv("AGNES_KEY", "")
AGNES_MODEL: str = os.getenv("AGNES_MODEL", PROVIDERS["agnes"]["default_model"])

SAGNES_BASE: str = os.getenv("SAGNES_BASE", PROVIDERS["sagnes"]["default_base"])
SAGNES_KEY: str = os.getenv("SAGNES_KEY", "")
SAGNES_MODEL: str = os.getenv("SAGNES_MODEL", PROVIDERS["sagnes"]["default_model"])

STEPFUN_LLM_BASE: str = os.getenv("STEPFUN_LLM_BASE", PROVIDERS["stepfun"]["default_base"])
STEPFUN_KEY: str = os.getenv("STEPFUN_KEY", "")
STEPFUN_LLM_MODEL: str = os.getenv("STEPFUN_LLM_MODEL", PROVIDERS["stepfun"]["default_model"])

GLM_BASE: str = os.getenv("GLM_BASE", PROVIDERS["glm"]["default_base"])
GLM_KEY: str = os.getenv("GLM_KEY", "")
GLM_MODEL: str = os.getenv("GLM_MODEL", PROVIDERS["glm"]["default_model"])

ARK_BASE: str = os.getenv("ARK_BASE", PROVIDERS["ark"]["default_base"])
ARK_KEY: str = os.getenv("ARK_KEY", "")
ARK_MODEL: str = os.getenv("ARK_MODEL", PROVIDERS["ark"]["default_model"])

# LLM 优先级链（默认 stepfun → agnes → sagnes → glm → ark，import 时读一次 env）
LLM_PRIORITY: list[str] = [
    p.strip() for p in os.getenv("LLM_PRIORITY", "stepfun,agnes,sagnes,glm,ark").lower().split(",")
    if p.strip() and p.strip() in PROVIDERS
]

# ===== Ollama 兼容（已移除，保留变量名供旧 import 兼容）=====
OLLAMA_HOST: str = ""
OLLAMA_MODEL: str = ""
OLLAMA_OPENAI_BASE: str = ""


# ======================================================================
# 内部辅助
# ======================================================================

def _is_configured(key: str) -> bool:
    """检查环境变量是否已配置（非空且非占位符）"""
    val = os.getenv(key, "").strip()
    if not val:
        return False
    placeholders = {"your-key", "xxx", "YOUR_KEY"}
    return val.lower() not in placeholders


def _read_env(name: str, default: str = "") -> str:
    """读环境变量，去掉首尾空格"""
    return os.getenv(name, default).strip()


def _is_provider_configured(name: str) -> bool:
    """从 PROVIDERS registry 检查某个 provider 的 key 是否已配"""
    p = PROVIDERS.get(name)
    if p is None:
        return False
    return _is_configured(p["key_env"])


def _get_provider_cfg(name: str) -> dict:
    """从环境变量读取并返回某 provider 的运行时配置（base, key, model）"""
    p = PROVIDERS[name]
    return {
        "base": _read_env(p["base_env"], p["default_base"]),
        "key": _read_env(p["key_env"], p["default_key"]),
        "model": _read_env(p["model_env"], p["default_model"]),
    }


# ======================================================================
# 优先级链
# ======================================================================

def _get_priority_list() -> list[str]:
    """获取当前 LLM 优先级链（仅含 registry 中存在的 provider 名）"""
    raw = os.getenv("LLM_PRIORITY", "stepfun,agnes,sagnes,glm,ark").lower().split(",")
    return [p.strip() for p in raw if p.strip() and p.strip() in PROVIDERS]


# ======================================================================
# 兼容函数：各节点 Key 是否已配置（agent/llm.py 直接导入使用）
# ======================================================================

def is_agnes_configured() -> bool:
    return _is_provider_configured("agnes")


def is_sagnes_configured() -> bool:
    return _is_provider_configured("sagnes")


def is_stepfun_configured() -> bool:
    return _is_provider_configured("stepfun")


def is_glm_configured() -> bool:
    return _is_provider_configured("glm")


def is_ark_configured() -> bool:
    return _is_provider_configured("ark")


# ======================================================================
# active_chat_endpoint — 返回 (base, key, model) tuple
# ======================================================================

def active_chat_endpoint() -> tuple[str, str, str]:
    """当前激活的 OpenAI 兼容对话端点 (api_base, api_key, model)。

    按 LLM_PRIORITY 优先级链依次检查，返回第一个可用的。
    都没配时返回空串。
    """
    for name in _get_priority_list():
        if _is_provider_configured(name):
            cfg = _get_provider_cfg(name)
            return cfg["base"], cfg["key"], cfg["model"]
    return "", "", ""


# ======================================================================
# resolve — 返回 llm_cfg dict（供 qwen_agent 使用）
# ======================================================================

def _build_generate_cfg(provider_name: str) -> dict:
    """根据 registry 生成 generate_cfg 片段"""
    p = PROVIDERS[provider_name]
    cfg: dict = {
        "use_raw_api": True,
        "max_tokens": p["max_tokens"],
        # 流式 read timeout：chunk 间最大等待（OpenAI SDK 直接接受 timeout kwarg）。
        # 首包超过 30s 视为端点异常，抛超时 → brain_stream_sentences 走回退链。
        "timeout": 30,
    }
    if provider_name == "stepfun":
        # StepFun reasoning 无法完全禁用，同时传 thinking=disabled + reasoning_effort=low
        # 实测两者叠加可减少 reasoning 字数（~370字 vs 700字），缩短首 content 延迟
        cfg["extra_body"] = {
            "thinking": {"type": "disabled"},
            "reasoning_effort": "low",
        }
    else:
        thinking_type = "disabled" if p["thinking_disabled"] else "enabled"
        cfg["extra_body"] = {"thinking": {"type": thinking_type}}
    return cfg


def resolve() -> dict:
    """返回 llm_cfg dict，按 LLM_PRIORITY 优先级链选择第一个可用的 provider。

    llm_cfg 结构（qwen_agent 依赖）：
      {
        'model': <model_name>,
        'model_type': 'oai',
        'api_base': <base_url>,
        'api_key': <api_key>,
        'generate_cfg': { 'use_raw_api': True, 'max_tokens': N,
                          'extra_body': { 'thinking': { 'type': ... } } }
      }
    """
    for name in _get_priority_list():
        if not _is_provider_configured(name):
            continue

        p = PROVIDERS[name]
        cfg = _get_provider_cfg(name)

        log.info(
            f"[llm] ━━━ LLM ━━━ {p['label']} {cfg['model']}"
            + (" (禁用thinking)" if p["thinking_disabled"] else "")
            + (f" (reasoning_effort=low, max_tokens={p['max_tokens']})"
               if name == "stepfun" else "")
        )

        return {
            'model': cfg["model"],
            'model_type': 'oai',
            'api_base': cfg["base"],
            'api_key': cfg["key"],
            'generate_cfg': _build_generate_cfg(name),
        }

    # ---- 无可用 LLM ----
    available = []
    for name, p in PROVIDERS.items():
        available.append(
            f"  - {name}: {p['key_env']} + {p['base_env']} ({p['label']})"
        )
    raise RuntimeError(
        "未配置 LLM。请至少配置一个 LLM Provider 的 API Key。\n"
        "配置示例:\n"
        "  LLM_PRIORITY=stepfun,agnes,sagnes,glm,ark  (按优先级排列)\n"
        "\n"
        "可用节点:\n"
        + "\n".join(available) +
        "\n\n获取 Key: https://console.agnes-ai.com  (Agnes 有免费额度)"
    )


# ======================================================================
# reload — 热重载（从环境变量重新读取所有配置）
# ======================================================================

def reload() -> None:
    """热重载 LLM 配置（从环境变量重新读取）"""
    global LLM_PRIORITY
    global AGNES_BASE, AGNES_KEY, AGNES_MODEL
    global SAGNES_BASE, SAGNES_KEY, SAGNES_MODEL
    global STEPFUN_LLM_BASE, STEPFUN_KEY, STEPFUN_LLM_MODEL
    global GLM_BASE, GLM_KEY, GLM_MODEL
    global ARK_BASE, ARK_KEY, ARK_MODEL
    global _glm_rotation_idx

    _glm_rotation_idx = 0
    LLM_PRIORITY = _get_priority_list()

    # 同步所有模块级兼容变量
    AGNES_BASE = _read_env("AGNES_BASE", PROVIDERS["agnes"]["default_base"])
    AGNES_KEY = _read_env("AGNES_KEY", "")
    AGNES_MODEL = _read_env("AGNES_MODEL", PROVIDERS["agnes"]["default_model"])

    SAGNES_BASE = _read_env("SAGNES_BASE", PROVIDERS["sagnes"]["default_base"])
    SAGNES_KEY = _read_env("SAGNES_KEY", "")
    SAGNES_MODEL = _read_env("SAGNES_MODEL", PROVIDERS["sagnes"]["default_model"])

    STEPFUN_LLM_BASE = _read_env("STEPFUN_LLM_BASE", PROVIDERS["stepfun"]["default_base"])
    STEPFUN_KEY = _read_env("STEPFUN_KEY", "")
    STEPFUN_LLM_MODEL = _read_env("STEPFUN_LLM_MODEL", PROVIDERS["stepfun"]["default_model"])

    GLM_BASE = _read_env("GLM_BASE", PROVIDERS["glm"]["default_base"])
    GLM_KEY = _read_env("GLM_KEY", "")
    GLM_MODEL = _read_env("GLM_MODEL", PROVIDERS["glm"]["default_model"])

    ARK_BASE = _read_env("ARK_BASE", PROVIDERS["ark"]["default_base"])
    ARK_KEY = _read_env("ARK_KEY", "")
    ARK_MODEL = _read_env("ARK_MODEL", PROVIDERS["ark"]["default_model"])

    log.info(f"[llm] 配置已热重载: 优先级链={LLM_PRIORITY}")


# ======================================================================
# GLM 模型链（暴露给 llm.py 的 brain 层做 429 限流轮换）
# ======================================================================

def get_glm_models() -> list[str]:
    """读取 GLM_MODELS 环境变量，返回模型列表（用于 429 限流轮换）

    GLM_MODELS 格式：逗号分隔的模型名，如 "glm-4.7-flash,glm-4-flash,glm-4.5-flash"
    未配置或为空时返回空列表（由调用方决定是否回退到默认模型）
    """
    raw = os.getenv("GLM_MODELS", "").strip()
    if not raw:
        return []
    return [m.strip() for m in raw.split(",") if m.strip()]


GLM_MODELS: list[str] = get_glm_models()  # 模块级变量，兼容 import
_glm_rotation_idx: int = 0

def rotate_glm_model() -> str:
    """轮换 GLM 模型（429 限流时调用），返回新的模型名"""
    global _glm_rotation_idx, GLM_MODEL  # noqa: F824 — 模块级兼容变量
    models = get_glm_models()
    if not models:
        return GLM_MODEL  # fallback to default
    _glm_rotation_idx = (_glm_rotation_idx + 1) % len(models)
    new_model = models[_glm_rotation_idx]
    GLM_MODEL = new_model
    os.environ["GLM_MODEL"] = new_model  # 让 resolve() 的 _read_env 能读到
    log.info(f"[llm] GLM 模型轮换到: {new_model}")
    return new_model


def ollama_online() -> bool:
    """Ollama 已移除，恒返回 False（保留供旧 import 兼容）"""
    return False


# ======================================================================
# demo_mode_active / llm_available — 遍历 PROVIDERS 统一判断
# ======================================================================

def demo_mode_active() -> bool:
    """当前是否处于 Demo 模式（registry 中没有任何 provider 的 key 被配）"""
    return not any(_is_provider_configured(n) for n in PROVIDERS)


def llm_available() -> bool:
    """registry 中是否有任一 provider 的 key 已配置"""
    return any(_is_provider_configured(n) for n in PROVIDERS)
