"""magic-notes: 备忘录 (2个工具)"""
from mcp.server.fastmcp import FastMCP
import os
mcp = FastMCP("magic-notes")


@mcp.tool()
def save_note(title: str = "", content: str = "") -> str:
    """保存语音备忘录。title=标题(可选，自动生成), content=内容

    例: save_note("明天带身份证") → 保存到备忘录目录
    """
    from datetime import datetime
    notes_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "notes")
    os.makedirs(notes_dir, exist_ok=True)

    if not title:
        now = datetime.now()
        title = f"备忘录_{now.strftime('%Y%m%d_%H%M')}"
    filename = f"{title.replace('/', '_').replace(' ', '_')}.md"
    filepath = os.path.join(notes_dir, filename)

    now = datetime.now()
    full = f"# {title}\n\n{content}\n\n_记录于 {now.strftime('%Y-%m-%d %H:%M')}_\n"
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(full)

    return f"已保存备忘录：{title} → {filename}"


@mcp.tool()
def list_notes() -> str:
    """列出所有语音备忘录"""
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


if __name__ == "__main__":
    mcp.run()
