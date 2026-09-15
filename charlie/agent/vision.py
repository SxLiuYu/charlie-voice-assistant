"""Vision fast path: screenshot + Mimo vision API analysis, bypassing LLM.

Extracted from voice_agent.py.
"""
import os, sys, platform, subprocess, tempfile, logging

log = logging.getLogger("magic")

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 项目根(charlie/)，skills/ 在此之下

_VISION_PREFIXES = [
    "看看看",
    "帮我看看",
    "帮我",
    "看看",
    "帮我识别",
    "帮我描述",
    "帮我分析",
    "截图分析",
    "识别",
    "分析一下",
    "描述一下",
    "描述",
    "屏幕上",
    "屏幕",
    "截屏",
    "截图",
    "看一下",
    "看下",
]


def _clean_vision_question(text: str) -> str:
    """有序剥离视觉请求前缀，剩余文本不足 2 字符时兜底。"""
    for prefix in _VISION_PREFIXES:
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    text = text.strip().rstrip("。.,，！!？?")
    if not text or len(text) < 2:
        text = "描述这张图片的内容"
    return text


def _run_vision_script(image_path: str, question: str) -> str:
    """调 mimo-vision 脚本分析本地图片，返回文本（失败返回空串）。"""
    VISION_SCRIPT = os.path.join(PROJECT_DIR, "skills", "mimo-vision", "scripts", "vision.py")
    if not os.path.isfile(VISION_SCRIPT):
        log.warning(f"[vision] vision.py 不存在: {VISION_SCRIPT}")
        return ""
    try:
        r = subprocess.run(
            [sys.executable, VISION_SCRIPT, image_path, "-q", question],
            capture_output=True, text=True, timeout=60,
            cwd=PROJECT_DIR
        )
        if r.returncode == 0 and r.stdout.strip():
            result = r.stdout.strip()
            log.info(f"[vision] 分析成功: {result[:60]}...")
            return result
        err = r.stderr.strip()[:200]
        log.warning(f"[vision] 分析失败: exit={r.returncode} err={err}")
        return ""
    except subprocess.TimeoutExpired:
        log.warning("[vision] 分析超时")
        return ""
    except Exception as e:
        log.warning(f"[vision] 异常: {e}")
        return ""


def _analyze_with_fallback(image_bytes: bytes, question: str, suffix: str = ".jpg") -> str:
    """两级视觉分析: Agnes API 优先 → mimo-vision 子进程回退。

    供 analyze_image_bytes() 与 direct_vision_analyze() 共用（摄像头路径经
    camera.analyze_camera() → analyze_image_bytes() 也走这里）。
    Agnes 抛任何异常（不只是 ImportError）都回退 mimo-vision。
    全部失败返回空串。
    """
    # Stage 1: Agnes vision (进程内 API 调用，无子进程开销)
    try:
        from agent.agnes_vision import agnes_analyze
        result = agnes_analyze(image_bytes, question)
        if result:
            return result
        log.debug("[vision] Agnes vision 返回空，回退 mimo-vision")
    except ImportError:
        log.debug("[vision] agnes_vision 模块不可用，使用 mimo-vision")
    except Exception as e:
        log.warning(f"[vision] Agnes vision 异常，回退 mimo-vision: {e}")

    # Stage 2: mimo-vision 子进程回退
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(image_bytes)
            tmp_path = tmp.name
        try:
            return _run_vision_script(tmp_path, question)
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    except Exception as e:
        log.warning(f"[vision] 临时文件写入失败: {e}")
        return ""


def analyze_image_bytes(image_bytes: bytes, question: str, suffix: str = ".jpg") -> str:
    """分析内存中的图片字节（摄像头帧/上传图片共用入口）。失败返回空串。"""
    return _analyze_with_fallback(image_bytes, question, suffix)


# 摄像头快路径关键词（handler 内再匹配具体源名）
_CAMERA_KEYWORDS = ('摄像头', '镜头', '门口', '门外', '看看外面', '外面有什么',
                    '看门口', '门铃', '监控', '看看我', '我前面', '眼前')


# 会话 → 最近一次分析的摄像头源（追问"那树是什么颜色"时沿用上次源）
_last_cam_source: dict[str, str] = {}


def direct_camera_vision_analyze(text: str, session_id: str = "default") -> str:
    """摄像头视觉快路径: 关键词命中 → 抓帧 → VLM → 返回文本（TTS 播报由上层负责）。

    源选择优先级: 文本中的源名 > 会话上次用的源（多轮追问） > 第一个源。
    未配置摄像头源时返回空串（上层回退 LLM 正常对话）。
    """
    try:
        from app import camera
    except Exception as e:
        log.warning(f"[vision] camera 模块不可用: {e}")
        return ""
    srcs = camera._get_sources()
    if not srcs:
        log.info("[camera] 未配置摄像头源(CAMERA_SOURCES)，跳过")
        return ""
    # 文本匹配具体源名（最长优先，避免"后门"误配"门"）
    name = None
    for src_name in sorted(srcs, key=len, reverse=True):
        if src_name in text:
            name = src_name
            break
    if name is None:
        # 无源名 → 多轮追问沿用会话上次分析的源
        name = _last_cam_source.get(session_id)
        if name:
            log.info(f"[camera] 追问沿用会话上次源: {name}")
    result = _camera_analyze_by_name(text, name) or "摄像头连着，但视觉服务暂时不可用，稍后再试。"
    if name:
        if len(_last_cam_source) > 100:  # 会话记忆上限，防长期运行无限增长
            _last_cam_source.pop(next(iter(_last_cam_source)))
        _last_cam_source[session_id] = name
    return result


def _camera_analyze_by_name(text: str, name: str | None) -> str:
    """抓指定源并 VLM 分析。抓帧/分析失败返回明确提示（不回退 LLM 防幻觉）。"""
    try:
        from app import camera
    except Exception as e:
        log.warning(f"[vision] camera 模块不可用: {e}")
        return ""
    srcs = camera._get_sources()
    if not srcs:
        return ""
    if name is None or name not in srcs:
        name = next(iter(srcs))
    question = _clean_vision_question(text)
    log.info(f"[camera] 视觉分析: 源={name}, 问题={question}")
    jpeg = camera.grab_frame(name)
    if not jpeg:
        log.warning(f"[camera] 源 {name} 抓帧失败")
        return f"摄像头{name}没有反应，可能离线了。"
    result = camera.analyze_camera(name, question)
    if not result:
        return "摄像头连着，但视觉服务暂时不可用，稍后再试。"
    return result


# 源名接管的排除词/意图词：防止"提醒我看看门口"等定时类请求被摄像头抢走
_CAMERA_EXCLUDE = ('提醒', '闹钟', '定时', '别忘了', '记得')
_CAMERA_INTENT = ('看看', '看下', '看一下', '查看', '瞄', '谁', '什么', '有没有',
                  '吗', '监控', '画面', '镜头', '拍到')


def try_camera_by_source_name(text: str, session_id: str = "default") -> str | None:
    """文本包含摄像头源名时（如"帮我看看门口"命中源"门口"）直接抓帧分析。

    未配置源 / 未命中任何源名（≥2字）返回 None，调用方继续正常流程。
    需同时含视觉意图词（看看/谁/什么…），且不含定时类词（提醒/闹钟…），
    避免"提醒我3分钟后看门口的快递"被摄像头接管导致提醒丢失。
    供 brain() 在 LLM 兜底前调用，兜住不在静态关键词里的源名说法。
    """
    try:
        from app import camera
    except Exception:
        return None
    srcs = camera._get_sources()
    if not srcs:
        return None
    if any(w in text for w in _CAMERA_EXCLUDE):
        return None
    if not any(w in text for w in _CAMERA_INTENT):
        return None
    for src_name in sorted(srcs, key=len, reverse=True):
        if len(src_name) >= 2 and src_name in text:
            result = _camera_analyze_by_name(text, src_name)
            if result:
                _last_cam_source[session_id] = src_name
            return result
    return None


def direct_vision_analyze(text: str) -> str:
    """视觉关键词命中时直接截图+两级分析（Agnes → mimo-vision），绕过 LLM 限流。

    返回分析文本，或面向用户的错误提示（截图失败/服务不可用）。
    """
    question = _clean_vision_question(text)
    screenshot_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            screenshot_path = tmp.name
        if platform.system() == "Darwin":
            subprocess.run(["screencapture", "-x", screenshot_path], timeout=5, check=True)
        else:
            try:
                import mss
                with mss.mss() as sct:
                    sct.shot(output=screenshot_path)
            except Exception:
                if os.name == "nt":
                    subprocess.run(['powershell', '-NoProfile', '-Command',
                        'Add-Type -AssemblyName System.Drawing; [System.Drawing.Screen]::PrimaryScreen.Bounds | ForEach-Object { $b=$_; $bmp=New-Object System.Drawing.Bitmap($b.Width,$b.Height); $g=[System.Drawing.Graphics]::FromImage($bmp); $g.CopyFromScreen($b.Location,[System.Drawing.Point]::Empty,$b.Size); $bmp.Save("' + screenshot_path.replace('\\','/') + '"); $g.Dispose(); $bmp.Dispose() }'],
                        timeout=8, check=True)
                else:
                    subprocess.run(['scrot', screenshot_path], timeout=5, check=True)

        try:
            with open(screenshot_path, "rb") as f:
                img_bytes = f.read()
        except Exception as e:
            log.warning(f"[vision] 截图文件读取失败: {e}")
            return "截取屏幕超时了，请稍后再试。"

        result = _analyze_with_fallback(img_bytes, question, suffix=".png")
        if not result:
            # 分析失败也别回退 LLM（无图只会幻觉），明确告知用户
            return "视觉服务暂时不可用，稍后再试吧。"
        return result
    except subprocess.TimeoutExpired:
        log.warning("[vision] 截图超时")
        return "截取屏幕超时了，请稍后再试。"
    except Exception as e:
        # screencapture 失败常见于 macOS 屏幕录制权限未授予（TCC）
        log.warning(f"[vision] 截图异常: {e}")
        return "我暂时截不了屏幕画面，可能需要在系统设置里授予屏幕录制权限。"
    finally:
        if screenshot_path:
            try:
                os.unlink(screenshot_path)
            except OSError:
                pass
