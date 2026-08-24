"""magic-notes: 备忘录 (2个工具)"""
# --- MCP 元数据（供 mcp_registry 自动发现，用 ast.parse 读取，不执行文件）---
__mcp_meta__ = {
    "name": "magic-notes",
    "tier": "core",
    "required_env": [],
    "label": "备忘录"
}

from mcp.server.fastmcp import FastMCP
import os
import logging
log = logging.getLogger("magic")
mcp = FastMCP("magic-notes")


@mcp.tool()
def save_note(title: str = "", content: str = "") -> str:
    log.info(f"[notes] save_note(title={title})")
    """保存语音备忘录。title=标题(可选，自动生成), content=内容

    例: save_note("明天带身份证") → 保存到备忘录目录
        save_note("购物清单", "牛奶、面包、鸡蛋") → 保存购物清单
    """
    try:
        from datetime import datetime
        notes_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "notes")
        os.makedirs(notes_dir, exist_ok=True)

        if not title:
            now = datetime.now()
            title = f"备忘录_{now.strftime('%Y%m%d_%H%M')}"
        # 防止路径遍历：替换所有危险字符
        safe_title = title.replace('/', '_').replace('\\', '_').replace('..', '_').replace(' ', '_').replace('\x00', '')
        filename = f"{safe_title}.md"
        filepath = os.path.join(notes_dir, filename)

        now = datetime.now()
        full = f"# {title}\n\n{content}\n\n_记录于 {now.strftime('%Y-%m-%d %H:%M')}_\n"
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(full)
        return f"已保存备忘录：{title} → {filename}"
    except Exception as e:
        return f"保存备忘录失败: {e}"


@mcp.tool()
def list_notes() -> str:
    log.debug("[list_notes] 被调用")
    """列出所有语音备忘录

    例: list_notes() → 列出所有备忘录
    """
    try:
        notes_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "notes")
        if not os.path.exists(notes_dir):
            return "还没有备忘录。"
        files = [f for f in os.listdir(notes_dir) if f.endswith(".md")]
        if not files:
            return "还没有备忘录。"
        files.sort(reverse=True)
        lines = [f"共 {len(files)} 条备忘录："]
        for f in files[:20]:
            size = os.path.getsize(os.path.join(notes_dir, f))
            lines.append(f"• {f.replace('.md','')} ({size}字节)")
        return "\n".join(lines)
    except Exception as e:
        return f"列出备忘录失败: {e}"




@mcp.tool()
def add_shopping_item(item: str, quantity: str = "") -> str:
    """添加商品到购物清单。item=商品名, quantity=数量(可选)

    例: add_shopping_item("牛奶", "2箱") → 购物清单添加牛奶2箱
        add_shopping_item("鸡蛋") → 购物清单添加鸡蛋
    """
    try:
        from datetime import datetime
        shopping_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "notes", "shopping_list.md")
        os.makedirs(os.path.dirname(shopping_file), exist_ok=True)
        # 读取现有清单
        existing = ""
        if os.path.exists(shopping_file):
            with open(shopping_file, 'r', encoding='utf-8') as f:
                existing = f.read()
        # 添加新条目
        entry = f"- [ ] {item} {quantity}".strip()
        if existing:
            lines = existing.split('\n')
            lines.append(entry)
            with open(shopping_file, 'w', encoding='utf-8') as f:
                f.write('\n'.join(lines))
        else:
            with open(shopping_file, 'w', encoding='utf-8') as f:
                f.write(f"# 购物清单\n\n{entry}\n")
        return f"已添加到购物清单：{item} {quantity}"
    except Exception as e:
        return f"添加购物清单失败: {e}"


@mcp.tool()
def list_shopping_items() -> str:
    """查看购物清单

    例: list_shopping_items() → 列出所有待买商品
    """
    try:
        shopping_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "notes", "shopping_list.md")
        if not os.path.exists(shopping_file):
            return "购物清单是空的。"
        with open(shopping_file, 'r', encoding='utf-8') as f:
            content = f.read()
        lines = [l for l in content.split('\n') if l.strip().startswith('-')]
        if not lines:
            return "购物清单是空的。"
        return f"购物清单（{len(lines)}项）：\n" + "\n".join(lines)
    except Exception as e:
        return f"查看购物清单失败: {e}"


@mcp.tool()
def clear_shopping_list() -> str:
    """清空购物清单

    例: clear_shopping_list() → 清空购物清单
    """
    try:
        shopping_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "notes", "shopping_list.md")
        with open(shopping_file, 'w', encoding='utf-8') as f:
            f.write("# 购物清单\n\n")
        return "购物清单已清空。"
    except Exception as e:
        return f"清空购物清单失败: {e}"


if __name__ == "__main__":
    mcp.run()
