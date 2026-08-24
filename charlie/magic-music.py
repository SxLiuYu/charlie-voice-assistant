"""magic-music: 网易云音乐播放 (6个工具)"""
# --- MCP 元数据（供 mcp_registry 自动发现，用 ast.parse 读取，不执行文件）---
__mcp_meta__ = {
    "name": "magic-music",
    "tier": "optional",
    "required_env": [],
    "label": "音乐播放"
}

from mcp.server.fastmcp import FastMCP
from mcp_common import NCM_BIN, _ensure_https
import subprocess, json as _json, random
import logging
log = logging.getLogger("magic")

mcp = FastMCP("magic-music")


@mcp.tool()
def search_music(keyword: str, limit: int = 5) -> str:
    log.debug(f"[music] search_music(query={query})")
    """搜索网易云音乐歌曲。keyword=歌名或歌手名, limit=返回条数(默认5)

    例: search_music("周杰伦 晴天") → 搜索周杰伦的晴天
        search_music("邓紫棋") → 搜索邓紫棋的歌曲
    """
    try:
        r = subprocess.run([NCM_BIN, "search", "song", keyword, "--limit", str(limit), "--json"],
                          capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            return f"搜索失败: {r.stderr.strip() or '未知错误'}"
        data = _json.loads(r.stdout)
        songs = data.get("result", {}).get("songs", [])
        if not songs:
            return f"没搜到'{keyword}'相关的歌"
        lines = [f"搜到{len(songs)}首歌："]
        for s in songs:
            ars = "/".join(a.get("name", "") for a in s.get("ar", []))
            lines.append(f"• [{s['id']}] {s['name']} - {ars}")
        return "\n".join(lines)
    except Exception as e:
        return f"搜索音乐失败: {e}"


@mcp.tool()
def play_music(keyword: str = "", song_id: int = 0) -> str:
    log.info(f"[music] play_music(query={query})")
    """播放音乐。keyword=歌名或歌手名(搜索后播放第一首), song_id=歌曲ID(直接播放)

    例: play_music("周杰伦 晴天") → 搜索并播放周杰伦的晴天
        play_music(song_id=186016) → 直接播放指定歌曲
    """
    import os as _os
    try:
        if not song_id and keyword:
            r = subprocess.run([NCM_BIN, "search", "song", keyword, "--limit", "1", "--json"],
                              capture_output=True, text=True, timeout=15)
            if r.returncode != 0:
                return f"搜索失败: {r.stderr.strip()}"
            data = _json.loads(r.stdout)
            songs = data.get("result", {}).get("songs", [])
            if not songs:
                return f"没搜到'{keyword}'"
            song_id = songs[0]["id"]
            song_name = songs[0]["name"]
            ars = "/".join(a.get("name", "") for a in songs[0].get("ar", []))
        elif song_id:
            r = subprocess.run([NCM_BIN, "song", str(song_id), "--json"],
                              capture_output=True, text=True, timeout=15)
            if r.returncode == 0:
                data = _json.loads(r.stdout)
                songs = data.get("songs", [])
                if songs:
                    song_name = songs[0].get("name", str(song_id))
                    ars = "/".join(a.get("name", "") for a in songs[0].get("ar", []))
                else:
                    song_name, ars = str(song_id), ""
            else:
                song_name, ars = str(song_id), ""
        else:
            return "请说歌名或歌曲ID"

        r = subprocess.run([NCM_BIN, "url", str(song_id), "--level", "exhigh", "--json"],
                          capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            return f"获取播放地址失败: {r.stderr.strip()}"
        url_data = _json.loads(r.stdout)
        _d = url_data.get("data", [])
        url = _d[0].get("url", "") if isinstance(_d, list) and _d else (_d.get("url", "") if isinstance(_d, dict) else url_data.get("url", ""))
        if not url:
            return f"'{song_name}'暂时无法播放（版权限制或需要VIP）"

        url = _ensure_https(url)
        return f"__MUSIC__{url}__{song_name}__{ars}"
    except Exception as e:
        return f"播放音乐失败: {e}"


@mcp.tool()
def play_random_music() -> str:
    """随机播放一首歌。从每日推荐歌曲中随机选一首播放。

    例: play_random_music() → 随机播放一首推荐歌曲
    """
    try:
        r = subprocess.run([NCM_BIN, "recommend", "songs", "--json"],
                          capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            r = subprocess.run([NCM_BIN, "record", "--json", "--limit", "30"],
                              capture_output=True, text=True, timeout=15)
            if r.returncode != 0:
                return f"获取推荐歌曲失败: {r.stderr.strip()}"
            data = _json.loads(r.stdout)
            songs = data.get("data", []) or data.get("record", [])
        else:
            data = _json.loads(r.stdout)
            songs = data.get("recommend", [])

        if not songs:
            return "暂时没有推荐歌曲"

        song = random.choice(songs)
        song_id = song["id"]
        song_name = song.get("name", "")
        ars = "/".join(a.get("name", "") for a in song.get("ar", []))

        r = subprocess.run([NCM_BIN, "url", str(song_id), "--level", "exhigh", "--json"],
                          capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            return f"获取播放地址失败: {r.stderr.strip()}"
        url_data = _json.loads(r.stdout)
        _d = url_data.get("data", [])
        url = _d[0].get("url", "") if isinstance(_d, list) and _d else (_d.get("url", "") if isinstance(_d, dict) else url_data.get("url", ""))
        if not url:
            return f"'{song_name}'暂时无法播放（版权限制）"

        url = _ensure_https(url)
        return f"__MUSIC__{url}__{song_name}__{ars}"
    except Exception as e:
        return f"随机播放失败: {e}"


@mcp.tool()
def stop_music() -> str:
    """停止当前正在播放的音乐"""
    return "__MUSIC_STOP__"


@mcp.tool()
def list_playlists() -> str:
    """列出你的网易云音乐歌单"""
    try:
        r = subprocess.run([NCM_BIN, "playlist", "list", "--json"],
                          capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            return f"获取歌单失败: {r.stderr.strip()}"
        data = _json.loads(r.stdout)
        playlists = data.get("playlist", [])
        if not playlists:
            return "没有歌单"
        lines = [f"共{len(playlists)}个歌单："]
        for p in playlists[:10]:
            lines.append(f"• [{p['id']}] {p['name']} ({p.get('trackCount', 0)}首)")
        return "\n".join(lines)
    except Exception as e:
        return f"获取歌单失败: {e}"


@mcp.tool()
def play_playlist(playlist_id: int = 0) -> str:
    """播放歌单。playlist_id=歌单ID(不传则播放每日推荐)"""
    try:
        if not playlist_id:
            r = subprocess.run([NCM_BIN, "recommend", "songs", "--limit", "1", "--json"],
                              capture_output=True, text=True, timeout=15)
            if r.returncode != 0:
                return f"获取每日推荐失败: {r.stderr.strip()}"
            data = _json.loads(r.stdout)
            songs = data.get("data", {}).get("dailySongs", [])
            if not songs:
                return "暂无每日推荐"
            song = songs[0]
            song_id = song["id"]
            song_name = song.get("name", "")
            ars = "/".join(a.get("name", "") for a in song.get("ar", []))
        else:
            r = subprocess.run([NCM_BIN, "playlist", "show", str(playlist_id), "--limit", "1", "--json"],
                              capture_output=True, text=True, timeout=15)
            if r.returncode != 0:
                return f"获取歌单失败: {r.stderr.strip()}"
            data = _json.loads(r.stdout)
            tracks = data.get("tracks", data.get("songs", []))
            if not tracks:
                return "歌单为空"
            song = tracks[0]
            song_id = song["id"]
            song_name = song.get("name", "")
            ars = "/".join(a.get("name", "") for a in song.get("ar", []))

        r = subprocess.run([NCM_BIN, "url", str(song_id), "--level", "exhigh", "--json"],
                          capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            return f"获取播放地址失败: {r.stderr.strip()}"
        url_data = _json.loads(r.stdout)
        _d = url_data.get("data", [])
        url = _d[0].get("url", "") if isinstance(_d, list) and _d else (_d.get("url", "") if isinstance(_d, dict) else url_data.get("url", ""))
        if not url:
            return f"'{song_name}'暂时无法播放（版权限制）"

        url = _ensure_https(url)
        return f"__MUSIC__{url}__{song_name}__{ars}"
    except Exception as e:
        return f"播放歌单失败: {e}"




@mcp.tool()
def play_white_noise(type: str = "rain") -> str:
    """播放白噪音/环境音，用于助眠、专注、放松。
    type: rain(雨声)/wind(风声)/ocean(海浪)/fire(壁炉)/forest(森林)/night(虫鸣)

    例: play_white_noise("rain") → 播放雨声助眠
        play_white_noise("ocean") → 播放海浪声放松
        play_white_noise() → 默认播放雨声
    """
    import subprocess, os as _os
    # 使用在线白噪音音频流
    urls = {
        "rain": "https://www.youtube.com/watch?v=mPZkdNFkNps",
        "wind": "https://www.youtube.com/watch?v=0WZ6U5m-vsM",
        "ocean": "https://www.youtube.com/watch?v=bScd0hISJ1w",
        "fire": "https://www.youtube.com/watch?v=ZiYkM-JqXMY",
        "forest": "https://www.youtube.com/watch?v=G2sQkTYhB0w",
        "night": "https://www.youtube.com/watch?v=ZcJjMn3m2B8",
    }
    name_map = {"rain": "雨声", "wind": "风声", "ocean": "海浪", "fire": "壁炉", "forest": "森林", "night": "虫鸣"}
    url = urls.get(type, urls["rain"])
    name = name_map.get(type, "白噪音")
    try:
        subprocess.Popen(["open", url], start_new_session=True)
        return f"正在播放{name}，已打开浏览器播放。"
    except Exception as e:
        return f"播放{name}失败: {e}"


@mcp.tool()
def set_alarm(hour: int, minute: int, label: str = "", repeat: str = "") -> str:
    """设置闹钟。hour=小时(0-23), minute=分钟(0-59), label=闹钟名称(可选), repeat=重复(daily/weekdays/留空=一次性)

    例: set_alarm(7, 0, "起床") → 每天早上7点闹钟
        set_alarm(8, 30, "开会", "weekdays") → 工作日8点半闹钟
        set_alarm(22, 0, "睡觉提醒") → 今晚10点提醒睡觉
    """
    try:
        from datetime import datetime as _dt, timedelta as _td
        from app.reminders import append_reminder
        now = _dt.now()
        alarm_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if alarm_time <= now:
            alarm_time += _td(days=1)
        label_clean = label or f"{hour:02d}:{minute:02d}闹钟"
        repeat_clean = repeat if repeat in ("daily", "weekdays") else ""
        item = append_reminder(label_clean, alarm_time.isoformat(), alarm_time.isoformat(), repeat=repeat_clean)
        repeat_desc = {"daily": "，每天重复", "weekdays": "，工作日重复"}.get(repeat_clean, "")
        return f"已设闹钟：{label_clean} ({hour:02d}:{minute:02d}){repeat_desc}"
    except Exception as e:
        return f"设置闹钟失败: {e}"


if __name__ == "__main__":
    mcp.run()
