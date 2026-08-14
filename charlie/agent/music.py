"""Music fast path: NCM (网易云音乐) direct play, bypassing LLM.

Extracted from voice_agent.py. Handles music keyword detection and
direct NCM API calls to avoid LLM 429 rate limits.
"""
import os, sys, subprocess, json, random, re, logging

log = logging.getLogger("magic")


def direct_music_play(text: str) -> str:
    """音乐关键词命中时直接调用 ncm-cli，绕过 LLM，避免 429 限流和反复迭代。
    返回 __MUSIC__url__name__artist 或空字符串(失败时)。"""
    NCM = os.path.expanduser("~/.local/bin/ncm")
    if getattr(sys, 'frozen', False):
        _ncm_packed = os.path.join(os.path.dirname(sys.executable), '_internal', 'bin', 'ncm')
        if os.path.isfile(_ncm_packed):
            NCM = _ncm_packed
    try:
        is_random = any(kw in text for kw in ('随机', '随便', '来一首', '来首歌', '随便来', '随便播'))

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
