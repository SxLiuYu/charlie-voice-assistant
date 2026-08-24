"""衣橱照片识别：上传衣服照片 → 分析颜色/类型 → 录入衣橱"""
import os, io, json, base64, tempfile, logging, colorsys, re, datetime, asyncio
from collections import Counter
from fastapi import APIRouter, UploadFile, File, HTTPException
from PIL import Image
import requests

log = logging.getLogger("magic")

router = APIRouter(prefix="/api/wardrobe", tags=["wardrobe"])

WARDROBE_FILE = os.path.join(
    os.environ.get("ASSISTANT_KID_DATA_DIR", os.path.expanduser("~")),
    "charlie", "wardrobe", "data", "wardrobe.json"
)

# 颜色名称映射（HSV → 中文颜色名）
def get_dominant_colors(image: Image.Image, n: int = 3) -> list:
    """提取图片主色调"""
    img = image.copy()
    img = img.resize((100, 100))
    img = img.convert("RGB")
    # 采样像素
    pixels = list(img.getdata())
    # 简单量化：按色相分组
    from collections import Counter
    
    color_names = {
        (0, 20): "红色", (20, 40): "橙色", (40, 70): "黄色",
        (70, 160): "绿色", (160, 200): "青色", (200, 240): "蓝色",
        (240, 280): "紫色", (280, 330): "粉色", (330, 360): "红色",
    }
    
    def rgb_to_hue(r, g, b):
        h, s, v = colorsys.rgb_to_hsv(r/255, g/255, b/255)
        return h * 360, s, v
    
    color_counts = Counter()
    for r, g, b in pixels[:500]:  # 采样500个像素
        h, s, v = rgb_to_hue(r, g, b)
        if v < 0.15:  # 太暗 → 黑色
            color_counts["黑色"] += 1
        elif s < 0.1 and v > 0.9:  # 低饱和度 → 白色
            color_counts["白色"] += 1
        elif s < 0.15 and v > 0.5:  # 灰色
            color_counts["灰色"] += 1
        else:
            for (lo, hi), name in color_names.items():
                if lo <= h < hi:
                    color_counts[name] += 1
                    break
            else:
                color_counts["其他"] += 1
    
    # 卡其色/棕色/米色 特殊处理
    brownish = 0
    for r, g, b in pixels[:500]:
        if r > g > b and r - b > 30:
            brownish += 1
    if brownish > 50:
        color_counts["棕色"] += brownish
        color_counts["卡其色"] = color_counts.get("卡其色", 0) + brownish // 2
    
    return [c for c, _ in color_counts.most_common(n)]

async def classify_clothing(colors: list, image_size: tuple) -> dict:
    """基于颜色和尺寸特征推断衣物类型"""
    w, h = image_size
    aspect = w / h if h > 0 else 1
    primary_color = colors[0] if colors else "未知"
    color_str = "、".join(colors[:3])

    prompt = f"""分析这张衣服照片的特征，推断衣物属性。返回JSON格式。

照片特征:
- 主色调: {color_str}
- 图片宽高比: {aspect:.2f} (宽{w}px, 高{h}px)
- 宽高比>1.2可能为鞋子/外套, 0.7-1.2可能为上装/裙子, <0.7可能为裤子/连衣裙

请推断并返回JSON:
{{"name": "衣物名称(如:白色短袖T恤)", "category": "上装/下装/外套/裙子/鞋子/配饰", "color": "颜色", "style": "正式/休闲/运动/约会/日常", "warmth": "薄/适中/厚"}}

只返回JSON。"""

    def _sync_post():
        from dotenv import load_dotenv
        load_dotenv()
        ark_key = os.getenv("ARK_KEY", "")
        ark_base = os.getenv("ARK_BASE", "")
        ark_model = os.getenv("ARK_MODEL", "u2")

        r = requests.post(
            f"{ark_base}/chat/completions",
            headers={"Authorization": f"Bearer {ark_key}", "Content-Type": "application/json"},
            json={"model": ark_model, "messages": [{"role": "user", "content": prompt}],
                  "max_tokens": 800, "temperature": 0.3},
            timeout=20,
        )
        return r

    try:
        r = await asyncio.to_thread(_sync_post)

        msg = r.json().get("choices", [{}])[0].get("message", {})
        reply = msg.get("content", "").strip()
        if not reply:
            reply = msg.get("reasoning_content", "").strip()
        if not reply:
            return None

        m = re.search(r'\{[^}]+\}', reply, re.DOTALL)
        if m:
            result = json.loads(m.group(0))
        else:
            result = json.loads(reply)

        return result
    except Exception as e:
        log.warning(f"[wardrobe] 衣物分类失败: {e}")
        return None

def load_wardrobe():
    if os.path.exists(WARDROBE_FILE):
        with open(WARDROBE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []

def save_wardrobe(items):
    os.makedirs(os.path.dirname(WARDROBE_FILE), exist_ok=True)
    with open(WARDROBE_FILE, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)

@router.post("/photo")
async def wardrobe_photo(file: UploadFile = File(...)):
    """上传衣服照片，自动识别并录入衣橱"""
    # 验证文件类型
    ext = (file.filename or "").rsplit(".", 1)[-1].lower()
    if ext not in ("jpg", "jpeg", "png", "webp", "heic", "bmp"):
        raise HTTPException(400, f"不支持的图片格式: {ext}")
    
    data = await file.read()
    if len(data) > 10 * 1024 * 1024:  # 10MB
        raise HTTPException(413, "图片过大(上限10MB)")
    
    try:
        img = Image.open(io.BytesIO(data))
        img = img.convert("RGB")
    except Exception:
        raise HTTPException(400, "无法解析图片，请确认是有效的照片")
    
    # 提取颜色
    colors = get_dominant_colors(img, n=3)
    log.info(f"[wardrobe] 照片分析: 色调={colors}, 尺寸={img.size}")
    
    # 分类
    result = classify_clothing(colors, img.size)
    if not result:
        raise HTTPException(500, "衣物识别失败, 请重试")
    
    # 添加到衣橱
    wardrobe = load_wardrobe()
    import datetime
    new_item = {
        "id": f"cloth_{len(wardrobe)+1}_{datetime.datetime.now().strftime('%m%d%H%M')}",
        "name": result.get("name", "未命名衣物"),
        "category": result.get("category", "上装"),
        "color": result.get("color", colors[0] if colors else "未知"),
        "style": result.get("style", "休闲"),
        "warmth": result.get("warmth", "适中"),
        "added_at": datetime.datetime.now().isoformat(),
        "from_photo": True,
    }
    wardrobe.append(new_item)
    save_wardrobe(wardrobe)
    
    log.info(f"[wardrobe] 照片录入: {new_item['name']} ({new_item['category']}, {new_item['color']})")
    
    return {
        "ok": True,
        "item": new_item,
        "colors_detected": colors,
        "wardrobe_total": len(wardrobe),
        "message": f"已识别并添加: {new_item['name']}",
    }
