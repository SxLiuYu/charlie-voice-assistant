"""Charlie 环境变量注册表 — 单一来源

把原本散落在 30+ 文件的 os.getenv 调用元数据集中到这里：
- 每个变量的默认值、是否必需、所属分组、Demo 模式是否可用、获取指引
- 启动校验、setup 页面白名单、.env 模板生成 都从此派生

设计原则：
- "必需"宽松化 — 缺 key 不阻塞启动，由智谱 GLM 免费 Key 兜底（无需本地硬件）
- "分组"驱动 setup 页面卡片渲染
- "可调参"(tunable=True) 的高级参数不进 setup 白名单，避免界面臃肿
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Iterable


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EnvEntry:
    """单个环境变量的元数据"""
    name: str
    default: str = ""
    required: bool = False                # 必需（缺失会警告，但不阻塞启动）
    group: str = "misc"                  # 分组：core/llm_fallback/asr_tts/...
    demo_supported: bool = False         # 缺失时 Demo 模式可兜底
    tunable: bool = False                # 高级调参（不进 setup 白名单）
    secret: bool = False                 # 是否敏感（setup 页面用 password 输入框）
    get_guide: str = ""                  # 获取地址/说明
    description: str = ""                # 一句话用途说明

    @property
    def is_set(self) -> bool:
        """当前环境里是否已配置（非空且非默认占位）"""
        return is_configured(self.name)


# ---------------------------------------------------------------------------
# 占位符判定 — 单一来源（#5 修复：消除三处复刻）
# ---------------------------------------------------------------------------

# 占位符前缀/精确匹配列表（统一 strip + lower 比较）
_PLACEHOLDER_PREFIXES = ("你的",)
_PLACEHOLDER_EXACT = {"xxx", "your-key", "YOUR_KEY", "your_api_key", "your-key-here"}


def is_placeholder(val: str) -> bool:
    """判断一个值是否是占位符（非真实配置）"""
    if not val:
        return True
    val = val.strip()
    if not val:
        return True
    if any(val.startswith(p) for p in _PLACEHOLDER_PREFIXES):
        return True
    return val.lower() in _PLACEHOLDER_EXACT


def is_configured(name: str) -> bool:
    """指定环境变量是否已配置（非空且非占位符）— 单一来源"""
    return not is_placeholder(os.getenv(name, ""))


# ---------------------------------------------------------------------------
# 分组顺序（setup 页面/日志按此顺序展示）
# ---------------------------------------------------------------------------

GROUP_ORDER = [
    "core",          # 大脑 + 语音 + 天气（最小可用集）
    "llm_fallback",  # Ollama 本地可选（需硬件支持）
    "asr_local",     # SenseVoice 本地 ASR
    "feishu",        # 飞书消息/推送
    "tuya",          # 涂鸦红外空调
    "esp32",         # ESP32 开发板终端
    "ecommerce",     # 搜索/购物分析
    "push",          # 个性化热点推送
    "system",        # 鉴权/端口/CORS/多用户
    "tuning",        # 高级调参（默认值够用，不进 setup 白名单）
]

GROUP_LABELS = {
    "core": "核心（必需）",
    "llm_fallback": "LLM 本地降级（Demo 模式）",
    "asr_local": "本地 ASR（可选，加速语音识别）",
    "feishu": "飞书（消息推送/文档）",
    "tuya": "涂鸦红外空调控制",
    "esp32": "ESP32 开发板终端",
    "ecommerce": "搜索与购物分析",
    "push": "个性化热点推送",
    "system": "运行时系统配置",
    "tuning": "高级调参（一般无需修改）",
}


# ---------------------------------------------------------------------------
# 注册表 — 所有环境变量
# ---------------------------------------------------------------------------

ENTRIES: list[EnvEntry] = [
    # --- core（核心：缺这些只会降级，但建议都配） ---
    # ---- LLM 节点（通过 LLM_PRIORITY 优先级链启用，顺序: stepfun,agnes,sagnes,glm,ark） ----
    EnvEntry("LLM_PRIORITY", default="stepfun,agnes,sagnes,glm,ark",
             group="core", tunable=False,
             description="LLM 优先级链（逗号分隔，如: stepfun,agnes,sagnes,glm,ark）"),
    EnvEntry("AGNES_KEY", group="core", secret=True, demo_supported=True,
             description="Agnes 中国节点 Key（默认首选 LLM，有免费额度）"),
    EnvEntry("AGNES_BASE", default="https://apihub.agnes-ai.com/v1",
             group="core", tunable=True,
             description="Agnes 中国节点 API 基址"),
    EnvEntry("AGNES_MODEL", default="agnes-2.0-flash",
             group="core", tunable=True,
             description="Agnes 中国节点模型名"),
    EnvEntry("SAGNES_KEY", group="core", secret=True, demo_supported=True,
             description="Agnes 新加坡节点 Key"),
    EnvEntry("SAGNES_BASE", default="https://api-sg.agnes-ai.com/v1",
             group="core", tunable=True,
             description="Agnes 新加坡节点 API 基址"),
    EnvEntry("SAGNES_MODEL", default="agnes-2.0-flash",
             group="core", tunable=True,
             description="Agnes 新加坡节点模型名"),
    # 智谱 GLM 免费大脑 — glm-4.7-flash 永久免费，OpenAI 兼容，作为 LLM 备选节点
    EnvEntry("GLM_KEY", group="core", secret=True, demo_supported=True,
             get_guide="https://open.bigmodel.cn/apikey/platform",
             description="智谱 GLM Key（永久免费 LLM 备选，通过 LLM_PRIORITY 启用）"),
    EnvEntry("GLM_BASE", default="https://open.bigmodel.cn/api/paas/v4",
             group="core", tunable=True,
             description="智谱 API 基址（OpenAI 兼容，通过 LLM_PRIORITY 启用）"),
    EnvEntry("GLM_MODEL", default="glm-4.7-flash",
             group="core", tunable=True,
             description="智谱模型名（glm-4.7-flash 永久免费，通过 LLM_PRIORITY 启用）"),
    EnvEntry("GLM_MODELS", default="glm-4.7-flash,glm-4-flash,glm-4.5-flash",
             group="core", tunable=True,
             description="GLM 429 限流 fallback 链（逗号分隔，按顺序轮换，通过 LLM_PRIORITY 启用）"),
    # 火山引擎 ARK — 作为 LLM 备选节点
    EnvEntry("ARK_KEY", group="core", secret=True, demo_supported=True,
             get_guide="https://console.volcengine.com/ark",
             description="火山引擎 ARK Key（LLM 备选，通过 LLM_PRIORITY 启用）"),
    EnvEntry("ARK_BASE", default="https://ark.cn-beijing.volces.com/api/plan/v3",
             group="core", tunable=True,
             description="ARK API 基址（通过 LLM_PRIORITY 启用）"),
    EnvEntry("ARK_MODEL", default="ark-code-latest", group="core", tunable=True,
             description="ARK 模型名（通过 LLM_PRIORITY 启用）"),
    # StepFun 节点（LLM + ASR 降级备选）
    EnvEntry("STEPFUN_KEY", group="core", secret=True, demo_supported=True,
             get_guide="https://platform.stepfun.com",
             description="StepFun API Key（LLM/ASR 降级备选。注意: LLM 的 reasoning 无法禁用，延迟比 Agnes 高。通过 LLM_PRIORITY 启用）"),
    EnvEntry("STEPFUN_LLM_BASE", default="https://api.stepfun.com/step_plan/v1",
             group="core", tunable=True,
             description="StepFun LLM API 基址（通过 LLM_PRIORITY 启用）"),
    EnvEntry("STEPFUN_LLM_MODEL", default="step-3.5-flash-2603",
             group="core", tunable=True,
             description="StepFun LLM 模型名（通过 LLM_PRIORITY 启用）"),
    EnvEntry("STEPFUN_ASR_BASE", default="https://api.stepfun.com/v1",
             group="core", tunable=True,
             description="StepFun ASR API 基址（OpenAI 兼容 /audio/transcriptions）"),
    EnvEntry("STEPFUN_ASR_MODEL", default="stepaudio-2.5-asr",
             group="core", tunable=True,
             description="StepFun ASR 模型名"),
    EnvEntry("STEPFUN_TTS_BASE", default="https://api.stepfun.com/v1",
             group="core", tunable=True,
             description="StepFun TTS API 基址（OpenAI 兼容 /audio/speech）"),
    EnvEntry("STEPFUN_TTS_MODEL", default="step-tts-2",
             group="core", tunable=True,
             description="StepFun TTS 模型名"),
    EnvEntry("STEPFUN_TTS_VOICE", default="cixingnansheng",
             group="core", tunable=True,
             description="StepFun TTS 系统预设音色名（如 cixingnansheng）"),
    EnvEntry("BAIDU_APP_ID", required=True, group="core", secret=True,
             get_guide="https://console.bce.baidu.com/ai/#/ai/speech/overview/index",
             description="百度智能云 App ID（ASR + TTS）"),
    EnvEntry("BAIDU_API_KEY", required=True, group="core", secret=True,
             description="百度 API Key"),
    EnvEntry("BAIDU_SECRET_KEY", required=True, group="core", secret=True,
             description="百度 Secret Key"),
    EnvEntry("AMAP_KEY", required=True, group="core", secret=True,
             get_guide="https://console.amap.com",
             description="高德地图 Key（天气查询）"),

    # --- llm_fallback（Ollama 本地可选，需硬件支持） ---
    EnvEntry("OLLAMA_ENABLED", default="0", group="llm_fallback",
             tunable=True,
             description="是否启用 Ollama 本地模式（1=启用，需安装 Ollama 且硬件足够；已移除，保留兼容）"),
    EnvEntry("OLLAMA_HOST", default="http://localhost:11434", group="llm_fallback",
             description="Ollama 服务地址（仅在 OLLAMA_ENABLED=1 时使用；已移除，保留兼容）"),
    EnvEntry("OLLAMA_MODEL", default="qwen3.5:2b", group="llm_fallback",
             tunable=True,
             description="Ollama 本地模型名，需 ollama pull <model>（仅在 OLLAMA_ENABLED=1 时使用；已移除，保留兼容）"),

    # --- asr_local（本地 SenseVoice ASR） ---
    EnvEntry("SENSE_VOICE_MODEL", default="models/sense-voice",
             group="asr_local", tunable=True,
             get_guide="https://github.com/k2-fsa/sherpa-onnx/releases",
             description="SenseVoice 本地 ASR 模型路径（26ms vs 百度 327ms）"),
    EnvEntry("SENSE_VOICE_DISABLED", default="0", group="asr_local", tunable=True,
             description="1=禁用本地 ASR，回退百度"),

    # --- feishu ---
    EnvEntry("FEISHU_APP_ID", group="feishu", secret=True,
             get_guide="https://open.feishu.cn",
             description="飞书应用 App ID"),
    EnvEntry("FEISHU_APP_SECRET", group="feishu", secret=True,
             description="飞书应用 App Secret"),
    EnvEntry("FEISHU_PUSH_OPEN_ID", group="feishu", secret=True,
             description="飞书推送目标用户 open_id（如 ou_xxx）"),
    EnvEntry("FEISHU_PUSH_ENABLED", default="1", group="feishu", tunable=True,
             description="0=禁用飞书主动推送"),
    EnvEntry("FEISHU_WEBHOOK", group="feishu", secret=True,
             description="飞书机器人 Webhook（watchdog 异常告警用）"),
    EnvEntry("FEISHU_VERIFICATION_TOKEN", group="feishu", secret=True,
             get_guide="https://open.feishu.cn/app → 事件订阅",
             description="飞书事件订阅 Verification Token（配置后启用双向对话）"),

    # --- tuya（红外空调） ---
    EnvEntry("TUYA_CLIENT_ID", group="tuya", secret=True,
             get_guide="https://iot.tuya.com",
             description="涂鸦 2B 开发者 Client ID"),
    EnvEntry("TUYA_ACCESS_KEY", group="tuya", secret=True,
             description="涂鸦 2B 开发者 Access Key（sk- 开头）"),
    EnvEntry("TUYA_API_KEY", group="tuya", secret=True,
             description="涂鸦 2C 终端用户 API Key（Bearer，读设备状态用）"),
    EnvEntry("TUYA_AC_DEVICE_ID", group="tuya",
             description="空调设备 ID（与红外遥控器同）"),
    EnvEntry("TUYA_IR_DEVICE_ID", group="tuya",
             description="红外网关设备 ID"),
    EnvEntry("TUYA_BASE_URL", default="https://openapi.tuyacn.com",
             group="tuya", tunable=True,
             description="涂鸦 2B API 基址"),

    # --- esp32（开发板终端） ---
    EnvEntry("ESP32_IP", group="esp32",
             description="ESP32 开发板局域网 IP（缺失则 ESP32 相关功能不可用）"),
    EnvEntry("ESP32_OTA_IP", group="esp32",
             description="OTA 返回给 ESP32 的服务器 IP（默认空=自动探测 LAN IP）"),

    # --- mqtt（ESP32 主动推送信令通道） ---
    EnvEntry("MQTT_BROKER", group="mqtt",
             description="MQTT broker 地址（本地 mosquitto 或公网 EMQX）"),
    EnvEntry("MQTT_PORT", group="mqtt", default="1883",
             description="MQTT broker 端口"),
    EnvEntry("MQTT_DEVICE_ID", group="mqtt", default="esp32-default",
             description="ESP32 设备 ID（固件 hello 上报的 chip_id，用于 MQTT topic 路由）"),
    EnvEntry("MQTT_ENABLE_OTA", group="mqtt", default="0", tunable=True,
             description="1=OTA 响应返回 mqtt 段（当前固件 MQTT 不 subscribe，默认关闭走 WebSocket）"),
    EnvEntry("MQTT_USER", group="mqtt", secret=True,
             description="MQTT broker 用户名（需要认证时填写）"),
    EnvEntry("MQTT_PASSWORD", group="mqtt", secret=True,
             description="MQTT broker 密码"),

    # --- ecommerce ---
    EnvEntry("TAVILY_API_KEY", group="ecommerce", secret=True,
             get_guide="https://tavily.com",
             description="Tavily 搜索（免费 1000 次/月）"),
    EnvEntry("ALIYUN_API_KEY", group="ecommerce", secret=True,
             get_guide="https://dashscope.aliyun.com",
             description="阿里云 DashScope（购物分析/翻译）"),

    # --- push（个性化热点推送） ---

    EnvEntry("PERSONALIZED_PUSH_INTERVAL", default="3600", group="push", tunable=True,
             description="热点推送间隔秒数（默认 1 小时）"),
    EnvEntry("DEFAULT_CITY", default="北京", group="push",
             description="默认城市（天气/穿搭推荐）"),

    # --- notifications（ntfy 备用通知通道） ---
    EnvEntry("NTFY_URL", group="push",
             description="ntfy 服务地址（如 https://ntfy.sh，留空则不推送）"),
    EnvEntry("NTFY_TOPIC", group="push",
             description="ntfy topic（填了才启用，如 charlie-alerts）"),
    EnvEntry("NTFY_AUTH", group="push", secret=True,
             description="ntfy 认证（格式 user:pass，可选）"),

    # --- system（运行时配置） ---
    EnvEntry("AUTH_TOKEN", group="system", secret=True,
             description="访问鉴权 Token（设置后非 localhost 请求需携带）"),
    EnvEntry("INTERNAL_API_TOKEN", group="system", secret=True,
             description="内部跨进程推送鉴权 Token（HTTP/HTTPS 分离部署时用；留空仅允许本机）"),
    EnvEntry("ASSISTANT_KID_TRUST_PROXY_HEADERS", default="0", group="system", tunable=True,
             description="1=信任 X-Forwarded-For（反代场景）"),
    EnvEntry("ASSISTANT_KID_HTTP_PORT", default="8000", group="system", tunable=True,
             description="HTTP 端口"),
    EnvEntry("ASSISTANT_KID_HTTPS_PORT", default="8443", group="system", tunable=True,
             description="HTTPS 端口"),
    EnvEntry("ASSISTANT_KID_CORS_ORIGINS", group="system", tunable=True,
             description="额外允许的 CORS 源（逗号分隔）"),
    EnvEntry("ASSISTANT_KID_DATA_DIR", group="system", tunable=True,
             description="数据目录（默认项目根）"),
    EnvEntry("ASSISTANT_KID_LOG_DIR", group="system", tunable=True,
             description="日志目录（默认项目根/logs）"),
    EnvEntry("CHARLIE_USER_ID", default="default", group="system", tunable=True,
             description="多用户 ID（按用户隔离对话历史/偏好）"),
    EnvEntry("SKIP_BACKGROUND", default="0", group="system", tunable=True,
             description="1=跳过后台调度器（HTTPS 进程用）"),
    EnvEntry("DECISION_ENGINE_ENABLED", default="1", group="system", tunable=True,
             description="1=启用自主决策引擎（早安简报/午饭/久坐/晚安等自动判断）"),
    EnvEntry("MCP_PROFILE", default="core", group="system", tunable=False,
             description="MCP 启用模式：core（默认 12 个）/ all（19 个）/ custom（读 MCP_SERVERS）"),
    EnvEntry("MCP_SERVERS", group="system", tunable=False,
             description="自定义 MCP 列表（逗号分隔，仅 MCP_PROFILE=custom 时生效）"),

    # --- tuning（高级调参，不进 setup 白名单） ---
    EnvEntry("LOG_FORMAT", group="tuning", tunable=True,
             description="日志格式"),
    EnvEntry("FINNA_API_KEY", group="tuning", secret=True,
             get_guide="https://www.finna.com.cn",
             description="Finna TTS（百度 TTS 失败时的降级）"),
    EnvEntry("FINNA_BASE", group="tuning", tunable=True,
             description="Finna API 基址"),
    EnvEntry("TTS_VOICE", group="tuning", tunable=True,
             description="Finna TTS 音色名"),
    EnvEntry("TTS_MODEL", group="tuning", tunable=True,
             description="Finna TTS 模型名"),
    EnvEntry("TTS_SPEED", group="tuning", tunable=True,
             description="TTS 语速"),
    EnvEntry("TTS_CACHE_MAX_CHARS", default="200", group="tuning", tunable=True,
             description="短文本 TTS 缓存上限字符数"),
    EnvEntry("TTS_FAILURE_THRESHOLD", default="3", group="tuning", tunable=True,
             description="TTS 连续失败熔断阈值"),
    EnvEntry("TTS_FAILURE_COOLDOWN", default="120", group="tuning", tunable=True,
             description="TTS 熔断冷却秒数"),
    EnvEntry("LOCAL_TTS_ENABLED", default="0", group="tuning", tunable=True,
             description="1=启用本地 TTS 降级"),
    EnvEntry("ASR_PRIORITY", default="sensevoice,baidu,stepfun,vosk", group="tuning", tunable=False,
             description="ASR 优先级链（逗号分隔，如: sensevoice,baidu,stepfun,vosk）"),
    EnvEntry("TTS_PRIORITY", default="baidu,stepfun,finna", group="tuning", tunable=False,
             description="TTS 优先级链（逗号分隔，如: baidu,stepfun,finna）"),
    EnvEntry("ASR_HOTWORDS", group="tuning", tunable=True,
             description="ASR 热词表（逗号分隔）"),
    EnvEntry("XIAOZHI_VAD_SILENCE_FRAMES", default="8", group="tuning", tunable=True,
             description="ESP32 VAD 尾静音帧数（0.06s/帧）"),
    EnvEntry("XIAOZHI_VAD_THRESHOLD", default="0.5", group="tuning", tunable=True,
             description="ESP32 VAD 阈值"),
    EnvEntry("KNOWN_DEVICES", group="tuning", tunable=True,
             description="多设备存在检测（格式 name=...;mac=...;ip=...）"),
    EnvEntry("WAKE_WORDS", group="tuning", tunable=True,
             description="本地唤醒词（逗号分隔）"),
    EnvEntry("ASSISTANT_KID_INTENT_FAILURE_THRESHOLD", default="2", group="tuning", tunable=True,
             description="意图分类熔断阈值"),
    EnvEntry("ASSISTANT_KID_INTENT_FAILURE_COOLDOWN", default="30", group="tuning", tunable=True,
             description="意图分类熔断冷却秒数"),
    EnvEntry("ASSISTANT_KID_RETRY_AFTER_CAP", default="60", group="tuning", tunable=True,
             description="重试延时上限秒数"),
]


# ---------------------------------------------------------------------------
# 索引
# ---------------------------------------------------------------------------

_BY_NAME: dict[str, EnvEntry] = {e.name: e for e in ENTRIES}


def get(name: str) -> EnvEntry | None:
    """按名查注册项"""
    return _BY_NAME.get(name)


def all_entries() -> list[EnvEntry]:
    """全部注册项（按定义顺序）"""
    return list(ENTRIES)


def entries_for_group(group: str) -> list[EnvEntry]:
    """按组取注册项"""
    return [e for e in ENTRIES if e.group == group]


def groups_in_order() -> list[str]:
    """分组顺序（setup 页面展示用）"""
    return list(GROUP_ORDER)


def group_label(group: str) -> str:
    """分组中文标签"""
    return GROUP_LABELS.get(group, group)


# ---------------------------------------------------------------------------
# 校验 / 状态查询
# ---------------------------------------------------------------------------

def missing_required() -> list[EnvEntry]:
    """所有缺失的"必需"项（按 group 顺序）"""
    return [e for e in ENTRIES if e.required and not e.is_set]


def llm_available() -> bool:
    """是否有任何一种 LLM 可用：AGNES / SAGNES / STEPFUN / GLM / ARK 已配

    开箱即用版推荐 AGNES 免费 Key + 百度免费 ASR/TTS，无需本地 GPU。
    """
    return (is_configured("AGNES_KEY")
            or is_configured("SAGNES_KEY")
            or is_configured("STEPFUN_KEY")
            or is_configured("GLM_KEY")
            or is_configured("ARK_KEY"))


def demo_mode_active() -> bool:
    """当前是否处于未配置状态（未配任何 LLM Key）

    此状态下启动会引导用户注册免费 AGNES Key。
    """
    return not (is_configured("AGNES_KEY")
                or is_configured("SAGNES_KEY")
                or is_configured("STEPFUN_KEY")
                or is_configured("GLM_KEY")
                or is_configured("ARK_KEY"))


def setup_whitelist_keys() -> list[str]:
    """setup 页面允许保存的 key 白名单（必需 + 各组主键，不含纯 tunable）"""
    return [e.name for e in ENTRIES if not e.tunable]


def env_template_lines() -> list[str]:
    """生成 .env 模板文本（带分组注释）

    用于 .env.example 和 _ENV_DEFAULTS 缺失时初始化。
    """
    lines: list[str] = ["# Charlie 语音助手 — 环境变量配置",
                        "# 复制此文件为 .env，填入你自己的 API 密钥",
                        "# 未配置的项会自动降级或禁用，不会导致启动失败",
                        ""]
    seen_groups: set[str] = set()
    for e in ENTRIES:
        if e.group not in seen_groups:
            seen_groups.add(e.group)
            lines.append(f"# ===== {group_label(e.group)} =====")
        # 默认值或占位
        if e.default:
            val = e.default
        elif e.secret:
            val = ""
        elif e.required:
            val = ""
        else:
            val = ""
        comment = f"  # {e.description}" if e.description else ""
        lines.append(f"{e.name}={val}{comment}")
        if e.get_guide:
            lines.append(f"# 获取: {e.get_guide}")
    return lines


def render_env_template() -> str:
    return "\n".join(env_template_lines()) + "\n"


def status_report() -> list[dict[str, object]]:
    """给 /api/setup/mcp-status 等展示用：每个变量的状态快照"""
    return [
        {
            "name": e.name,
            "group": e.group,
            "required": e.required,
            "demo_supported": e.demo_supported,
            "configured": e.is_set,
            "description": e.description,
        }
        for e in ENTRIES
    ]


# ---------------------------------------------------------------------------
# 投影方法（#7 修复：消除 voice_server 散乱的 setup 链路）
# voice_server 的 _validate_env / welcome_status / get_setup / mcp_status
# 都改为调用这些单一来源方法。
# ---------------------------------------------------------------------------

def render_startup_log() -> list[str]:
    """启动时按分组打印配置状态（替代 voice_server._validate_env 的手写日志）"""
    lines = []
    for group in GROUP_ORDER:
        entries = entries_for_group(group)
        if not entries:
            continue
        lines.append(f"--- {group_label(group)} ---")
        for e in entries:
            tag = "✅" if e.is_set else ("⚠️" if e.required else "  ")
            note = ""
            if not e.is_set and e.demo_supported:
                note = " (Demo 可用)"
            elif not e.is_set and e.required:
                note = " (必需, 缺失)"
            # 对 SENSE_VOICE_DISABLED 显示实际生效值（0=启用本地ASR，1=禁用）
            extra = ""
            if e.name == "SENSE_VOICE_DISABLED":
                extra = f"={os.getenv(e.name, e.default)}"
            lines.append(f"  {tag} {e.name}{extra}{note}  {e.description}")
    return lines


def render_welcome_status() -> dict:
    """给 /api/welcome/status 返回的状态（单一来源，消除混用真值源）"""
    return {
        "has_env": True,  # voice_server 层判断 .env 是否存在
        "demo_mode": demo_mode_active(),
        "ollama_online": False,  # voice_server 层从 llm_config 取
        "missing_required": [e.name for e in missing_required()],
        "llm_available": llm_available(),
        # 引导向导用：各模块是否已配（不暴露密钥值）
        "baidu_configured": (is_configured("BAIDU_APP_ID")
                             and is_configured("BAIDU_API_KEY")
                             and is_configured("BAIDU_SECRET_KEY")),
        "agnes_configured": is_configured("AGNES_KEY"),
        "sagnes_configured": is_configured("SAGNES_KEY"),
        "stepfun_configured": is_configured("STEPFUN_KEY"),
        "glm_configured": is_configured("GLM_KEY"),
        "ark_configured": is_configured("ARK_KEY"),
    }