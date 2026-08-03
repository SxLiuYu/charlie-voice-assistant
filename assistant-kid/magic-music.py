"""magic-music: 网易云音乐播放 (6个工具)"""
from mcp.server.fastmcp import FastMCP
from mcp_common import NCM_BIN, _ensure_https
import subprocess, json as _json, random

mcp = FastMCP("magic-music")


@mcp.tool()
def search_music(keyword: str, limit: int = 5) -> str:
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


if __name__ == "__main__":
    mcp.run()
