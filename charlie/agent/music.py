"""Music fast path: NCM (网易云音乐) direct play, bypassing LLM.

Extracted from voice_agent.py. Handles music keyword detection and
direct NCM API calls to avoid LLM 429 rate limits.
"""
import os, sys, subprocess, json, random, re, logging

log = logging.getLogger("magic")


def netease_search_url(keyword: str) -> tuple[str, str, str] | None:
    """ncm CLI 缺失时的网易云 HTTP 兜底：搜索 → outer url。

    返回 (url, song_name, artist) 或 None。outer url 对免费曲目可直接播。
    """
    try:
        from voice_agent import _session
        r = _session.post(
            "https://music.163.com/api/search/get",
            data={"s": keyword, "type": 1, "limit": 8},
            headers={"Referer": "https://music.163.com/",
                     "User-Agent": "Mozilla/5.0"},
            timeout=8)
        songs = (r.json() or {}).get("result", {}).get("songs", [])
        if not songs:
            return None
        song = random.choice(songs[:8])
        sid = song.get("id")
        name = song.get("name", "")
        ars = "/".join(a.get("name", "") for a in song.get("artists", []))
        if not sid:
            return None
        url = f"https://music.163.com/song/media/outer/url?id={sid}.mp3"
        return url, name, ars
    except Exception as e:
        log.warning(f"[music] HTTP 兜底搜索失败: {e}")
        return None


def _ncm_path() -> str:
    NCM = os.path.expanduser("~/.local/bin/ncm")
    if getattr(sys, 'frozen', False):
        _ncm_packed = os.path.join(os.path.dirname(sys.executable), '_internal', 'bin', 'ncm')
        if os.path.isfile(_ncm_packed):
            NCM = _ncm_packed
    return NCM


_GENERIC_SONG_WORDS = {
    "歌", "歌曲", "音乐", "一首歌", "首歌", "点歌", "来首歌", "歌来",
    "好听的歌", "开心的歌", "喜欢的歌", "流行歌", "热歌",
}


def _strip_music_prefix(text: str) -> str:
    return re.sub(
        r'^(播放|放一首|放个|我要听|我想听|来一首|播一首|点一首|放|唱|听|来首|点歌|一首|放首|放点|整首|整点|循环|单曲)',
        '', (text or '').strip()
    ).strip().rstrip('。.,，！! 　')


_warm_proto: str = ""  # keep-hot 预热好的那首（磁盘缓存已落），首次放歌秒出声


def set_warm_song(proto: str) -> None:
    global _warm_proto
    if proto and proto.startswith("__MUSIC__"):
        _warm_proto = proto


def get_warm_song() -> str:
    return _warm_proto


def direct_music_play(text: str) -> str:
    """音乐关键词命中时直接调用 ncm-cli，绕过 LLM，避免 429 限流和反复迭代。
    返回 __MUSIC__url__name__artist 或空字符串(失败时)。"""
    text = text or ""
    NCM = _ncm_path()
    have_ncm = os.path.isfile(NCM) or os.path.isfile(NCM + ".exe")
    is_random = any(kw in text for kw in (
        '随机', '随便', '来一首', '来首歌', '随便来', '随便播',
        '换一首', '换首歌', '切歌', '下一首', '上一首', '换个歌', '换首'))
    keyword = _strip_music_prefix(text)
    if keyword in _GENERIC_SONG_WORDS or keyword in (
            "换一首", "切歌", "下一首", "上一首", "随机播放", "随机",
            "歌单", "来个歌单", "每日推荐", "热歌榜", "推荐歌曲"):
        keyword = "热门歌曲"
        is_random = True
    global _warm_proto
    if is_random and _warm_proto:
        # 首次"放歌/唱个歌"用预热那首（磁盘缓存命中，~0.9s 出声）；
        # 用完即清，"换一首"再随机新的。
        proto = _warm_proto
        _warm_proto = ""
        log.info("[music] 命中预热缓存: %.40s", proto)
        return proto
    if not have_ncm:
        # ncm 未安装：HTTP 兜底，别让 LLM 再去调 play_music 撞 FileNotFoundError
        hit = netease_search_url(keyword or "热门歌曲")
        if not hit:
            return ""
        url, song_name, ars = hit
        log.info(f"[music] HTTP兜底: {song_name} - {ars}")
        return f"__MUSIC__{url}__{song_name}__{ars}"
    try:
        if is_random:
            r = subprocess.run([NCM, "recommend", "songs", "--json"], capture_output=True, text=True, timeout=10)
            songs = []
            if r.returncode == 0 and r.stdout.strip():
                data = json.loads(r.stdout)
                songs = data.get("recommend", []) or data.get("data", [])
            if not songs:
                r = subprocess.run([NCM, "record", "--json", "--limit", "30"], capture_output=True, text=True, timeout=10)
                if r.returncode == 0 and r.stdout.strip():
                    data = json.loads(r.stdout)
                    songs = data.get("data", []) or data.get("record", [])
            if not songs:
                return ""
            song = random.choice(songs)
            song_id = song["id"]
            song_name = song.get("name", "")
            ars = "/".join(a.get("name", "") for a in song.get("ar", []))
        else:
            keyword = re.sub(
                r'^(播放|放一首|放个|我要听|我想听|来一首|播一首|点一首|放|唱|听|来首|点歌|一首|放首|放点|整首|整点|循环|单曲)',
                '', text
            ).strip().rstrip('。.,，！!')
            if not keyword or len(keyword) < 1:
                return ""
            r = subprocess.run([NCM, "search", "song", keyword, "--limit", "1", "--json"], capture_output=True, text=True, timeout=10)
            if r.returncode != 0:
                return ""
            data = json.loads(r.stdout)
            songs = data.get("result", {}).get("songs", [])
            if not songs:
                return ""
            song_id = songs[0]["id"]
            song_name = songs[0]["name"]
            ars = "/".join(a.get("name", "") for a in songs[0].get("ar", []))

        r = subprocess.run([NCM, "url", str(song_id), "--level", "exhigh", "--json"], capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            return ""
        url_data = json.loads(r.stdout)
        _d = url_data.get("data", [])
        url = _d[0].get("url", "") if isinstance(_d, list) and _d else (_d.get("url", "") if isinstance(_d, dict) else url_data.get("url", ""))
        if not url:
            return ""
        if url.startswith("http://"):
            url = "https://" + url[7:]
        elif url.startswith("//"):
            url = "https:" + url
        log.info(f"[music] ncm直连成功: {song_name} - {ars} ({len(url)}B URL)")
        return f"__MUSIC__{url}__{song_name}__{ars}"
    except Exception as e:
        log.warning(f"[music] ncm直连异常: {e}")
        return ""
