"""Collect Chinese recipes from HowToCook and other sources."""
import os, json, re, requests, time

DATA_DIR = "/Users/sxliuyu/orca/projects/charlie/data/charlie/recipe-app/data"
OUTPUT_FILE = os.path.join(DATA_DIR, "recipes.json")
os.makedirs(DATA_DIR, exist_ok=True)

# Load existing recipes
existing = []
if os.path.exists(OUTPUT_FILE):
    try:
        with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
            existing = json.load(f)
    except:
        pass
existing_names = {r["name"] for r in existing}
print(f"Existing recipes: {len(existing)}")

# Category mapping
CAT_MAP = {
    "meat_dish": "经典荤菜",
    "vegetable_dish": "热菜",
    "aquatic": "水产海鲜",
    "staple": "主食",
    "soup": "汤羹",
    "dessert": "烘焙甜点",
    "breakfast": "早餐早点",
    "drink": "饮品奶茶",
    "condiment": "凉菜",
    "semi-finished": "小吃",
}

def parse_recipe_md(content, name):
    """Parse HowToCook markdown format to extract recipe data."""
    recipe = {
        "name": name,
        "category": "热菜",
        "difficulty": "中等",
        "time": "30分钟",
        "ingredients": [],
        "steps": [],
        "taste": {"spiciness": 0, "sweetness": 0, "sourness": 0},
    }
    
    # Extract ingredients
    ing_pattern = re.compile(r'(?:##|###)\s*(?:食材|原料|材料|所需食材|准备食材|用料).*?\n(.*?)(?:\n(?:##|###)|$)', re.DOTALL)
    ing_match = ing_pattern.search(content)
    if ing_match:
        ing_text = ing_match.group(1)
        for line in ing_text.split("\n"):
            line = line.strip()
            if line.startswith(("*", "-", "+")):
                line = line[1:].strip()
            elif re.match(r'^\d+\.', line):
                line = re.sub(r'^\d+\.\s*', '', line)
            if not line or len(line) > 50:
                continue
            line = re.sub(r'\*\*([^*]+)\*\*', r'\1', line)
            line = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', line)
            ing_name = re.sub(r'[：:]\s*.*', '', line).strip()
            ing_name = re.sub(r'\d+.*$', '', ing_name).strip()
            ing_name = re.sub(r'[（(][^)）]*[)）]', '', ing_name).strip()
            if ing_name and len(ing_name) >= 2 and len(ing_name) <= 20:
                recipe["ingredients"].append(ing_name)
    
    # Extract steps
    step_pattern = re.compile(r'(?:##|###)\s*(?:步骤|做法|制作|操作步骤|烹饪步骤).*?\n(.*?)(?:\n(?:##|###)|$)', re.DOTALL)
    step_match = step_pattern.search(content)
    if step_match:
        step_text = step_match.group(1)
        for line in step_text.split("\n"):
            line = line.strip()
            if re.match(r'^\d+[\.、）)]', line):
                line = re.sub(r'^\d+[\.、）)]\s*', '', line)
            elif line.startswith(("*", "-", "+")):
                line = line[1:].strip()
            if not line or len(line) < 5:
                continue
            line = re.sub(r'\*\*([^*]+)\*\*', r'\1', line)
            line = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', line)
            if len(line) > 5:
                recipe["steps"].append(line[:100])
    
    # Estimate time
    time_match = re.search(r'(?:预计|烹饪|制作|总)?(?:耗时|用时|时间)[:：]?\s*(\d+)\s*(?:分钟|分)', content)
    if time_match:
        recipe["time"] = f"{time_match.group(1)}分钟"
    
    # Estimate difficulty
    if re.search(r'(?:简单|容易|快速|快手)', content):
        recipe["difficulty"] = "简单"
    elif re.search(r'(?:困难|复杂|繁琐|考究)', content):
        recipe["difficulty"] = "困难"
    
    # Taste detection
    if re.search(r'(?:辣|辣椒|麻辣|红油|豆瓣|剁椒)', content):
        recipe["taste"]["spiciness"] = 3
    if re.search(r'(?:糖|甜|拔丝|可乐|蜜)', content):
        recipe["taste"]["sweetness"] = 2
    if re.search(r'(?:醋|酸|柠檬|番茄)', content):
        recipe["taste"]["sourness"] = 2
    
    return recipe

def download_howtocook():
    """Download recipes from Anduin2017/HowToCook."""
    base_url = "https://raw.githubusercontent.com/Anduin2017/HowToCook/master"
    
    # Get file list
    r = requests.get("https://api.github.com/repos/Anduin2017/HowToCook/git/trees/master?recursive=1", timeout=15)
    if r.status_code != 200:
        print(f"GitHub API failed: {r.status_code}")
        return []
    
    trees = r.json().get("tree", [])
    recipe_files = [t for t in trees if t["path"].startswith("dishes/") and 
                    t["path"].endswith(".md") and t["path"].count("/") >= 2
                    and "/template/" not in t["path"]]
    
    print(f"Found {len(recipe_files)} recipe files")
    
    recipes = []
    total = len(recipe_files)
    for i, item in enumerate(recipe_files):
        path = item["path"]
        category_key = path.split("/")[1]
        name = path.split("/")[-1].replace(".md", "")
        category = CAT_MAP.get(category_key, "热菜")
        
        if name in existing_names:
            continue
        
        try:
            url = f"{base_url}/{path}"
            r = requests.get(url, timeout=10)
            if r.status_code == 200:
                recipe = parse_recipe_md(r.text, name)
                recipe["category"] = category
                if recipe["ingredients"] and recipe["steps"]:
                    recipes.append(recipe)
        except Exception as e:
            pass
        
        if (i + 1) % 50 == 0:
            print(f"  Progress: {i+1}/{total}, collected {len(recipes)} new")
        time.sleep(0.1)
    
    return recipes

# Main
print("Downloading from HowToCook...")
new_recipes = download_howtocook()

# Merge
all_recipes = existing + new_recipes
print(f"\nTotal: {len(all_recipes)} recipes ({len(new_recipes)} new)")

# Save
with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
    json.dump(all_recipes, f, ensure_ascii=False, indent=2)
print(f"Saved to {OUTPUT_FILE}")
