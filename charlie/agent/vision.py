"""Vision fast path: screenshot + Mimo vision API analysis, bypassing LLM.

Extracted from voice_agent.py.
"""
import os, sys, re, platform, subprocess, tempfile, logging

log = logging.getLogger("magic")

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))


def direct_vision_analyze(text: str) -> str:
    """视觉关键词命中时直接截图+调用 vision.py，绕过 LLM，避免 429 限流。
    返回视觉分析结果文本，或空字符串(失败时)。
    """
    VISION_SCRIPT = os.path.join(PROJECT_DIR, "skills", "mimo-vision", "scripts", "vision.py")
    if not os.path.isfile(VISION_SCRIPT):
        log.warning(f"[vision] vision.py 不存在: {VISION_SCRIPT}")
        return ""
    question = re.sub(
        r'^(看看|帮我看|帮我看看|截图分析|识别|分析一下|描述|描述一下|'
        r'屏幕上|屏幕|截屏|截图|帮我识别|帮我描述|帮我分析|'
        r'看一下|看下|看看看|看看)',
        '', text
    ).strip().rstrip('。.,，！!？?')
    if not question or len(question) < 2:
        question = "描述这张图片的内容"
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
        r = subprocess.run(
            [sys.executable, VISION_SCRIPT, screenshot_path, "-q", question],
            capture_output=True, text=True, timeout=60,
            cwd=PROJECT_DIR
        )
        try:
            os.unlink(screenshot_path)
        except OSError:
            pass
        if r.returncode == 0 and r.stdout.strip():
            result = r.stdout.strip()
            log.info(f"[vision] 分析成功: {result[:60]}...")
            return result
        else:
            err = r.stderr.strip()[:200]
            log.warning(f"[vision] 分析失败: exit={r.returncode} err={err}")
            return ""
    except subprocess.TimeoutExpired:
        log.warning("[vision] 截图或分析超时")
        return ""
    except Exception as e:
        log.warning(f"[vision] 异常: {e}")
        return ""
