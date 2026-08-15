"""magic-recipe: AI 做菜推荐 MCP

融合 recipe-app 菜谱库(346道) + 口味画像(wife_profile) + ARK LLM，给做菜建议。
支持:
- 按食材/菜名搜索菜谱
- 查完整做法(本地→AI兜底)
- 今日个性化推荐(基于口味画像)
- 场景推荐(想吃辣的/想吃肉/来点清爽的)
- AI 生成新菜谱

依赖: ~/.charlie/recipe-app/data/ (recipes.json + wife_profile.json + order_history.json)
"""
import os, json, random, re
from datetime import datetime, timedelta
from mcp.server.fastmcp import FastMCP
import logging
log = logging.getLogger("magic")

# 注意: 不在此处 os.chdir() — 它会全局改变工作目录，破坏其他模块的相对路径
try:
    from dotenv import load_dotenv
    _env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    load_dotenv(_env_path)
except ImportError: pass

mcp = FastMCP("magic-recipe")

# ── 数据路径(用户数据目录，跨平台可写) ──
RECIPE_DIR = os.environ.get("CHARLIE_RECIPE_DIR") or os.path.join(
    os.environ.get("ASSISTANT_KID_DATA_DIR") or os.path.expanduser("~"), "charlie", "recipe-app")
DATA_DIR = os.path.join(RECIPE_DIR, "data")
RECIPE_FILE = os.path.join(DATA_DIR, "recipes.json")
PROFILE_FILE = os.path.join(DATA_DIR, "wife_profile.json")
HISTORY_FILE = os.path.join(DATA_DIR, "order_history.json")
try:
    os.makedirs(DATA_DIR, exist_ok=True)
except OSError as e:
    log.warning(f"[recipe] 创建数据目录失败: {e}")


# ── 数据加载(扁平 list, 照 smart_recommend 写法; recipe_core 的 {"recipes":[]} 与真实数据不匹配) ──
def _load_recipes() -> list:
    if os.path.exists(RECIPE_FILE):
        try:
            with open(RECIPE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return []


def _load_profile() -> dict:
    default = {
        "name": "老婆", "created_at": datetime.now().isoformat(),
        "taste": {"spiciness": 0, "saltiness": 3, "sweetness": 2, "sourness": 2},
        "preferences": {"favorite_cuisines": [], "favorite_ingredients": [],
                        "disliked_ingredients": [], "allergies": [], "dietary": []},
        "favorites": [], "dislikes": [],
        "stats": {"total_orders": 0, "last_order_at": None},
    }
    if os.path.exists(PROFILE_FILE):
        try:
            with open(PROFILE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return default


def _load_history() -> dict:
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"orders": []}


def _save_recipes(recipes: list):
    try:
        with open(RECIPE_FILE, "w", encoding="utf-8") as f:
            json.dump(recipes, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ── 格式化 ──
def _format_recipe(r: dict) -> str:
    lines = [f"🍳 {r.get('name', '?')}"]
    if r.get("difficulty") or r.get("time"):
        lines.append(f"难度：{r.get('difficulty', '?')}  预计时间：{r.get('time', '?')}")
    ings = r.get("ingredients", [])
    if ings:
        lines.append(f"食材：{', '.join(ings)}")
    steps = r.get("steps", [])
    if steps:
        lines.append("")
        lines.append("做法：")
        for j, step in enumerate(steps, 1):
            lines.append(f"{j}. {step}")
    return "\n".join(lines)


# ── 菜系关键词 — 模块级常量避免每次调用重建 ──
_CUISINE_KEYWORDS = {
    "川菜": ["麻辣", "豆瓣酱", "花椒", "干辣椒", "红油", "水煮", "鱼香", "宫保", "回锅"],
    "粤菜": ["蚝油", "清蒸", "白切", "煲", "虾饺", "叉烧"],
    "湘菜": ["剁椒", "腊肉", "酸豆角"],
    "东北菜": ["酸菜", "炖", "锅包肉", "地三鲜"],
    "江浙菜": ["糖醋", "红烧", "东坡"],
    "西北菜": ["孜然", "羊肉", "拉面"],
}


# ── 打分(基于口味画像, 移植自 smart_recommend._score_recipe) ──
def _score_recipe(recipe: dict, profile: dict) -> int:
    score = 50
    name = recipe.get("name", "")
    ingredients = recipe.get("ingredients", [])
    pref = profile.get("preferences", {})
    favorites = profile.get("favorites", [])
    dislikes = profile.get("dislikes", [])

    if name in dislikes:
        return -100
    if name in favorites:
        score += 30

    fav_ingredients = pref.get("favorite_ingredients", [])
    for ing in ingredients:
        for fi in fav_ingredients:
            if fi == ing or fi in ing.split():
                score += 15
                break

    disliked_ingredients = pref.get("disliked_ingredients", [])
    for ing in ingredients:
        for di in disliked_ingredients:
            if di == ing or di in ing.split():
                score -= 40
                break

    allergies = pref.get("allergies", [])
    for ing in ingredients:
        for allergy in allergies:
            if allergy == ing or allergy in ing.split():
                return -100

    fav_cuisines = pref.get("favorite_cuisines", [])
    all_text = " ".join(ingredients) + name
    for cuisine in fav_cuisines:
        if cuisine in _CUISINE_KEYWORDS:
            for kw in _CUISINE_KEYWORDS[cuisine]:
                if kw in all_text:
                    score += 10
                    break

    taste = profile.get("taste", {})
    spiciness = taste.get("spiciness", 0)
    spicy_kw = ["辣", "辣椒", "花椒", "麻辣", "红油"]
    if any(kw in all_text for kw in spicy_kw):
        if spiciness >= 4:
            score += 10
        elif spiciness <= 1:
            score -= 20
    sweet_kw = ["糖", "甜", "蜜", "拔丝"]
    if any(kw in all_text for kw in sweet_kw):
        if taste.get("sweetness", 2) >= 4:
            score += 8
        elif taste.get("sweetness", 2) <= 0:
            score -= 15
    sour_kw = ["醋", "酸", "柠檬"]
    if any(kw in all_text for kw in sour_kw):
        if taste.get("sourness", 2) >= 4:
            score += 8
        elif taste.get("sourness", 2) <= 0:
            score -= 10
    return score


# ── LLM (ARK, 照 magic-wardrobe; 放弃 recipe_core 的 FinnA) ──
def _call_ark_llm(system_prompt: str, user_message: str, max_tokens: int = 1024) -> str:
    ark_key = os.getenv("ARK_KEY", "")
    ark_base = os.getenv("ARK_BASE", "https://ark.cn-beijing.volces.com/api/plan/v3")
    ark_model = os.getenv("ARK_MODEL", "ark-code-latest")
    if not ark_key:
        return ""
    try:
        import requests
        r = requests.post(f"{ark_base}/chat/completions",
            headers={"Authorization": f"Bearer {ark_key}", "Content-Type": "application/json"},
            json={
                "model": ark_model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
                "max_tokens": max_tokens,
                "temperature": 0.8,
                "extra_body": {"enable_thinking": False},
            }, timeout=30)
        return r.json().get("choices", [{}])[0].get("message", {}).get("content", "").strip()
    except Exception:
        return ""


def _parse_llm_recipe(text: str) -> dict:
    """从 LLM 返回提取 JSON 菜谱(原样移植自 recipe_core)"""
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        text = m.group(1)
    try:
        data = json.loads(text)
        if all(k in data for k in ["name", "steps", "ingredients"]):
            return data
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            data = json.loads(text[start:end+1])
            if all(k in data for k in ("name", "steps", "ingredients")):
                return data
        except json.JSONDecodeError:
            pass
    return {}


def _split_list(s: str) -> list:
    """逗号/顿号/分号分隔 → list"""
    if not s:
        return []
    return [x.strip() for x in re.split(r"[,，;；、]", s) if x.strip()]


# ════════════════════ MCP 工具 ════════════════════

@mcp.tool()
def search_recipe(keyword: str) -> str:
    log.debug("[search_recipe] 被调用")
    """按菜名或食材搜索菜谱。

    参数:
    - keyword: 菜名(如"可乐鸡翅")或食材(如"番茄")

    例: search_recipe("番茄") → 列出含番茄的菜谱
    """
    recipes = _load_recipes()
    matches = []
    for r in recipes:
        name = r.get("name", "")
        ings = r.get("ingredients", [])
        if keyword.lower() in name.lower() or any(keyword.lower() in i.lower() for i in ings):
            matches.append(r)
    if not matches:
        return f"没找到含「{keyword}」的菜谱"
    lines = [f"找到 {len(matches)} 道相关菜谱："]
    for i, r in enumerate(matches[:20], 1):
        lines.append(f"{i}. {r['name']} ({r.get('difficulty','?')}, {r.get('time','?')})")
        lines.append(f"   食材：{', '.join(r.get('ingredients', []))}")
    return "\n".join(lines)


@mcp.tool()
def get_recipe(name: str) -> str:
    log.debug(f"[recipe] get_recipe(name={name})")
    """查菜谱完整做法(本地346道菜优先，找不到用AI生成)。

    参数:
    - name: 菜名(如"可乐鸡翅")

    例: get_recipe("可乐鸡翅") → 返回完整做法步骤
    """
    recipes = _load_recipes()
    for r in recipes:
        if r.get("name") == name or name.lower() in r.get("name", "").lower():
            return _format_recipe(r)
    # AI 兜底生成
    sys_prompt = """你是一个中餐菜谱专家。根据菜名生成家常菜谱。
    返回 JSON: {"name":"...","ingredients":[...],"steps":[...],"difficulty":"简单/中等/困难","time":"如20分钟"}
    只返回 JSON。"""
    text = _call_ark_llm(sys_prompt, f"菜名：{name}")
    if text:
        recipe = _parse_llm_recipe(text)
        if recipe:
            return f"🤖 AI 生成的「{recipe.get('name', name)}」做法：\n\n" + _format_recipe(recipe)
    return f"没找到「{name}」的做法，AI 生成也失败了"


@mcp.tool()
def list_recipes(category: str = "") -> str:
    log.debug("[list_recipes] 被调用")
    """列出菜谱库(可按类别筛选)。

    参数:
    - category: 类别筛选(可选): 凉菜/热菜/经典荤菜/汤羹/水产海鲜/烘焙甜点/主食/小吃/面食主食/汤羹煲类/西式料理/日韩料理/烧烤小吃/早餐早点/饮品奶茶

    例: list_recipes() → 列全部
        list_recipes("凉菜") → 只看凉菜
    """
    recipes = _load_recipes()
    if category:
        recipes = [r for r in recipes if r.get("category", "") == category]
    if not recipes:
        return f"没有{('「'+category+'」类') if category else ''}菜谱"
    lines = [f"📖 共 {len(recipes)} 道菜谱{('（'+category+'）') if category else ''}："]
    for i, r in enumerate(recipes[:50], 1):
        lines.append(f"{i}. {r['name']} ({r.get('difficulty','?')}, {r.get('time','?')})")
    if len(recipes) > 50:
        lines.append(f"... 还有 {len(recipes)-50} 道，用 search_recipe 搜具体的")
    return "\n".join(lines)


@mcp.tool()
def random_recipe() -> str:
    log.debug("[random_recipe] 被调用")
    """随机推荐一道菜谱。

    例: random_recipe() → 随机来一道
    """
    recipes = _load_recipes()
    if not recipes:
        return "菜谱库是空的"
    return _format_recipe(random.choice(recipes))


@mcp.tool()
def recommend_recipe(ingredients: str = "", cuisine: str = "") -> str:
    log.debug("[recommend_recipe] 被调用")
    """按现有食材推荐一道菜(本地优先，找不到AI生成)。

    参数:
    - ingredients: 现有食材，逗号/顿号分隔(如"番茄,鸡蛋")
    - cuisine: 偏好菜系(可选，如"川菜")

    例: recommend_recipe("番茄,鸡蛋") → 推荐番茄炒蛋
    """
    ings = _split_list(ingredients)
    recipes = _load_recipes()
    # 本地优先: 匹配食材，按命中数排序
    local_matches = []
    for r in recipes:
        if not ings:
            local_matches.append(r)
        else:
            if any(i.lower() in ri.lower() for i in ings for ri in r.get("ingredients", [])):
                local_matches.append(r)
    if local_matches and ings:
        local_matches.sort(
            key=lambda r: sum(1 for i in ings if any(i.lower() in ri.lower() for ri in r.get("ingredients", []))),
            reverse=True)
    if local_matches:
        recipe = local_matches[0]
        src = f"本地菜谱库(匹配食材{sum(1 for i in ings if any(i.lower() in ri.lower() for ri in recipe.get('ingredients', [])))}种)" if ings else "本地菜谱库"
        return f"💡 推荐({src})：\n\n" + _format_recipe(recipe)
    # AI 兜底
    sys_prompt = """你是中餐菜谱专家。根据食材推荐一道家常菜。
    返回 JSON: {"name":"...","ingredients":[...],"steps":[...],"difficulty":"...","time":"..."}
    只返回 JSON。"""
    user_msg = f"食材：{'、'.join(ings)}" if ings else "随便推荐一道家常菜"
    if cuisine:
        user_msg += f"\n偏好：{cuisine}菜系"
    text = _call_ark_llm(sys_prompt, user_msg)
    if text:
        recipe = _parse_llm_recipe(text)
        if recipe:
            return f"🤖 AI 生成(基于食材)：\n\n" + _format_recipe(recipe)
    # 最后兜底: 随机
    return "没找到匹配的，随机来一道：\n\n" + _format_recipe(random.choice(recipes)) if recipes else "菜谱库是空的"


@mcp.tool()
def recommend_daily() -> str:
    log.debug("[recommend_daily] 被调用")
    """今日个性化推荐6道菜(基于口味画像，带推荐理由)。

    例: recommend_daily() → 今日推荐
    """
    recipes = _load_recipes()
    profile = _load_profile()
    history = _load_history()
    fav_ingredients = profile.get("preferences", {}).get("favorite_ingredients", [])

    # 排除7天内点过的
    recently = set()
    cutoff = datetime.now() - timedelta(days=7)
    for order in history.get("orders", []):
        try:
            if datetime.fromisoformat(order["ordered_at"]) > cutoff:
                recently.add(order["dish"])
        except Exception:
            pass

    # 打分排序
    candidates = []
    for r in recipes:
        if r.get("name") in recently:
            continue
        score = _score_recipe(r, profile)
        if score > -50:
            candidates.append((score, r))
    candidates.sort(key=lambda x: x[0], reverse=True)
    top = candidates[:6]
    # 不够用随机补
    if len(top) < 6:
        chosen_names = {c[1]["name"] for c in top}
        remain = [r for r in recipes if r.get("name") not in chosen_names and r.get("name") not in recently]
        random.shuffle(remain)
        for r in remain[:6 - len(top)]:
            top.append((-999, r))

    lines = ["🍳 今日推荐："]
    for score, r in top:
        name = r["name"]
        reasons = []
        if name in profile.get("favorites", []):
            reasons.append("💝 收藏过的")
        if score == 0 or score == -999:
            reasons.append("🆕 换换口味")
        ings_text = " ".join(r.get("ingredients", []))
        if fav_ingredients:
            for fi in fav_ingredients:
                if fi in ings_text:
                    reasons.append(f"有爱吃的{fi}")
                    break
        if score > 80:
            reasons.append("⭐ 强烈推荐")
        elif score > 60:
            reasons.append("👍 应该会喜欢")
        reason_str = "，".join(reasons) if reasons else "常规推荐"
        lines.append(f"• {name} ({r.get('difficulty','?')}, {r.get('time','?')}) — {reason_str}")
    lines.append("")
    lines.append("想看做法告诉我菜名，我调 get_recipe 查详细步骤。")
    return "\n".join(lines)


@mcp.tool()
def recommend_by_context(context: str) -> str:
    """按场景推荐菜谱(如想吃辣的/想吃肉/来点清爽的)。

    参数:
    - context: 场景描述(如"想吃辣的"/"想吃肉"/"来点清爽的"/"下饭的"/"不要辣")

    例: recommend_by_context("想吃辣的") → 推荐辣菜
    """
    recipes = _load_recipes()
    profile = _load_profile()
    ctx = context.lower()

    keyword_map = {
        "清爽": ["凉拌", "清炒", "白灼", "蒸", "凉"],
        "清淡": ["清炒", "白灼", "蒸", "煮", "素"],
        "肉": ["肉", "鸡", "鱼", "虾", "牛", "羊", "排骨", "翅"],
        "辣的": ["辣", "麻辣", "红油", "干辣椒", "豆瓣", "剁椒"],
        "酸的": ["醋", "酸", "柠檬", "番茄"],
        "甜的": ["糖", "甜", "蜜", "拔丝", "可乐"],
        "汤": ["汤", "羹"],
        "快手": ["炒", "煎"],
        "下饭": ["麻婆", "红烧", "鱼香", "宫保", "回锅", "干煸"],
    }
    positive_kw, negative_kw = [], []
    for intent, keywords in keyword_map.items():
        if intent in ctx:
            positive_kw.extend(keywords)
    for no_prefix in ["不要", "不吃", "不想", "别"]:
        idx = ctx.find(no_prefix)
        if idx >= 0:
            negative_kw.append(ctx[idx + len(no_prefix):].strip()[:10])
    if "素菜" in ctx or "素食" in ctx:
        negative_kw.extend(["肉", "鸡", "鱼", "虾", "牛", "羊", "排骨", "翅", "腿"])
    if "无辣不欢" in ctx or "越辣越好" in ctx:
        positive_kw.extend(["麻辣", "红油", "干辣椒", "剁椒", "泡椒"])

    candidates = []
    for r in recipes:
        full_text = r.get("name", "") + " ".join(r.get("ingredients", []))
        score = 50
        if positive_kw:
            score += sum(15 for kw in positive_kw if kw in full_text)
        if negative_kw and any(kw in full_text for kw in negative_kw):
            score -= 60
        score += _score_recipe(r, profile) - 50
        if score > -50:
            candidates.append((min(score, 120), r))
    candidates.sort(key=lambda x: x[0], reverse=True)

    if not candidates:
        return f"没找到匹配「{context}」的菜"
    lines = [f"🍳 「{context}」推荐："]
    for score, r in candidates[:8]:
        lines.append(f"• {r['name']} ({r.get('difficulty','?')}, {r.get('time','?')})")
    lines.append("")
    lines.append("想看做法告诉我菜名。")
    return "\n".join(lines)


@mcp.tool()
def add_recipe(name: str, ingredients: str, steps: str,
               difficulty: str = "简单", time: str = "") -> str:
    log.debug("[add_recipe] 被调用")
    """添加新菜谱到菜谱库。

    参数:
    - name: 菜名
    - ingredients: 食材，逗号/顿号分隔(如"番茄,鸡蛋,盐")
    - steps: 步骤，分号或换行分隔(如"番茄切块;鸡蛋打散;热油翻炒")
    - difficulty: 难度(简单/中等/困难)，默认简单
    - time: 预计时间(如"15分钟")

    例: add_recipe("我的炒饭", "米饭,鸡蛋,葱花", "打散鸡蛋;热油炒饭;加蛋翻炒")
    """
    recipes = _load_recipes()
    for r in recipes:
        if r.get("name") == name:
            return f"「{name}」菜谱已存在"
    recipe = {
        "name": name,
        "ingredients": _split_list(ingredients),
        "steps": [s.strip() for s in re.split(r"[;\n；]", steps) if s.strip()],
        "difficulty": difficulty,
        "time": time or "未知",
    }
    recipes.append(recipe)
    _save_recipes(recipes)
    return f"✅ 已添加菜谱：{name}"


@mcp.tool()
def generate_recipe(name: str = "", ingredients: str = "") -> str:
    log.debug("[generate_recipe] 被调用")
    """用AI生成一道新菜谱(本地没有时用)。

    参数:
    - name: 菜名(可选，如"酸辣土豆丝")
    - ingredients: 食材，逗号分隔(可选，如"土豆,醋,辣椒")

    例: generate_recipe("蒜蓉粉丝蒸虾") → AI生成做法
    """
    sys_prompt = """你是一个中餐菜谱专家。根据菜名和/或食材生成家常菜谱。
    返回 JSON: {"name":"...","ingredients":[...],"steps":[...],"difficulty":"简单/中等/困难","time":"如20分钟"}
    只返回 JSON。"""
    ings = _split_list(ingredients)
    if name and not ings:
        user_msg = f"菜名：{name}"
    else:
        user_msg = f"菜名：{name or '家常菜'}，食材：{'、'.join(ings)}"
    text = _call_ark_llm(sys_prompt, user_msg)
    if not text:
        return "AI 生成失败，请稍后再试"
    recipe = _parse_llm_recipe(text)
    if recipe:
        return f"🤖 AI 生成的「{recipe.get('name', name)}」做法：\n\n" + _format_recipe(recipe)
    return f"🤖 AI 生成的菜谱：\n\n{text}"


if __name__ == "__main__":
    mcp.run()
