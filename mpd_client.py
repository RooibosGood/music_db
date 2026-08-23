"""MPD (moOde Audio) 制御モジュール

voice_bot.py から切り出し。
- get_mpd_client: MPD 接続
- get_moode_status: 再生ステータスと現在曲情報の取得
- control_moode: 選曲・再生・停止・スキップ・音量などの操作

MOODE_IP / MOODE_PORT / ANNOUNCE_LANGUAGE / broadcast_process_status は
voice_bot.main() の起動時に mpd_client モジュールへ同期される想定
（循環 import 回避のため値・参照の注入方式）。
"""
import time
import traceback
from typing import Any, Dict, Optional

from db import add_db_tracks_to_mpd, find_track_metadata, search_tracks_from_db

try:
    from mpd import MPDClient
except ImportError:
    MPDClient = None

# voice_bot.main() から同期される設定値
MOODE_IP = "192.168.68.198"
MOODE_PORT = 6600
ANNOUNCE_LANGUAGE = "en"

# 処理ステータス通知関数（voice_bot から注入される。None の場合は print のみ）
broadcast_process_status = None

# 二重曲紹介防止用（voice_bot の watcher ループと共有するためモジュール変数で保持）
last_announced_file: Optional[str] = None
last_announced_songid: Optional[str] = None


def _broadcast(step: str, detail: str, auto_idle_sec: Optional[float] = None):
    """注入された broadcast_process_status があれば呼び出す"""
    if broadcast_process_status is not None:
        broadcast_process_status(step, detail, auto_idle_sec=auto_idle_sec)


def get_mpd_client() -> Optional[Any]:
    """MPD クライアントの接続を取得"""
    if MPDClient is None:
        return None
    try:
        client = MPDClient()
        client.timeout = 5
        client.connect(MOODE_IP, MOODE_PORT)
        return client
    except Exception as e:
        return None


def get_moode_status() -> Dict[str, Any]:
    """moOde の再生ステータスと現在曲情報を取得"""
    client = get_mpd_client()
    if client is None:
        return {
            "connected": False,
            "state": "stop",
            "volume": "50",
            "elapsed": 0,
            "duration": 0,
            "song": {},
        }
    try:
        status = client.status()
        song = client.currentsong()
        client.close()
        client.disconnect()

        audio_format = status.get("audio", "")
        sample_rate = ""
        bit_depth = ""
        if audio_format and ":" in audio_format:
            parts = audio_format.split(":")
            if len(parts) >= 2:
                sample_rate = parts[0]
                bit_depth = parts[1]

        song_file = song.get("file", "")
        song_title = song.get("title") or (song_file.split("/")[-1] if song_file else "未選択")
        song_artist = song.get("artist") or "アーティスト未設定"
        song_album = song.get("album") or "moOde Audio Library"

        db_meta = find_track_metadata(file_path=song_file, title=song_title, artist=song_artist)
        description_ja = db_meta.get("description_ja", "") if db_meta else ""
        description_en = db_meta.get("description_en", "") if db_meta else ""
        description = (description_en if ANNOUNCE_LANGUAGE == "en" and description_en else description_ja) or description_en or description_ja

        song_info = {
            "title": song_title,
            "artist": song_artist,
            "album": song_album,
            "file": song_file,
            "id": song.get("id", ""),
            "sample_rate": sample_rate,
            "bit_depth": bit_depth,
            "is_hires": (int(sample_rate) > 48000 or int(bit_depth) > 16) if sample_rate.isdigit() and bit_depth.isdigit() else (db_meta.get("is_hires", 0) == 1 if db_meta else False),
            "description": description,
            "description_ja": description_ja,
            "description_en": description_en,
        }

        return {
            "connected": True,
            "state": status.get("state", "stop"),
            "volume": status.get("volume", "50"),
            "elapsed": float(status.get("elapsed", 0)),
            "duration": float(status.get("duration", 0)),
            "song": song_info,
        }
    except Exception as e:
        return {
            "connected": False,
            "error": str(e),
            "state": "stop",
            "volume": "50",
            "elapsed": 0,
            "duration": 0,
            "song": {},
        }


def control_moode(command: Dict[str, Any]) -> Dict[str, Any]:
    """music_meta.db から選曲し、MPD 経由で moOde audio を操作"""
    action = command.get("action")
    query = command.get("query", "").strip()
    result = {
        "success": False,
        "action": action,
        "tracks_added": [],
        "track_info": None,
        "description": "",
        "description_ja": "",
        "description_en": "",
        "message": "",
    }

    if action == "unknown" or not action:
        print("🎵 [moOde] 音楽操作はありません。", flush=True)
        result["message"] = "音楽操作なし"
        return result

    client = get_mpd_client()
    if client is None:
        print(f"❌ [moOde] {MOODE_IP}:{MOODE_PORT} に接続できませんでした。", flush=True)
        result["message"] = f"moOde ({MOODE_IP}) に接続できませんでした。"
        return result

    global last_announced_file, last_announced_songid
    try:
        if action == "play_search":
            client.clear()

            print(f"🔍 [music_meta.db] 楽曲検索中: query='{query}'", flush=True)
            _broadcast("db", f"🔍 楽曲データベースを検索中 (SQLite): 「{query}」")
            db_tracks = search_tracks_from_db(query, limit=15)
            print(f"📊 [music_meta.db] 該当曲数: {len(db_tracks)} 件", flush=True)

            if db_tracks:
                _broadcast("moode", f"🎵 moOde 再生キューを更新中 ({len(db_tracks)}曲をセット)...")
                added_tracks = add_db_tracks_to_mpd(client, db_tracks)
                added_count = len(added_tracks)

                # MPD追加成功曲があればそれをベースに、なければDB検索1件目を使用
                first_track = added_tracks[0] if added_tracks else db_tracks[0]
                first_title = first_track.get("title", "未設定")
                first_artist = first_track.get("artist", "アーティスト未設定")
                first_file = first_track.get("relative_path", "")
                description_ja = first_track.get("description_ja", "")
                description_en = first_track.get("description_en", "")
                description = (description_en if ANNOUNCE_LANGUAGE == "en" and description_en else description_ja) or description_en or description_ja

                # MPD のプレイリスト先頭情報を取得して同期（監視ループでの誤検知・自己曲紹介を防止）
                playlist_items = client.playlistinfo()
                if playlist_items:
                    first_mpd = playlist_items[0]
                    last_announced_file = first_mpd.get("file", first_file)
                    last_announced_songid = first_mpd.get("id", "")
                else:
                    last_announced_file = first_file
                    last_announced_songid = ""

                result["tracks_added"] = [t.get("title", "") for t in added_tracks]
                result["track_info"] = {
                    "title": first_title,
                    "artist": first_artist,
                    "file": last_announced_file,
                    "description_ja": description_ja,
                    "description_en": description_en,
                }
                result["description"] = description
                result["description_ja"] = description_ja
                result["description_en"] = description_en
                result["success"] = True
                result["needs_playback"] = True
                result["message"] = f"「{query}」に該当する楽曲 ({len(db_tracks)}曲) をセットしました。"

                print(f"🎵 [moOde] '{query}' の楽曲をセットしました ({added_count}曲 キュー追加, 先頭={last_announced_file})", flush=True)
                if description_ja or description_en:
                    print(f"📖 [Description 取得成功] (日): {description_ja} | (英): {description_en}", flush=True)
                else:
                    print(f"ℹ️ [Description] DB内に解説文が見つかりませんでした (title='{first_title}', artist='{first_artist}')", flush=True)
            else:
                result["message"] = f"「{query}」に該当する曲がデータベースに見つかりませんでした。"
                print(f"⚠️ [music_meta.db] '{query}' に該当する曲が見つかりません", flush=True)

        elif action == "play":
            _broadcast("moode", "▶️ moOde 音楽再生を再開中...", auto_idle_sec=3.0)
            client.play()
            result["success"] = True
            result["message"] = "音楽の再生を再開しました。"
        elif action == "pause":
            _broadcast("moode", "⏸️ 音楽を一時停止中...", auto_idle_sec=3.0)
            client.pause(1)
            result["success"] = True
            result["message"] = "音楽を一時停止しました。"
        elif action == "stop":
            _broadcast("moode", "⏹️ 音楽再生を停止中...", auto_idle_sec=3.0)
            client.stop()
            result["success"] = True
            result["message"] = "音楽を停止しました。"
        elif action in ("next", "previous"):
            act_text = "次の曲にスキップ" if action == "next" else "前の曲に戻る"
            _broadcast("moode", f"⏭️ moOde 操作: {act_text}中...")
            if action == "next":
                client.next()
                result["message"] = "次の曲にスキップしました。"
            else:
                client.previous()
                result["message"] = "前の曲に戻りました。"

            time.sleep(0.3)
            new_song = client.currentsong()
            new_file = new_song.get("file", "")
            last_announced_file = new_file
            last_announced_songid = new_song.get("id", "")

            # 解説文読み上げのために一旦一時停止
            client.pause(1)
            result["success"] = True
            result["needs_playback"] = True

            new_title = new_song.get("title") or (new_file.split("/")[-1] if new_file else "次の曲")
            new_artist = new_song.get("artist") or "アーティスト未設定"
            db_meta = find_track_metadata(file_path=new_file, title=new_title, artist=new_artist)
            description_ja = db_meta.get("description_ja", "") if db_meta else ""
            description_en = db_meta.get("description_en", "") if db_meta else ""
            description = (description_en if ANNOUNCE_LANGUAGE == "en" and description_en else description_ja) or description_en or description_ja

            result["track_info"] = {
                "title": new_title,
                "artist": new_artist,
                "file": new_file,
                "description_ja": description_ja,
                "description_en": description_en,
            }
            result["description"] = description
            result["description_ja"] = description_ja
            result["description_en"] = description_en

        elif action == "volume":
            vol = command.get("value", 50)
            client.setvol(int(vol))
            result["success"] = True
            result["message"] = f"音量を {vol}% に設定しました。"

        client.close()
        client.disconnect()
    except Exception as e:
        print(f"❌ moOde 操作エラー: {e}")
        traceback.print_exc()
        result["message"] = f"moOde 操作エラー: {e}"

    return result
