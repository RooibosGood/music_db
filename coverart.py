"""アルバムジャケット画像 (Cover Art) 取得モジュール

voice_bot.py から切り出し。
MPD (albumart/readpicture) / ローカルフォルダー / moOde Web (coverart.php) /
iTunes Search API / Deezer API の順でカバー画像を探索する。

MOODE_IP / MOODE_PORT は voice_bot.main() の CLI 引数に合わせて
``coverart.MOODE_IP = ...`` のように起動時に上書きされる想定。
"""
import hashlib
import json
import os
import re
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional, Tuple

from db import find_track_metadata

try:
    from mpd import MPDClient
except ImportError:
    MPDClient = None

# moOde (MPD) 接続先。voice_bot.main() から同期される。
MOODE_IP = "192.168.68.198"
MOODE_PORT = 6600

cover_art_cache: Dict[str, Tuple[bytes, str]] = {}
moode_default_cover_hash: Optional[str] = None  # moOde デフォルトジャケット画像のMD5ハッシュ


def _get_mpd_client() -> Optional[Any]:
    """MPD クライアントの接続を取得（coverart 内部専用）"""
    if MPDClient is None:
        return None
    try:
        client = MPDClient()
        client.timeout = 5
        client.connect(MOODE_IP, MOODE_PORT)
        return client
    except Exception:
        return None


def get_moode_default_cover_hash() -> Optional[str]:
    """moOde の coverart.php がカバー未発見時に返すデフォルト画像のMD5ハッシュを取得（キャッシュ）

    moOde の coverart.php は、カバー画像が存在しない場合でも HTTP 200 で
    デフォルトジャケット画像を返す仕様のため、それを検出してスキップするために使用する。
    """
    global moode_default_cover_hash
    if moode_default_cover_hash is not None:
        return moode_default_cover_hash
    if not MOODE_IP:
        return None
    try:
        url = f"http://{MOODE_IP}/coverart.php?file=__nonexistent_track_for_default_probe__.xyz"
        req = urllib.request.Request(url, headers={"User-Agent": "moOde-AI/1.0"})
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            if resp.status == 200:
                data = resp.read()
                if len(data) > 100:
                    moode_default_cover_hash = hashlib.md5(data).hexdigest()
                    print(f"🖼️ [Cover Art] moOde デフォルトジャケット検出 (MD5: {moode_default_cover_hash[:8]}...)", flush=True)
    except Exception as e:
        print(f"⚠️ [Cover Art] moOde デフォルト画像のプローブ失敗: {e}", flush=True)
    return moode_default_cover_hash

DEFAULT_COVER_SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="300" height="300" viewBox="0 0 300 300">
  <defs>
    <radialGradient id="grad" cx="50%" cy="50%" r="50%">
      <stop offset="0%" stop-color="#00f2fe" stop-opacity="0.8"/>
      <stop offset="100%" stop-color="#4facfe" stop-opacity="0.2"/>
    </radialGradient>
  </defs>
  <rect width="300" height="300" rx="16" fill="#111827"/>
  <circle cx="150" cy="150" r="100" fill="url(#grad)"/>
  <circle cx="150" cy="150" r="30" fill="#0f172a" stroke="#00f2fe" stroke-width="3"/>
  <circle cx="150" cy="150" r="6" fill="#00f2fe"/>
  <text x="150" y="270" text-anchor="middle" fill="#94a3b8" font-size="14" font-family="sans-serif">moOde Audio Player</text>
</svg>"""


def clean_album_or_artist_for_search(text: str) -> str:
    """iTunes / Deezer 検索用に [Disc 1], (Remastered) などの付加文字列を除去"""
    if not text:
        return ""
    t = re.sub(r"\[.*?\]", "", text)
    t = re.sub(r"\(.*?\)", "", t)
    t = re.sub(r"【.*?】", "", t)
    t = re.sub(r"\b(disc|disk|cd|remaster|remastered|version|edition|vol|volume|deluxe|bonus|mono|stereo|live)\b.*$", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def get_album_cover_bytes(song_file: str = "", artist: str = "", album: str = "", title: str = "") -> Tuple[bytes, str]:
    """MPD / ローカルフォルダー / moOde Web / iTunes API / Deezer API からアルバムジャケット画像を取得"""
    cache_key = f"{song_file}::{artist}::{album}::{title}"
    if cache_key in cover_art_cache:
        return cover_art_cache[cache_key]

    # 0. DBからの正確なメタデータ補完
    db_meta = find_track_metadata(file_path=song_file, title=title, artist=artist)
    if db_meta:
        if not artist or artist in ("アーティスト未設定", "Unknown", "unknown"):
            artist = db_meta.get("artist") or artist
        if not album or album in ("moOde Audio Library", "Unknown", "unknown"):
            album = db_meta.get("album") or album
        if not title or title in ("未選択", "Unknown", "unknown"):
            title = db_meta.get("title") or title
        db_file_path = db_meta.get("file_path", "")
    else:
        db_file_path = ""

    # 1. ローカル/NASの音楽フォルダ内の画像ファイル直接探索 (cover.jpg, folder.jpg 等)
    for target_path in [song_file, db_file_path]:
        if target_path:
            norm = target_path.replace("\\", "/")
            dir_path = os.path.dirname(norm)
            if dir_path and os.path.isdir(dir_path):
                for img_name in ["cover.jpg", "folder.jpg", "front.jpg", "album.jpg", "artwork.jpg", "cover.png", "folder.png", "front.png"]:
                    full_img_path = os.path.join(dir_path, img_name)
                    if os.path.isfile(full_img_path) and os.path.getsize(full_img_path) > 1000:
                        try:
                            with open(full_img_path, "rb") as f:
                                img_bytes = f.read()
                            media_type = "image/png" if img_name.endswith(".png") else "image/jpeg"
                            ret = (img_bytes, media_type)
                            cover_art_cache[cache_key] = ret
                            return ret
                        except Exception:
                            pass

    # 2. MPD albumart / readpicture コマンド
    if song_file and MPDClient is not None:
        try:
            client = _get_mpd_client()
            if client:
                try:
                    # 2-1. albumart
                    res = client.albumart(song_file, 0)
                    if isinstance(res, dict) and "binary" in res:
                        img_data = bytearray(res["binary"])
                        size = int(res.get("size", len(img_data)))
                        while len(img_data) < size:
                            chunk = client.albumart(song_file, len(img_data))
                            if not chunk or "binary" not in chunk:
                                break
                            img_data.extend(chunk["binary"])
                        if len(img_data) > 500:
                            ret = (bytes(img_data), "image/jpeg")
                            cover_art_cache[cache_key] = ret
                            return ret
                except Exception:
                    pass

                # 2-2. readpicture (ID3タグ埋め込み画像)
                try:
                    res = client.readpicture(song_file, 0)
                    if isinstance(res, dict) and "binary" in res:
                        img_data = bytearray(res["binary"])
                        size = int(res.get("size", len(img_data)))
                        while len(img_data) < size:
                            chunk = client.readpicture(song_file, len(img_data))
                            if not chunk or "binary" not in chunk:
                                break
                            img_data.extend(chunk["binary"])
                        if len(img_data) > 500:
                            ret = (bytes(img_data), "image/jpeg")
                            cover_art_cache[cache_key] = ret
                            return ret
                except Exception:
                    pass
                finally:
                    try:
                        client.close()
                        client.disconnect()
                    except Exception:
                        pass
        except Exception:
            pass

    # 3. moOde Web サーバーの coverart.php
    if song_file and MOODE_IP:
        try:
            default_hash = get_moode_default_cover_hash()
            quoted_file = urllib.parse.quote(song_file)
            for url_fmt in [
                f"http://{MOODE_IP}/coverart.php?file={quoted_file}",
                f"http://{MOODE_IP}/coverart.php/{quoted_file}",
            ]:
                try:
                    req = urllib.request.Request(url_fmt, headers={"User-Agent": "moOde-AI/1.0"})
                    with urllib.request.urlopen(req, timeout=1.5) as resp:
                        if resp.status == 200:
                            content_type = resp.headers.get("Content-Type", "image/jpeg")
                            img_bytes = resp.read()
                            if len(img_bytes) > 1000 and "image" in content_type:
                                # moOde デフォルトジャケット画像（カバー未発見時のフォールバック）ならスキップ
                                if default_hash and hashlib.md5(img_bytes).hexdigest() == default_hash:
                                    print("🖼️ [Cover Art] coverart.php はデフォルト画像を返却 → スキップ", flush=True)
                                    continue
                                ret = (img_bytes, content_type)
                                cover_art_cache[cache_key] = ret
                                return ret
                except Exception:
                    pass
        except Exception:
            pass

    # 4. iTunes Search API (クリーンアップ正規化クエリで超高画質 600x600 取得)
    clean_art = clean_album_or_artist_for_search(artist)
    clean_alb = clean_album_or_artist_for_search(album)
    clean_tit = clean_album_or_artist_for_search(title)

    queries = []
    if clean_art and clean_alb and clean_art not in ("アーティスト未設定", "Unknown"):
        queries.append(f"{clean_art} {clean_alb}")
    if clean_art and clean_tit and clean_art not in ("アーティスト未設定", "Unknown"):
        queries.append(f"{clean_art} {clean_tit}")
    if clean_alb and clean_alb not in ("moOde Audio Library", "Unknown"):
        queries.append(clean_alb)

    for q in queries:
        try:
            itunes_url = f"https://itunes.apple.com/search?term={urllib.parse.quote(q)}&entity=album&limit=1"
            req = urllib.request.Request(itunes_url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 moOde-AI/1.0"})
            with urllib.request.urlopen(req, timeout=2.5) as resp:
                res_json = json.loads(resp.read().decode("utf-8"))
                results = res_json.get("results", [])
                if results:
                    art_url = results[0].get("artworkUrl100", "")
                    if art_url:
                        hi_art_url = art_url.replace("100x100bb.jpg", "600x600bb.jpg")
                        req_art = urllib.request.Request(hi_art_url, headers={"User-Agent": "Mozilla/5.0"})
                        with urllib.request.urlopen(req_art, timeout=2.5) as art_resp:
                            img_bytes = art_resp.read()
                            if len(img_bytes) > 500:
                                ret = (img_bytes, "image/jpeg")
                                cover_art_cache[cache_key] = ret
                                return ret
        except Exception:
            pass

    # 5. Deezer Music API (iTunes で見つからない楽曲のフォールバック)
    for q in queries:
        try:
            dz_url = f"https://api.deezer.com/search?q={urllib.parse.quote(q)}&limit=1"
            req = urllib.request.Request(dz_url, headers={"User-Agent": "Mozilla/5.0 moOde-AI/1.0"})
            with urllib.request.urlopen(req, timeout=2.5) as resp:
                res_json = json.loads(resp.read().decode("utf-8"))
                data = res_json.get("data", [])
                if data:
                    album_info = data[0].get("album", {})
                    cover_url = album_info.get("cover_xl") or album_info.get("cover_big") or album_info.get("cover_medium")
                    if cover_url:
                        req_cov = urllib.request.Request(cover_url, headers={"User-Agent": "Mozilla/5.0"})
                        with urllib.request.urlopen(req_cov, timeout=2.5) as cov_resp:
                            img_bytes = cov_resp.read()
                            if len(img_bytes) > 500:
                                ret = (img_bytes, "image/jpeg")
                                cover_art_cache[cache_key] = ret
                                return ret
        except Exception:
            pass

    # 6. デフォルト SVG
    return (DEFAULT_COVER_SVG.encode("utf-8"), "image/svg+xml")
