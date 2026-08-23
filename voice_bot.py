import argparse
import asyncio
import io
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import wave
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel

# 分割モジュール (db.py / coverart.py)
import coverart
from coverart import get_album_cover_bytes
from db import add_db_tracks_to_mpd, find_track_metadata, search_tracks_from_db

# 音声系ライブラリの安全なインポート
try:
    import pyaudio
except ImportError:
    pyaudio = None

try:
    from faster_whisper import WhisperModel
except ImportError:
    WhisperModel = None

try:
    from mpd import MPDClient
except ImportError:
    MPDClient = None

try:
    import edge_tts
except ImportError:
    edge_tts = None


# ==================== 設定領域 ====================
MOODE_IP = "192.168.68.198"  # moOde (Raspberry Pi 5) の IP アドレス
MOODE_PORT = 6600

VOICEVOX_URL = "http://localhost:50021"
OLLAMA_CHAT_URL = "http://localhost:11434/api/chat"
SPEAKER_ID = 13  # 青山龍星（落ち着いた男性音声）
LLM_MODEL = "qwen2.5:1.5b"
ANNOUNCE_LANGUAGE = "en"  # 曲紹介の言語: "en" (英語DJモード) または "ja" (日本語)
ENGLISH_VOICE = "en-US-ChristopherNeural"  # 英語ラジオDJ風ニューラル音声 (edge-tts)
AUDIO_OUTPUT_NAME = "Sennheiser"  # 再生デバイス名（部分一致で自動検索）
AUDIO_OUTPUT_DEV = None  # Noneの場合は自動検出、または "plughw:1,0" 等
VOICE_PRE_SILENCE_SEC = 0.3  # 再生開始時の音切れ防止用
INPUT_DEVICE_NAME = "Sennheiser SP 20"  # PyAudioの表示名に含まれる文字列
INPUT_DEVICE_INDEX = None  # 名前で見つからない場合に使うPyAudio番号
# DB_PATH は db.py に移動

WAKE_WORD_PATTERNS = (
    r"ヘイ[\s、,。！？!?]*マスター",
    r"へい[\s、,。！？!?]*ますたー",
    r"hey[\s、,。！？!?]*master",
)

# マイク録音設定
FORMAT = pyaudio.paInt16 if pyaudio else 2
CHANNELS = 1
RATE = 16000
CHUNK = 1024
RECORD_SECONDS = 4

# グローバル状態管理
voice_lock = threading.Lock()
is_speaking_event = threading.Event()
stt_model = None
chat_history: List[Dict[str, Any]] = []
active_websockets: List[WebSocket] = []
last_announced_file: Optional[str] = None  # 二重曲紹介防止用 (MPD file)
last_announced_songid: Optional[str] = None  # 二重曲紹介防止用 (MPD songid)
# recent_played_track_ids は db.py に移動
voice_state = {
    "is_listening": False,
    "state": "idle",
    "last_text": "",
    "error": None,
}


def is_same_track(file_a: Optional[str], file_b: Optional[str], id_a: Optional[str] = None, id_b: Optional[str] = None) -> bool:
    """2つのトラック情報が同一曲かどうかを判定（MPD ID、フルパス、相対パス、ファイル名で比較）"""
    if id_a and id_b and str(id_a).strip() == str(id_b).strip() and str(id_a).strip() != "":
        return True
    if not file_a or not file_b:
        return False
    norm_a = file_a.replace("\\", "/").rstrip("/")
    norm_b = file_b.replace("\\", "/").rstrip("/")
    if norm_a == norm_b:
        return True
    if norm_a.endswith(norm_b) or norm_b.endswith(norm_a):
        return True
    base_a = norm_a.split("/")[-1]
    base_b = norm_b.split("/")[-1]
    return base_a == base_b and base_a != ""


# ==================== HTTP ヘルパー ====================
def http_post_json(url: str, data: Dict[str, Any], timeout: float = 30.0) -> Dict[str, Any]:
    """JSONペイロードをPOSTしてJSONレスポンスを返す"""
    json_bytes = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=json_bytes,
        headers={"Content-Type": "application/json", "User-Agent": "moOde-AI-Master/1.0"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        resp_data = response.read().decode("utf-8")
        return json.loads(resp_data)


# find_track_metadata / search_tracks_from_db / add_db_tracks_to_mpd は db.py に移動
# get_album_cover_bytes など Cover Art 関連は coverart.py に移動


# ==================== MPD (moOde) 制御 ====================
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
            broadcast_process_status("db", f"🔍 楽曲データベースを検索中 (SQLite): 「{query}」")
            db_tracks = search_tracks_from_db(query, limit=15)
            print(f"📊 [music_meta.db] 該当曲数: {len(db_tracks)} 件", flush=True)

            if db_tracks:
                broadcast_process_status("moode", f"🎵 moOde 再生キューを更新中 ({len(db_tracks)}曲をセット)...")
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
            broadcast_process_status("moode", "▶️ moOde 音楽再生を再開中...", auto_idle_sec=3.0)
            client.play()
            result["success"] = True
            result["message"] = "音楽の再生を再開しました。"
        elif action == "pause":
            broadcast_process_status("moode", "⏸️ 音楽を一時停止中...", auto_idle_sec=3.0)
            client.pause(1)
            result["success"] = True
            result["message"] = "音楽を一時停止しました。"
        elif action == "stop":
            broadcast_process_status("moode", "⏹️ 音楽再生を停止中...", auto_idle_sec=3.0)
            client.stop()
            result["success"] = True
            result["message"] = "音楽を停止しました。"
        elif action in ("next", "previous"):
            act_text = "次の曲にスキップ" if action == "next" else "前の曲に戻る"
            broadcast_process_status("moode", f"⏭️ moOde 操作: {act_text}中...")
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


# ==================== 音声合成 & 出力 (VOICEVOX) ====================
def detect_alsa_output_device(target_name: str = "Sennheiser") -> str:
    """aplay -l からターゲットデバイス名に一致する ALSA デバイス (plughw:X,Y) を自動検出"""
    if os.name == "nt":
        return "default"
    try:
        res = subprocess.run(["aplay", "-l"], capture_output=True, text=True)
        if res.returncode == 0:
            lines = res.stdout.splitlines()
            for line in lines:
                if target_name.lower() in line.lower() or "sp 20" in line.lower() or "sp20" in line.lower():
                    card_match = re.search(r"(?:card|カード)\s*(\d+)", line, re.IGNORECASE)
                    dev_match = re.search(r"(?:device|デバイス)\s*(\d+)", line, re.IGNORECASE)
                    card_idx = card_match.group(1) if card_match else None
                    dev_idx = dev_match.group(1) if dev_match else "0"
                    if card_idx is not None:
                        dev_str = f"plughw:{card_idx},{dev_idx}"
                        return dev_str
            for line in lines:
                if "usb audio" in line.lower() or "usb-audio" in line.lower():
                    card_match = re.search(r"(?:card|カード)\s*(\d+)", line, re.IGNORECASE)
                    dev_match = re.search(r"(?:device|デバイス)\s*(\d+)", line, re.IGNORECASE)
                    card_idx = card_match.group(1) if card_match else None
                    dev_idx = dev_match.group(1) if dev_match else "0"
                    if card_idx is not None:
                        return f"plughw:{card_idx},{dev_idx}"
    except Exception as e:
        print(f"⚠️ [Audio Output] デバイス検出エラー: {e}")
    return "default"


def play_wav_file(wav_path: str, target_dev: Optional[str] = None) -> bool:
    """ALSA aplay / Windows winsound で WAV ファイルを安全・確実に再生（自動フォールバック対応）"""
    global AUDIO_OUTPUT_DEV
    if not os.path.exists(wav_path) or os.path.getsize(wav_path) < 100:
        print(f"⚠️ [play_wav_file] WAVファイルが無効または空です: {wav_path}", flush=True)
        return False

    dev = target_dev or AUDIO_OUTPUT_DEV or detect_alsa_output_device(AUDIO_OUTPUT_NAME)
    if os.name != "nt":
        print(f"🔊 [aplay] 再生中 (デバイス: {dev}, ファイル: {wav_path})...", flush=True)
        # 1. 指定または検出した ALSA デバイスで再生
        res = subprocess.run(["aplay", "-D", dev, "-q", wav_path], capture_output=True)
        if res.returncode == 0:
            return True

        err_msg = res.stderr.decode("utf-8", errors="ignore").strip()
        print(f"⚠️ [aplay] -D {dev} 失敗 (code {res.returncode}): {err_msg}", flush=True)

        # 2. デフォルトデバイス (default) で再試行
        if dev != "default":
            print("🔊 [aplay] default デバイスで再試行中...", flush=True)
            res_def = subprocess.run(["aplay", "-D", "default", "-q", wav_path], capture_output=True)
            if res_def.returncode == 0:
                return True
            err_def = res_def.stderr.decode("utf-8", errors="ignore").strip()
            print(f"⚠️ [aplay] default デバイスでも失敗: {err_def}", flush=True)

        # 3. 最後の手段: -D 引数なしで再生
        print("🔊 [aplay] デバイス指定なし (aplay -q) で再生試行...", flush=True)
        res_raw = subprocess.run(["aplay", "-q", wav_path], capture_output=True)
        return res_raw.returncode == 0
    else:
        try:
            import winsound
            winsound.PlaySound(wav_path, winsound.SND_FILENAME)
            return True
        except Exception as e:
            print(f"⚠️ [winsound] 再生エラー: {e}", flush=True)
            return False


def add_silence_padding_to_wav(source_wav_path: str, output_wav_path: str, silence_sec: float = VOICE_PRE_SILENCE_SEC) -> bool:
    """WAVファイルの先頭に無音フレームを付加してスピーカー（Sennheiser SP 20等）の頭切れ・音切れを防止"""
    try:
        with wave.open(source_wav_path, "rb") as source_wav:
            params = source_wav.getparams()
            frames = source_wav.readframes(source_wav.getnframes())
            framerate = source_wav.getframerate()
            nchannels = source_wav.getnchannels()
            sampwidth = source_wav.getsampwidth()

        with wave.open(output_wav_path, "wb") as output_wav:
            output_wav.setparams(params)
            silence_frames = int(framerate * silence_sec)
            output_wav.writeframes(b"\0" * silence_frames * nchannels * sampwidth)
            output_wav.writeframes(frames)
        return True
    except Exception as e:
        print(f"⚠️ [WAV Padding] 無音パディング付加エラー: {e}", flush=True)
        return False


def fetch_google_tts_audio(text: str, lang: str = "en", output_file: str = "/tmp/voice_reply_en.mp3") -> bool:
    """Google Translate TTS からネイティブ英語音声を直接ダウンロード (依存関係ゼロ・確実動作)"""
    try:
        base_url = "https://translate.google.com/translate_tts"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        }
        # 句読点で文を分割
        sentences = re.split(r"(?<=[.!?])\s+", text)
        all_audio_bytes = bytearray()

        for s in sentences:
            s = s.strip()
            if not s:
                continue
            params = urllib.parse.urlencode({
                "ie": "UTF-8",
                "q": s[:180],
                "tl": lang,
                "client": "tw-ob"
            })
            url = f"{base_url}?{params}"
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                all_audio_bytes.extend(resp.read())

        if len(all_audio_bytes) > 200:
            with open(output_file, "wb") as f:
                f.write(all_audio_bytes)
            print(f"✅ [Google TTS] ネイティブ英語音声ダウンロード成功 ({len(all_audio_bytes)} bytes)", flush=True)
            return True
    except Exception as e:
        print(f"⚠️ [Google TTS] 音声取得エラー: {e}", flush=True)
    return False


def play_mp3_or_wav_audio(mp3_path: str, raw_wav_path: str, padded_wav_path: str) -> bool:
    """MP3 ファイルを ALSA (Sennheiser SP 20 / default) から確実にネイティブ音声再生"""
    target_dev = AUDIO_OUTPUT_DEV or detect_alsa_output_device(AUDIO_OUTPUT_NAME)

    # 1. ffmpeg で WAV 変換 ➔ 無音パディング付加 ➔ aplay
    if shutil.which("ffmpeg") and os.path.exists(mp3_path) and os.path.getsize(mp3_path) > 100:
        conv = subprocess.run(
            ["ffmpeg", "-y", "-i", mp3_path, "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le", raw_wav_path],
            capture_output=True, timeout=6
        )
        if conv.returncode == 0 and os.path.exists(raw_wav_path):
            add_silence_padding_to_wav(raw_wav_path, padded_wav_path, silence_sec=VOICE_PRE_SILENCE_SEC)
            if play_wav_file(padded_wav_path, target_dev):
                return True

    # 2. mpg123 で WAV 変換 ➔ 無音パディング付加 ➔ aplay
    if shutil.which("mpg123") and os.path.exists(mp3_path) and os.path.getsize(mp3_path) > 100:
        conv = subprocess.run(["mpg123", "-w", raw_wav_path, mp3_path], capture_output=True, timeout=6)
        if conv.returncode == 0 and os.path.exists(raw_wav_path):
            add_silence_padding_to_wav(raw_wav_path, padded_wav_path, silence_sec=VOICE_PRE_SILENCE_SEC)
            if play_wav_file(padded_wav_path, target_dev):
                return True
        # mpg123 直接再生
        res_direct = subprocess.run(["mpg123", "-a", target_dev, "-q", mp3_path], capture_output=True)
        if res_direct.returncode == 0:
            return True
        if target_dev != "default":
            res_def = subprocess.run(["mpg123", "-a", "default", "-q", mp3_path], capture_output=True)
            if res_def.returncode == 0:
                return True

    # 3. mpv で直接再生
    if shutil.which("mpv") and os.path.exists(mp3_path) and os.path.getsize(mp3_path) > 100:
        res_mpv = subprocess.run(["mpv", f"--audio-device=alsa/{target_dev}", "--no-video", mp3_path], capture_output=True)
        if res_mpv.returncode == 0:
            return True
        res_mpv_def = subprocess.run(["mpv", "--no-video", mp3_path], capture_output=True)
        if res_mpv_def.returncode == 0:
            return True

    # 4. ffplay で直接再生
    if shutil.which("ffplay") and os.path.exists(mp3_path) and os.path.getsize(mp3_path) > 100:
        res_ff = subprocess.run(["ffplay", "-nodisp", "-autoexit", mp3_path], capture_output=True)
        if res_ff.returncode == 0:
            return True

    # 5. aplay (もし既に padded WAV がある場合)
    if os.path.exists(padded_wav_path) and os.path.getsize(padded_wav_path) > 100:
        return play_wav_file(padded_wav_path, target_dev)

    # 6. Windows 環境 (winsound)
    if os.name == "nt":
        if os.path.exists(padded_wav_path):
            return play_wav_file(padded_wav_path)
        if shutil.which("ffmpeg") and os.path.exists(mp3_path):
            subprocess.run(["ffmpeg", "-y", "-i", mp3_path, raw_wav_path], capture_output=True)
            if os.path.exists(raw_wav_path):
                return play_wav_file(raw_wav_path)

    return False


def clean_text_for_speech(text: str, max_chars: int = 120) -> str:
    """VOICEVOX 読み上げ用にテキストを整形・短縮（自然な1〜2文を抽出）"""
    if not text:
        return ""
    # 特殊記号や重複括弧の除去
    t = text.replace("《", "").replace("》", "").replace("『", "「").replace("』", "」")
    t = re.sub(r"[【】\[\]\(\)]", " ", t)
    # moOde を日本語で自然に「モード」と発音
    t = re.sub(r"\bmo+de\b", "モード", t, flags=re.IGNORECASE)
    t = re.sub(r"\bmoOde\b", "モード", t)
    # 先頭の不自然なゴミ文字（「S「〜や数字等）をクリーンアップ
    t = re.sub(r"^「[A-Za-z0-9]「", "「", t)
    t = re.sub(r"^1(\d{3}年代)", r"\1", t)
    
    # 句点または読点で文を分割
    sentences = re.split(r"(?<=[。！？!?])", t)
    result = ""
    for s in sentences:
        s = s.strip()
        if not s:
            continue
        if len(result) + len(s) <= max_chars:
            result += s
        else:
            if not result:
                result = s[:max_chars] + "。"
            break
    return result or t[:max_chars]


# 英語・音楽用語のカタカナ発音辞書
ENGLISH_KATAKANA_DICT = {
    # システム・プレイヤー名
    "moode audio": "モード・オーディオ",
    "moode ai": "モード・エーアイ",
    "moode": "モード",

    # 代表的アーティスト・バンド名
    "cream": "クリーム",
    "the beatles": "ザ・ビートルズ",
    "beatles": "ビートルズ",
    "eric clapton": "エリック・クラプトン",
    "clapton": "クラプトン",
    "diana krall": "ダイアナ・クラール",
    "bill evans": "ビル・エヴァンス",
    "miles davis": "マイルス・デイヴィス",
    "john coltrane": "ジョン・コルトレーン",
    "norah jones": "ノラ・ジョーンズ",
    "steely dan": "スティーリー・ダン",
    "pink floyd": "ピンク・フロイド",
    "led zeppelin": "レッド・ツェッペリン",
    "queen": "クイーン",
    "michael jackson": "マイケル・ジャクソン",
    "the ritz": "ザ・リッツ",
    "ritz": "リッツ",

    # 代表曲名・キーワード
    "white room": "ホワイト・ルーム",
    "crossroads": "クロスロード",
    "sunshine of your love": "サンシャイン・オブ・ユア・ラヴ",
    "badge": "バッジ",
    "spoonful": "スプーンフル",
    "politician": "ポリティシャン",
    "sitting on top of the world": "シッティング・オン・トップ・オブ・ザ・ワールド",
    "born under a bad sign": "ボーン・アンダー・ア・バッド・サイン",
    "passing the time": "パッシング・ザ・タイム",
    "as you said": "アズ・ユー・セッド",
    "pressed rat and warthog": "プレスド・ラット・アンド・ウォートホッグ",
    "those were the days": "ゾーズ・ワー・ザ・デイズ",
    "deserted cities of the heart": "デザート・シティーズ・オブ・ザ・ハート",
    "fly me to the moon": "フライ・ミー・トゥ・ザ・ムーン",
    "waltz for debby": "ワルツ・フォー・デビィ",
    "autumn leaves": "枯葉",
    "take five": "テイク・ファイブ",
    "blue in green": "ブルー・イン・グリーン",
    "meditation": "メディテーション",
    "it could happen to you": "イット・クッド・ハプン・トゥ・ユー",
    "take my breath away": "テイク・マイ・ブレス・アウェイ",
    "mack the knife": "マック・ザ・ナイフ",

    # 一般音楽用語
    "live": "ライブ",
    "take": "テイク",
    "disc": "ディスク",
    "disk": "ディスク",
    "vol": "ボリューム",
    "remaster": "リマスター",
    "remastered": "リマスター",
    "version": "バージョン",
    "acoustic": "アコースティック",
    "featuring": "フィーチャリング",
    "feat": "フィーチャリング",
    "track": "トラック",
    "album": "アルバム",
    "jazz": "ジャズ",
    "rock": "ロック",
    "pop": "ポップ",
    "blues": "ブルース",
    "classic": "クラシック",
    "classical": "クラシック",
    "best": "ベスト",
    "greatest": "グレイテスト",
    "hits": "ヒッツ",
    "the": "ザ",
    "love": "ラヴ",
    "night": "ナイト",
    "day": "デイ",
    "time": "タイム",
    "world": "ワールド",
    "music": "ミュージック",
}


def convert_english_to_katakana(text: str) -> str:
    """英単語・英語曲名・アーティスト名を VOICEVOX 用の自然なカタカナ読みに変換（アルファベット棒読み防止）"""
    if not text or not re.search(r"[A-Za-z]{2,}", text):
        return text

    converted = text

    # 1. 高速辞書置換（長いフレーズから順にマッチング）
    sorted_dict = sorted(ENGLISH_KATAKANA_DICT.items(), key=lambda x: len(x[0]), reverse=True)
    for en_word, kana_word in sorted_dict:
        pattern = re.compile(rf"\b{re.escape(en_word)}\b", re.IGNORECASE)
        converted = pattern.sub(kana_word, converted)

    # アルファベットが残っていなければ終了
    if not re.search(r"[A-Za-z]{2,}", converted):
        return converted

    # 2. Ollama (LLM) による文脈カタカナ化
    try:
        payload = {
            "model": LLM_MODEL,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "あなたは日本語音声合成用の発音変換アシスタントです。"
                        "入力文に含まれる英単語やアルファベット（曲名、アーティスト名等）を、自然な日本語カタカナ読みに変換してください。"
                        "文構造や前後の日本語はそのまま保ち、変換後のナレーション文のみを1行で出力してください。"
                        "余計な解説や引用符、マークダウンは一切出力しないでください。"
                    ),
                },
                {"role": "user", "content": converted},
            ],
            "stream": False,
            "think": False,
            "keep_alive": "10m",
            "options": {
                "num_ctx": 1024,
                "temperature": 0,
                "num_predict": 128,
            },
        }
        res_json = http_post_json(OLLAMA_CHAT_URL, payload, timeout=4.0)
        llm_reply = res_json.get("message", {}).get("content", "").strip()
        llm_reply = re.sub(r"<think>[\s\S]*?</think>", "", llm_reply).strip()
        llm_reply = llm_reply.replace("```", "").replace("\n", " ").strip()
        if llm_reply and len(llm_reply) >= len(converted) * 0.5:
            print(f"🔤 [Kana] 英語カタカナ変換: '{text}' ➔ '{llm_reply}'", flush=True)
            return llm_reply
    except Exception:
        pass

    return converted


def speak(text: str):
    """VOICEVOX ➔ aplay で Jetson スピーカーから音声出力（英語のカタカナ化対応）"""
    global AUDIO_OUTPUT_DEV
    if not text:
        return

    # アルファベットの棒読みを防止し、自然なカタカナ発音に変換
    text = convert_english_to_katakana(text)

    with voice_lock:
        is_speaking_event.set()
        started_at = time.monotonic()
        print(f"🔊 [VOICEVOX] 読み上げ開始: '{text}'", flush=True)
        temp_wav = "/tmp/voice_reply.wav" if os.name != "nt" else os.path.join(os.environ.get("TEMP", "."), "voice_reply.wav")
        try:
            # 1. audio_query (タイムアウトを十分に確保)
            encoded_text = urllib.parse.quote(text)
            query_url = f"{VOICEVOX_URL}/audio_query?text={encoded_text}&speaker={SPEAKER_ID}"
            req_q = urllib.request.Request(query_url, data=b"", headers={"User-Agent": "moOde-AI/1.0"}, method="POST")
            with urllib.request.urlopen(req_q, timeout=30) as res_q:
                query_data = res_q.read()

            # 2. synthesis (長文でも耐えられるよう timeout=60 に設定)
            synth_url = f"{VOICEVOX_URL}/synthesis?speaker={SPEAKER_ID}"
            req_s = urllib.request.Request(
                synth_url,
                data=query_data,
                headers={"Content-Type": "application/json", "User-Agent": "moOde-AI/1.0"},
                method="POST",
            )
            with urllib.request.urlopen(req_s, timeout=60) as res_s:
                wav_bytes = res_s.read()

            # 3. wavファイル生成（先頭に無音パディングを付加して音切れ防止）
            with wave.open(io.BytesIO(wav_bytes), "rb") as source_wav:
                with wave.open(temp_wav, "wb") as output_wav:
                    output_wav.setparams(source_wav.getparams())
                    silence_frames = int(source_wav.getframerate() * VOICE_PRE_SILENCE_SEC)
                    output_wav.writeframes(
                        b"\0" * silence_frames * source_wav.getnchannels() * source_wav.getsampwidth()
                    )
                    output_wav.writeframes(source_wav.readframes(source_wav.getnframes()))

            # 4. 音声再生
            play_wav_file(temp_wav)
            print(f"🔊 [VOICEVOX] 音声出力完了（{time.monotonic() - started_at:.1f}秒）", flush=True)
        except urllib.error.URLError as url_err:
            print(f"❌ [VOICEVOX] 接続エラー ({VOICEVOX_URL}): {url_err}")
            print("💡 VOICEVOX (ポート 50021) が Jetson 上で起動しているか確認してください。")
        except Exception as e:
            print(f"❌ [VOICEVOX] 発話処理エラー: {e}")
            traceback.print_exc()
        finally:
            if os.path.exists(temp_wav):
                try:
                    os.remove(temp_wav)
                except Exception:
                    pass
            is_speaking_event.clear()


GENRE_JA_TO_EN = {
    "ジャズ": "Jazz",
    "ロック": "Rock",
    "ポップ": "Pop",
    "クラシック": "Classical",
    "ブルース": "Blues",
    "R&B・ソウル": "Soul & R&B",
    "エレクトロニック": "Electronic",
    "フォーク・カントリー": "Folk & Country",
    "ヒップホップ": "Hip-Hop",
    "サウンドトラック・インスト": "Soundtrack",
    "その他": "",
}


# ==================== 英語曲紹介ナレーション & 英語音声合成 ====================
def clean_english_text_for_speech(text: str, max_chars: int = 250) -> str:
    """英語読み上げ用にテキストを整形（不要記号や改行の削除、文単位での適切な長さ制限）"""
    if not text:
        return ""
    # 特殊記号や引用符・角括弧のクリーンアップ
    t = text.replace('"', '').replace('"', '').replace('"', '').replace('`', '').replace('“', '').replace('”', '')
    t = re.sub(r'[\r\n\t]+', ' ', t)
    t = re.sub(r'\s+', ' ', t).strip()
    # moOde は英語音声合成エンジンで "mode" と発音
    t = re.sub(r"\bmo+de\b", "mode", t, flags=re.IGNORECASE)
    t = re.sub(r"\bmoOde\b", "mode", t)

    # 句点（. ! ?）で文を分割して長さを調整
    sentences = re.split(r'(?<=[.!?])\s+', t)
    result = []
    curr_len = 0
    for s in sentences:
        s = s.strip()
        if not s:
            continue
        if curr_len + len(s) + 1 <= max_chars:
            result.append(s)
            curr_len += len(s) + 1
        else:
            if not result:
                result.append(s[:max_chars].rstrip() + ".")
            break
    return " ".join(result) if result else t[:max_chars]


def build_english_track_announcement(
    track_info: Dict[str, Any],
    is_next: bool = False,
    is_skip: bool = False,
) -> str:
    """曲情報からスマートで自然な英語FMラジオDJ曲紹介文を生成（description_en を最優先活用）"""
    if not track_info:
        return "Now playing the next track. Enjoy the music." if is_next else "Now playing music."

    title = (track_info.get("title") or "Unknown Track").strip()
    artist = (track_info.get("artist") or "").strip()
    genre = (track_info.get("genre") or "").strip()
    mood = (track_info.get("mood") or "").strip()
    desc_en = (track_info.get("description_en") or "").strip()
    desc_ja = (track_info.get("description_ja") or track_info.get("description") or "").strip()

    # 日本語/未設定表記のクリーンアップ
    if artist in ("アーティスト未設定", "Unknown", "unknown", "None", ""):
        artist = ""

    # 英語ジャンル名への変換
    en_genre = genre
    for ja_g, en_g in GENRE_JA_TO_EN.items():
        if ja_g in str(genre):
            en_genre = en_g
            break

    # 基本の英語DJプレフィックスフレーズ
    if is_skip:
        base_msg = f"Skipping to '{title}' by {artist}." if artist else f"Skipping to '{title}'."
    elif is_next:
        base_msg = f"Next up is '{title}' by {artist}." if artist else f"Next up is '{title}'."
    else:
        base_msg = f"Now playing: '{title}' by {artist}." if artist else f"Now playing: '{title}'."

    # 1. description_en が存在する場合：直接 description_en を結合して流暢に紹介
    if desc_en:
        clean_en = clean_english_text_for_speech(desc_en, max_chars=220)
        if clean_en:
            announcement = f"{base_msg} {clean_en}"
            print(f"🎙️ [English DJ ナレーション (description_en)] {announcement}", flush=True)
            return announcement

    # 2. description_en は無いが description_ja がある場合：LLM (Ollama) で英語DJ紹介文を生成
    if desc_ja:
        try:
            prompt = (
                f"You are a sophisticated FM Radio DJ. "
                f"Write a smooth, natural single-sentence track introduction in English based on: "
                f"Title: {title}, Artist: {artist}, Genre: {en_genre}, Description: {desc_ja[:150]}. "
                f"Keep it under 25 words. Output ONLY the single DJ sentence without quotes or preamble."
            )
            payload = {
                "model": LLM_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "options": {"num_ctx": 1024, "temperature": 0.3, "num_predict": 48},
            }
            res = http_post_json(OLLAMA_CHAT_URL, payload, timeout=2.5)
            dj_line = res.get("message", {}).get("content", "").strip()
            dj_line = re.sub(r"<think>[\s\S]*?</think>", "", dj_line).strip()
            dj_line = dj_line.replace('"', '').replace('```', '').replace('\n', ' ').strip()
            if dj_line and len(dj_line) > 10:
                print(f"🎙️ [English DJ ナレーション (LLM生成)] '{dj_line}'", flush=True)
                return dj_line
        except Exception:
            pass

    # 3. テンプレートによる補完
    extra = ""
    if en_genre and en_genre != "その他":
        extra = f" Enjoy this {en_genre} track."
    elif mood:
        clean_mood = mood.split(",")[0].strip()
        extra = f" Setting a {clean_mood} mood."
    else:
        extra = " Enjoy the music."

    announcement = f"{base_msg}{extra}"
    print(f"🎙️ [English DJ ナレーション (Template)] {announcement}", flush=True)
    return announcement


def speak_english(text: str):
    """英語テキストを Jetson スピーカーからネイティブ英語音声で出力 (edge-tts / Google TTS / espeak-ng)"""
    global AUDIO_OUTPUT_DEV
    if not text:
        return

    # moOde は英語音声合成エンジンで "mode" と発音
    speech_text = re.sub(r"\bmo+de\b", "mode", text, flags=re.IGNORECASE)
    speech_text = re.sub(r"\bmoOde\b", "mode", speech_text)

    with voice_lock:
        is_speaking_event.set()
        started_at = time.monotonic()
        print(f"\n🎙️ [English DJ] ネイティブ英語読み上げ開始: '{text}' (発音用: '{speech_text}')", flush=True)

        temp_dir = "/tmp" if os.name != "nt" else os.environ.get("TEMP", ".")
        temp_raw_wav = os.path.join(temp_dir, "voice_reply_raw.wav")
        temp_padded_wav = os.path.join(temp_dir, "voice_reply_en.wav")
        temp_mp3 = os.path.join(temp_dir, "voice_reply_en.mp3")

        # 前回のテンポラリファイルをクリア
        for p in [temp_mp3, temp_raw_wav, temp_padded_wav]:
            if os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass

        tts_success = False

        # =========================================================================
        # エンジン 1: edge-tts (Microsoft 超高音質ニューラル英語ラジオDJボイス)
        # =========================================================================
        try:
            print(f"🎙️ [English DJ] 1. Microsoft edge-tts 音声合成を試行中... (ボイス: {ENGLISH_VOICE})", flush=True)
            # 1-1. Python モジュールとしての edge_tts 呼び出し
            if edge_tts is not None:
                try:
                    async def _gen_edge_tts():
                        communicate = edge_tts.Communicate(speech_text, ENGLISH_VOICE)
                        await communicate.save(temp_mp3)
                    asyncio.run(_gen_edge_tts())
                    if os.path.exists(temp_mp3) and os.path.getsize(temp_mp3) > 200:
                        print("✅ [English DJ] edge_tts Python モジュールで MP3 生成成功", flush=True)
                except Exception as py_edge_err:
                    print(f"⚠️ [edge_tts Python] 生成エラー: {py_edge_err}", flush=True)

            # 1-2. Python インタプリタ経由 (sys.executable -m edge_tts) を試行
            if not os.path.exists(temp_mp3) or os.path.getsize(temp_mp3) < 200:
                cmd_py = [
                    sys.executable, "-m", "edge_tts",
                    "--voice", ENGLISH_VOICE,
                    "--text", speech_text,
                    "--write-media", temp_mp3,
                ]
                res_py = subprocess.run(cmd_py, capture_output=True, timeout=12)
                if res_py.returncode == 0 and os.path.exists(temp_mp3) and os.path.getsize(temp_mp3) > 200:
                    print("✅ [English DJ] python -m edge_tts で MP3 生成成功", flush=True)
                else:
                    err = res_py.stderr.decode("utf-8", errors="ignore").strip()
                    if err:
                        print(f"⚠️ [python -m edge_tts] 失敗: {err}", flush=True)

            # 1-3. システム CLI コマンドの edge-tts を試行
            if not os.path.exists(temp_mp3) or os.path.getsize(temp_mp3) < 200:
                if shutil.which("edge-tts"):
                    cmd_cli = [
                        "edge-tts",
                        "--voice", ENGLISH_VOICE,
                        "--text", speech_text,
                        "--write-media", temp_mp3,
                    ]
                    res_cli = subprocess.run(cmd_cli, capture_output=True, timeout=12)
                    if res_cli.returncode == 0 and os.path.exists(temp_mp3) and os.path.getsize(temp_mp3) > 200:
                        print("✅ [English DJ] edge-tts CLI で MP3 生成成功", flush=True)

            # edge-tts MP3 の ALSA 再生
            if os.path.exists(temp_mp3) and os.path.getsize(temp_mp3) > 200:
                if play_mp3_or_wav_audio(temp_mp3, temp_raw_wav, temp_padded_wav):
                    tts_success = True
                    print("🎉 [English DJ] edge-tts ネイティブ英語音声の再生完了！", flush=True)
        except Exception as e:
            print(f"⚠️ [English DJ] edge-tts 処理例外: {e}", flush=True)

        # =========================================================================
        # エンジン 2: Google Translate TTS (ゼロ依存・確実なネイティブ英語音声)
        # =========================================================================
        if not tts_success:
            try:
                print("🎙️ [English DJ] 2. Google Translate TTS (ゼロ依存 ネイティブ英語) を試行中...", flush=True)
                if fetch_google_tts_audio(speech_text, lang="en", output_file=temp_mp3):
                    if play_mp3_or_wav_audio(temp_mp3, temp_raw_wav, temp_padded_wav):
                        tts_success = True
                        print("🎉 [English DJ] Google TTS ネイティブ英語音声の再生完了！", flush=True)
            except Exception as g_err:
                print(f"⚠️ [English DJ] Google TTS 処理例外: {g_err}", flush=True)

        # =========================================================================
        # エンジン 3: espeak-ng / espeak (オフライン ネイティブ英語エンジン)
        # =========================================================================
        if not tts_success and os.name != "nt":
            for espeak_cmd in ["espeak-ng", "espeak"]:
                if shutil.which(espeak_cmd):
                    try:
                        print(f"🎙️ [English DJ] 3. {espeak_cmd} (オフライン英語) で音声生成中...", flush=True)
                        res = subprocess.run([espeak_cmd, "-v", "en-us", "-s", "140", "-w", temp_raw_wav, speech_text], capture_output=True)
                        if res.returncode == 0 and os.path.exists(temp_raw_wav):
                            add_silence_padding_to_wav(temp_raw_wav, temp_padded_wav, silence_sec=VOICE_PRE_SILENCE_SEC)
                            if play_wav_file(temp_padded_wav):
                                tts_success = True
                                print(f"🎉 [English DJ] {espeak_cmd} ネイティブ英語音声の再生完了！", flush=True)
                                break
                    except Exception as esp_err:
                        print(f"⚠️ [English DJ] {espeak_cmd} 例外: {esp_err}", flush=True)

        # =========================================================================
        # エンジン 4: Windows SAPI (Windowsローカル環境用)
        # =========================================================================
        if not tts_success and os.name == "nt":
            try:
                ps_cmd = f'Add-Type -AssemblyName System.speech; $speak = New-Object System.Speech.Synthesis.SpeechSynthesizer; $speak.Speak("{speech_text}")'
                subprocess.run(["powershell", "-Command", ps_cmd], capture_output=True, timeout=10)
                tts_success = True
            except Exception:
                pass

        if not tts_success:
            print("❌ [English DJ] ネイティブ英語音声の再生に失敗しました。", flush=True)
            print("💡 Jetson 端末で以下のいずれかを実行してください:", flush=True)
            print("   1. pip install edge-tts ffmpeg-python")
            print("   2. sudo apt update && sudo apt install -y ffmpeg mpg123 espeak-ng")

        print(f"🎙️ [English DJ] 音声処理サイクル終了（所要時間: {time.monotonic() - started_at:.1f}秒）\n", flush=True)

        for p in [temp_mp3, temp_raw_wav, temp_padded_wav]:
            if os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass
        is_speaking_event.clear()


# ==================== LLM 意図解析 (Ollama) ====================
def parse_intent_with_llm(user_text: str) -> Dict[str, Any]:
    """テキスト ➔ 意図抽出 (LLM - JSON構造化)"""
    started_at = time.monotonic()
    print(f"🤖 [LLM] 解析要求: '{user_text}'", flush=True)
    broadcast_process_status("llm", f"🤖 AIが選曲・意図を解釈中 ({LLM_MODEL}): 「{user_text}」")

    system_prompt = """あなたは音楽再生AIアシスタントです。ユーザーの要望を解釈し、moOde audioの操作コマンドと自然な日本語の返答を生成してください。
出力は必ず、説明文やMarkdownを含まない1つのJSONオブジェクトだけにしてください。

【出力形式】
{"action":"play_search"|"pause"|"stop"|"next"|"previous"|"unknown","query":"検索語","reply":"日本語返答"}

【ルール】
- 「〜をかけて」「〜を流して」「Jazz」「静かな曲」「ハイレゾ」 ➔ action: "play_search", query: "検索語(ジャズ/ロック/アーティスト名/Ambientなど)", reply: "〜を再生します。"
- 「止めて」「一時停止」「ストップ」 ➔ action: "pause", query: "", reply: "音楽を一時停止します。"
- 「次の曲」「スキップ」 ➔ action: "next", query: "", reply: "次の曲を再生します。"
- 「前の曲」「戻って」 ➔ action: "previous", query: "", reply: "前の曲に戻ります。"
- 雑談や一般的な質問 ➔ action: "unknown", query: "", reply: "内容に応じた親切で自然な日本語返答"
"""

    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ],
        "stream": False,
        "format": "json",
        "think": False,
        "keep_alive": "10m",
        "options": {
            "num_ctx": 2048,
            "temperature": 0,
            "num_predict": 192,
        },
    }

    try:
        response_json = http_post_json(OLLAMA_CHAT_URL, payload, timeout=60)
        message = response_json.get("message", {})
        response_text = message.get("content", "").strip()
        print(f"🤖 [LLM] 応答受信（{time.monotonic() - started_at:.1f}秒）: {response_text}", flush=True)

        cleaned_text = response_text.replace("```json", "").replace("```", "")
        cleaned_text = re.sub(r"<think>[\s\S]*?</think>", "", cleaned_text).strip()
        decoder = json.JSONDecoder()
        for start, character in enumerate(cleaned_text):
            if character != "{":
                continue
            try:
                command, _ = decoder.raw_decode(cleaned_text[start:])
                if isinstance(command, dict):
                    return command
            except json.JSONDecodeError:
                continue

    except Exception as e:
        print(f"⚠️ [LLM] Ollama接続エラー/フォールバック: {e}")

    # フォールバック（キーワードベースの簡易判定）
    if any(k in user_text for k in ["止め", "ストップ", "停止"]):
        return {"action": "pause", "query": "", "reply": "音楽を停止します。"}
    elif any(k in user_text for k in ["次", "スキップ"]):
        return {"action": "next", "query": "", "reply": "次の曲を再生します。"}
    elif any(k in user_text for k in ["前", "戻っ"]):
        return {"action": "previous", "query": "", "reply": "前の曲に戻ります。"}
    elif any(k in user_text for k in ["かけて", "流して", "再生", "聴きたい", "ジャズ", "jazz", "ロック", "クラシック", "ポップ"]):
        query = (
            user_text.replace("をかけて", "")
            .replace("を流して", "")
            .replace("を再生して", "")
            .replace("かけて", "")
            .replace("流して", "")
            .replace("再生して", "")
            .strip()
        )
        return {"action": "play_search", "query": query or user_text, "reply": f"{query or user_text} を再生します。"}

    return {
        "action": "unknown",
        "query": "",
        "reply": "ご用件を承りました。音楽のリクエストやご質問をどうぞ。",
    }


# ==================== 共通リクエスト処理エンジン ====================
def process_user_message(
    user_text: str,
    source: str = "chat",
    speak_voice: bool = True,
) -> Dict[str, Any]:
    """音声・Web Chat両方からのメッセージを処理するコア関数 (description読み上げ対応)"""
    print(f"\n📩 [Request] 処理開始 (from {source}): '{user_text}'", flush=True)
    broadcast_process_status("llm", f"🤖 リクエスト処理開始: 「{user_text}」")

    # 1. LLMによる意図抽出
    cmd = parse_intent_with_llm(user_text)

    # 2. moOde (MPD) 操作 & DB解説取得
    control_res = control_moode(cmd)

    reply_text = cmd.get("reply", "承知いたしました。")
    description = control_res.get("description", "")
    track_info = control_res.get("track_info") or {}

    # 3. 再生・スキップ時、曲紹介文を構築
    if cmd.get("action") in ("play_search", "next", "previous") and control_res.get("success"):
        if ANNOUNCE_LANGUAGE == "en":
            is_skip = cmd.get("action") in ("next", "previous")
            reply_text = build_english_track_announcement(track_info, is_next=False, is_skip=is_skip)
            print(f"🎙️ [English DJ ナレーション] {reply_text}", flush=True)
        else:
            t_title = track_info.get("title") or "楽曲"
            t_artist = track_info.get("artist")
            clean_desc = clean_text_for_speech(description, max_chars=100)
            prefix = "次の曲、" if cmd.get("action") == "next" else ("前の曲、" if cmd.get("action") == "previous" else "")

            if clean_desc:
                if t_artist and t_artist != "アーティスト未設定" and t_artist != "Unknown":
                    reply_text = f"{prefix}『{t_title}』（{t_artist}）を再生します。{clean_desc}"
                else:
                    reply_text = f"{prefix}『{t_title}』を再生します。{clean_desc}"
            else:
                if t_artist and t_artist != "アーティスト未設定" and t_artist != "Unknown":
                    reply_text = f"{prefix}『{t_title}』（{t_artist}）を再生します。"
                else:
                    reply_text = f"{prefix}『{t_title}』を再生します。"

            print(f"📖 [音声案内テキスト] {reply_text}", flush=True)

    # 4. 音声読み上げと moOde 音楽再生の順序制御（解説文を話し終えてから再生）
    needs_playback = control_res.get("needs_playback", False)

    def trigger_playback_start():
        """発話完了後に moOde の音楽再生を開始"""
        client = get_mpd_client()
        if client:
            try:
                broadcast_process_status("playing", "▶️ moOde 音楽再生をスタートしました", auto_idle_sec=3.5)
                client.play()
                client.close()
                client.disconnect()
                print("▶️ [moOde] 音声案内完了後に音楽再生を開始しました。", flush=True)
                broadcast_status()
            except Exception as e:
                print(f"⚠️ [moOde] 再生開始エラー: {e}")

    if speak_voice:
        def speak_and_play_flow():
            if ANNOUNCE_LANGUAGE == "en" and cmd.get("action") in ("play_search", "next", "previous"):
                broadcast_process_status("tts", "🎙️ DJ英語曲紹介音声を生成・再生中 (edge-tts)...")
                speak_english(reply_text)
            else:
                broadcast_process_status("tts", "🎙️ 曲紹介音声を合成・再生中 (VOICEVOX)...")
                speak(reply_text)
            if needs_playback:
                trigger_playback_start()
            else:
                broadcast_process_status("idle", "🎙️ 音声待機中 (「ヘイ、マスター」)")

        threading.Thread(target=speak_and_play_flow, daemon=True).start()
    else:
        # 音声読み上げなしの場合は即座に再生
        if needs_playback:
            trigger_playback_start()
        else:
            broadcast_process_status("idle", "🎙️ 音声待機中 (「ヘイ、マスター」)")

    # 5. 履歴に追加
    msg_record = {
        "sender": "assistant",
        "text": reply_text,
        "source": source,
        "action": cmd.get("action"),
        "query": cmd.get("query"),
        "track_info": track_info,
        "description": description,
        "tracks_added": control_res.get("tracks_added", []),
        "timestamp": time.strftime("%H:%M:%S"),
    }
    chat_history.append(msg_record)

    # 6. 全 WebSocket クライアントにブロードキャスト
    broadcast_event({
        "type": "chat_message",
        "message": msg_record,
    })

    # 最新ステータスもプッシュ
    broadcast_status()

    return {
        "action": cmd.get("action"),
        "query": cmd.get("query"),
        "reply": reply_text,
        "description": description,
        "track_info": track_info,
        "tracks_added": control_res.get("tracks_added", []),
        "control_success": control_res.get("success", False),
    }


# ==================== WebSocket リアルタイム配信 ====================
def broadcast_event(data: Dict[str, Any]):
    """接続中の全WebSocketにイベントを非同期送信"""
    if not active_websockets:
        return
    loop = None
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        pass

    if loop and loop.is_running():
        asyncio.run_coroutine_threadsafe(_async_broadcast(data), loop)
    else:
        threading.Thread(target=lambda: asyncio.run(_async_broadcast(data)), daemon=True).start()


async def _async_broadcast(data: Dict[str, Any]):
    msg_str = json.dumps(data, ensure_ascii=False)
    disconnected = []
    for ws in active_websockets:
        try:
            await ws.send_text(msg_str)
        except Exception:
            disconnected.append(ws)
    for ws in disconnected:
        if ws in active_websockets:
            active_websockets.remove(ws)


current_processing_state: Dict[str, Any] = {
    "step": "idle",
    "detail": "音声待機中 (「ヘイ、マスター」)",
    "timestamp": time.time(),
}


def broadcast_process_status(step: str, detail: str, auto_idle_sec: Optional[float] = None):
    """処理進行状況をリアルタイムで WebSocket クライアントに通知 (Web画面でのステータス表示用)"""
    global current_processing_state
    current_processing_state = {
        "step": step,
        "detail": detail,
        "timestamp": time.time(),
    }
    print(f"⚡ [Process Status] [{step.upper()}] {detail}", flush=True)
    broadcast_event({
        "type": "process_status",
        "step": step,
        "detail": detail,
        "timestamp": time.time(),
    })

    if auto_idle_sec:
        def _reset_to_idle():
            time.sleep(auto_idle_sec)
            if current_processing_state.get("step") == step:
                broadcast_process_status("idle", "音声待機中 (「ヘイ、マスター」)")
        threading.Thread(target=_reset_to_idle, daemon=True).start()


def broadcast_status():
    """現在の moOde 再生状態、音声ステータス、処理ステータス、および言語モードをプッシュ"""
    status = get_moode_status()
    broadcast_event({
        "type": "status_update",
        "player_status": status,
        "voice_status": voice_state,
        "process_status": current_processing_state,
        "language": ANNOUNCE_LANGUAGE,
    })


# ==================== 音声認識リスナー (Whisper + PyAudio) ====================
def init_whisper():
    global stt_model
    if WhisperModel is None:
        print("🎙️ [STT] faster_whisper がインポートされていないため、音声リスナーは無効化されます。")
        return
    if stt_model is None:
        print("Whisper STT モデルをロード中...", flush=True)
        try:
            stt_model = WhisperModel("small", device="cpu", compute_type="int8")
            print("✅ Whisper STT モデル準備完了", flush=True)
        except Exception as e:
            print(f"⚠️ Whisper 初期化失敗: {e}")


def record_audio_stream() -> Optional[io.BytesIO]:
    """マイクから音声を録音 (発話中は待機)"""
    if pyaudio is None:
        return None

    while is_speaking_event.is_set():
        time.sleep(0.2)

    p = pyaudio.PyAudio()

    input_device = None
    if INPUT_DEVICE_NAME:
        for device_index in range(p.get_device_count()):
            try:
                device_info = p.get_device_info_by_index(device_index)
                if (
                    device_info.get("maxInputChannels", 0) > 0
                    and INPUT_DEVICE_NAME.lower() in device_info["name"].lower()
                ):
                    input_device = device_info
                    break
            except Exception:
                continue

    if input_device is None and INPUT_DEVICE_INDEX is not None:
        try:
            input_device = p.get_device_info_by_index(INPUT_DEVICE_INDEX)
        except Exception:
            pass
    if input_device is None:
        try:
            input_device = p.get_default_input_device_info()
        except Exception:
            p.terminate()
            return None

    selected_input_index = int(input_device["index"])
    try:
        stream = p.open(
            format=FORMAT,
            channels=CHANNELS,
            rate=RATE,
            input=True,
            frames_per_buffer=CHUNK,
            input_device_index=selected_input_index,
        )
    except Exception:
        p.terminate()
        return None

    frames = []
    for _ in range(0, int(RATE / CHUNK * RECORD_SECONDS)):
        if is_speaking_event.is_set():
            break
        try:
            data = stream.read(CHUNK, exception_on_overflow=False)
            frames.append(data)
        except Exception:
            break

    sample_width = p.get_sample_size(FORMAT)
    try:
        stream.stop_stream()
        stream.close()
    except Exception:
        pass
    p.terminate()

    if not frames:
        return None

    wav_io = io.BytesIO()
    wf = wave.open(wav_io, "wb")
    wf.setnchannels(CHANNELS)
    wf.setsampwidth(sample_width)
    wf.setframerate(RATE)
    audio_bytes = b"".join(frames)
    wf.writeframes(audio_bytes)
    wav_io.seek(0)
    return wav_io


def speech_to_text(audio_stream: io.BytesIO) -> str:
    """音声 ➔ テキスト (STT)"""
    if stt_model is None or audio_stream is None:
        return ""
    try:
        segments, _ = stt_model.transcribe(
            audio_stream,
            language="ja",
            beam_size=1,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
            condition_on_previous_text=False,
        )
        text = "".join([segment.text for segment in segments]).strip()

        hallucinations = [
            "ご視聴ありがとうございました",
            "ご視聴ありがとうございます",
            "チャンネル登録",
            "高評価",
            "字幕",
            "おかりな",
        ]
        for h in hallucinations:
            if h in text:
                return ""
        return text
    except Exception as e:
        print(f"⚠️ STT エラー: {e}")
        return ""


def command_after_wake_word(text: str) -> Optional[str]:
    """ウェイクワードの後ろに続く発話だけを取り出す"""
    for wake_pattern in WAKE_WORD_PATTERNS:
        match = re.search(wake_pattern, text, flags=re.IGNORECASE)
        if match:
            cmd = text[match.end():].strip(" \t、,。！？!?")
            print(f"✅ ウェイクワード検出: {match.group(0)}", flush=True)
            return cmd
    return None


# ==================== 自動トラック監視 & 曲紹介ループ ====================
def run_track_watcher_loop():
    """moOde の再生進行を監視し、2曲目以降のトラック切り替わり時に自動で曲紹介を読み上げるスレッド"""
    global last_announced_file, last_announced_songid
    time.sleep(3.0)  # 起動初期化待ち
    print("🎧 [Watcher] トラック変更監視ループを開始しました。(2曲目以降の自動解説)", flush=True)

    while True:
        time.sleep(1.0)
        try:
            # システム発話中（TTS再生中）は監視スキップ
            if is_speaking_event.is_set():
                continue

            status_data = get_moode_status()
            if not status_data.get("connected"):
                continue

            play_state = status_data.get("state")
            song = status_data.get("song") or {}
            cur_file = song.get("file")
            cur_id = song.get("id")

            if not cur_file or play_state != "play":
                continue

            # 初回起動直後など、まだ何も記録されていない場合は記録のみ
            if last_announced_file is None:
                last_announced_file = cur_file
                last_announced_songid = cur_id
                continue

            # 同一曲判定（MPD ID または パス末尾/ファイル名が一致していれば同一曲とみなしスキップ）
            if is_same_track(cur_file, last_announced_file, cur_id, last_announced_songid):
                continue

            # トラックが実際に切り替わったことを検出！
            print(f"\n🔄 [Watcher] トラック切り替わり検知: {cur_file} (前曲={last_announced_file})", flush=True)
            last_announced_file = cur_file
            last_announced_songid = cur_id

            # 1. 音楽を一旦一時停止して、曲紹介を発話
            mpd_client = get_mpd_client()
            if mpd_client:
                try:
                    mpd_client.pause(1)
                    mpd_client.close()
                    mpd_client.disconnect()
                except Exception:
                    pass

            # 2. 曲情報と解説文を取得
            t_title = song.get("title") or "楽曲"
            t_artist = song.get("artist")
            broadcast_process_status("db", f"🔍 次の曲の解説を取得中 (SQLite): {t_title}")
            db_meta = find_track_metadata(file_path=cur_file, title=t_title, artist=t_artist)
            description_ja = db_meta.get("description_ja", "") if db_meta else ""
            description_en = db_meta.get("description_en", "") if db_meta else ""
            description = (description_en if ANNOUNCE_LANGUAGE == "en" and description_en else description_ja) or description_en or description_ja
            if db_meta:
                song["description"] = description
                song["description_ja"] = description_ja
                song["description_en"] = description_en
                song["genre"] = db_meta.get("genre", song.get("genre", ""))
                song["mood"] = db_meta.get("mood", song.get("mood", ""))

            if ANNOUNCE_LANGUAGE == "en":
                announce_text = build_english_track_announcement(song, is_next=True)
                print(f"🎙️ [Watcher 英語曲紹介] {announce_text}", flush=True)
            else:
                clean_desc = clean_text_for_speech(description, max_chars=100)
                if clean_desc:
                    if t_artist and t_artist != "アーティスト未設定" and t_artist != "Unknown":
                        announce_text = f"続いては、『{t_title}』、{t_artist}です。{clean_desc}"
                    else:
                        announce_text = f"続いては、『{t_title}』です。{clean_desc}"
                else:
                    if t_artist and t_artist != "アーティスト未設定" and t_artist != "Unknown":
                        announce_text = f"続いては、『{t_title}』、{t_artist}をお送りします。"
                    else:
                        announce_text = f"続いては、『{t_title}』をお送りします。"
                print(f"📖 [Watcher 自動曲紹介] {announce_text}", flush=True)

            # チャット履歴・Web UI にもプッシュ
            msg_record = {
                "sender": "assistant",
                "text": announce_text,
                "source": "auto_announcer",
                "action": "track_transition",
                "track_info": song,
                "description": description,
                "timestamp": time.strftime("%H:%M:%S"),
            }
            chat_history.append(msg_record)
            broadcast_event({"type": "chat_message", "message": msg_record})

            # 3. 発話を実行（排他ロックで安全に発話）
            if ANNOUNCE_LANGUAGE == "en":
                broadcast_process_status("tts", f"🎙️ DJ曲紹介アナウンス中: {t_title}")
                speak_english(announce_text)
            else:
                broadcast_process_status("tts", f"🎙️ 曲紹介アナウンス中: {t_title}")
                speak(announce_text)

            # 4. 発話完了後に音楽再生を再開
            mpd_client = get_mpd_client()
            if mpd_client:
                try:
                    broadcast_process_status("playing", f"▶️ 音楽再生を再開しました: {t_title}", auto_idle_sec=3.5)
                    mpd_client.play()
                    mpd_client.close()
                    mpd_client.disconnect()
                    print("▶️ [moOde] 2曲目の曲紹介完了後に音楽再生を再開しました。", flush=True)
                    broadcast_status()
                except Exception as e:
                    print(f"⚠️ [moOde] 再生再開エラー: {e}")

        except Exception as e:
            time.sleep(2.0)


def play_startup_greeting():
    """起動時の初期案内アナウンス（英語/日本語モード連動・Webチャット画面にも表示）"""
    try:
        if ANNOUNCE_LANGUAGE == "en":
            greeting = (
                "Hello! This is your moOde AI Assistant. "
                "Say 'Hey Master' to request a song, or use the web chat below to play your favorite music."
            )
            print(f"🎙️ [Greeting] 起動案内 (English): '{greeting}'", flush=True)
            msg_record = {
                "sender": "assistant",
                "text": greeting,
                "source": "system",
                "action": "greeting",
                "timestamp": time.strftime("%H:%M:%S"),
            }
            chat_history.append(msg_record)
            broadcast_event({"type": "chat_message", "message": msg_record})

            broadcast_process_status("tts", "🎙️ Speaking startup greeting...")
            speak_english(greeting)
            broadcast_process_status("idle", "🎙️ Ready for voice commands ('Hey Master')")
        else:
            greeting = (
                "こんにちは！moOde AI アシスタントです。"
                "マイクに向かって「ヘイ、マスター」と話しかけるか、下のチャット欄から曲やジャンルをリクエストしてください。"
            )
            print(f"🎙️ [Greeting] 起動案内 (Japanese): '{greeting}'", flush=True)
            msg_record = {
                "sender": "assistant",
                "text": greeting,
                "source": "system",
                "action": "greeting",
                "timestamp": time.strftime("%H:%M:%S"),
            }
            chat_history.append(msg_record)
            broadcast_event({"type": "chat_message", "message": msg_record})

            broadcast_process_status("tts", "🎙️ 起動案内を発話中...")
            speak(greeting)
            broadcast_process_status("idle", "🎙️ 音声待機中 (「ヘイ、マスター」)")
    except Exception as e:
        print(f"⚠️ [Greeting] 起動アナウンスエラー: {e}", flush=True)


def run_voice_loop():
    """音声待機・認識バックグラウンドスレッド"""
    play_startup_greeting()

    init_whisper()
    if stt_model is None or pyaudio is None:
        print("🎙️ 音声入力デバイスまたはモデルが利用できないため、音声リスナーを停止します。（Web Chatは利用可能です）")
        return

    print("🎙️ 音声アシスタント待機ループを開始しました。(「ヘイ、マスター」)", flush=True)

    while True:
        try:
            voice_state["is_listening"] = False
            voice_state["state"] = "idle"
            broadcast_event({"type": "voice_event", "event": "idle"})

            audio_data = record_audio_stream()
            if not audio_data:
                time.sleep(0.5)
                continue

            voice_state["is_listening"] = True
            voice_state["state"] = "recognizing"
            broadcast_event({"type": "voice_event", "event": "listening"})
            broadcast_process_status("stt", "🎙️ 音声を文字起こし中 (Whisper)...")

            wake_text = speech_to_text(audio_data)
            if not wake_text:
                broadcast_process_status("idle", "🎙️ 音声待機中 (「ヘイ、マスター」)")
                continue

            user_text = command_after_wake_word(wake_text)
            if user_text is None:
                broadcast_process_status("idle", "🎙️ 音声待機中 (「ヘイ、マスター」)")
                continue

            if not user_text:
                if ANNOUNCE_LANGUAGE == "en":
                    speak_english("Yes, I'm listening.")
                else:
                    speak("はい、どうぞ。")
                cmd_audio = record_audio_stream()
                broadcast_process_status("stt", "🎙️ コマンドを認識中 (Whisper)...")
                user_text = speech_to_text(cmd_audio)
                if not user_text:
                    broadcast_process_status("idle", "🎙️ 音声待機中 (「ヘイ、マスター」)")
                    continue

            print(f"👤 [Voice Input] ユーザー発言: {user_text}", flush=True)
            voice_state["last_text"] = user_text

            broadcast_event({
                "type": "chat_message",
                "message": {
                    "sender": "user",
                    "text": user_text,
                    "source": "voice",
                    "timestamp": time.strftime("%H:%M:%S"),
                },
            })

            process_user_message(user_text, source="voice", speak_voice=True)

        except Exception as e:
            print(f"⚠️ 音声ループ例外: {e}")
            time.sleep(1.0)


# ==================== FastAPI Web アプリケーション ====================
@asynccontextmanager
async def lifespan(app: FastAPI):
    yield


app = FastAPI(title="moOde AI Master", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ChatRequest(BaseModel):
    message: str
    speak: Optional[bool] = True


class ControlRequest(BaseModel):
    action: str
    value: Optional[Any] = None


@app.get("/")
async def get_index():
    index_path = os.path.join(os.path.dirname(__file__), "web", "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return JSONResponse({"message": "moOde AI Master Backend is running."})


@app.post("/api/chat")
async def api_chat(req: ChatRequest):
    """Web Chat からのメッセージ受付"""
    user_text = req.message.strip()
    if not user_text:
        return JSONResponse({"error": "Empty message"}, status_code=400)

    res = process_user_message(user_text, source="chat", speak_voice=bool(req.speak))
    return JSONResponse(res)


@app.get("/api/status")
async def api_status():
    """現在の moOde 再生情報 & システム状態を取得"""
    player_status = get_moode_status()
    return JSONResponse({
        "player_status": player_status,
        "voice_status": voice_state,
        "moode_ip": f"{MOODE_IP}:{MOODE_PORT}",
        "language": ANNOUNCE_LANGUAGE,
        "llm_model": LLM_MODEL,
    })


@app.post("/api/player/control")
async def api_player_control(req: ControlRequest):
    """プレイヤーの直接操作 (play, pause, next, previous, stop, volume)"""
    cmd = {"action": req.action, "value": req.value}
    res = control_moode(cmd)
    broadcast_status()
    return JSONResponse({"result": res, "status": get_moode_status()})


@app.get("/api/player/cover")
async def api_player_cover(file: Optional[str] = None, artist: Optional[str] = None, album: Optional[str] = None, title: Optional[str] = None):
    """現在再生中楽曲または指定楽曲のアルバムジャケット画像（Cover Art）を取得"""
    if not file and not artist and not album and not title:
        status = get_moode_status()
        song = status.get("song", {})
        file = song.get("file", "")
        artist = song.get("artist", "")
        album = song.get("album", "")
        title = song.get("title", "")

    img_bytes, media_type = get_album_cover_bytes(song_file=file or "", artist=artist or "", album=album or "", title=title or "")
    return Response(
        content=img_bytes,
        media_type=media_type,
        headers={"Cache-Control": "public, max-age=3600"},
    )


@app.get("/api/history")
async def api_history():
    """チャット履歴を取得"""
    return JSONResponse({"history": chat_history})


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    active_websockets.append(websocket)
    try:
        await websocket.send_text(
            json.dumps(
                {
                    "type": "status_update",
                    "player_status": get_moode_status(),
                    "voice_status": voice_state,
                },
                ensure_ascii=False,
            )
        )
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        if websocket in active_websockets:
            active_websockets.remove(websocket)
    except Exception:
        if websocket in active_websockets:
            active_websockets.remove(websocket)


# ==================== メイン実行 ====================
def main():
    global MOODE_IP, MOODE_PORT, AUDIO_OUTPUT_DEV, ANNOUNCE_LANGUAGE, LLM_MODEL

    parser = argparse.ArgumentParser(
        description="moOde AI Master (Voice & Web Chat Assistant with DJ Announcements)",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("--moode-ip", type=str, default=MOODE_IP, help="moOde (MPD) IP address (default: 192.168.68.198)")
    parser.add_argument("--moode-port", type=int, default=MOODE_PORT, help="moOde (MPD) port (default: 6600)")
    parser.add_argument("--model", "--llm-model", type=str, default=LLM_MODEL, help=f"Ollama LLM model name (default: {LLM_MODEL})")
    parser.add_argument("--audio-dev", type=str, default=None, help="Audio output ALSA device (e.g. plughw:1,0, default)")
    parser.add_argument(
        "--lang", "-l",
        type=str,
        default=None,
        help="Announcement language: 'en'/'english' (English DJ mode) or 'ja'/'japanese' (Japanese mode) [Default: en]",
    )
    parser.add_argument(
        "--en", "--english",
        action="store_true",
        help="Run in English FM DJ announcement mode (reads description_en in English)",
    )
    parser.add_argument(
        "--ja", "--japanese",
        action="store_true",
        help="Run in Japanese announcement mode (reads description_ja in Japanese)",
    )
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Web server host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8000, help="Web server port (default: 8000)")
    parser.add_argument("--no-voice", action="store_true", help="Disable microphone voice listener thread")
    args = parser.parse_args()

    MOODE_IP = args.moode_ip
    MOODE_PORT = args.moode_port
    LLM_MODEL = args.model

    # 分割した coverart モジュールにも moOde 接続先を同期
    coverart.MOODE_IP = MOODE_IP
    coverart.MOODE_PORT = MOODE_PORT

    # 言語モードの判定
    if args.ja:
        ANNOUNCE_LANGUAGE = "ja"
    elif args.en:
        ANNOUNCE_LANGUAGE = "en"
    elif args.lang:
        lang_val = args.lang.lower().strip()
        if lang_val in ("ja", "japanese", "jp", "nihongo", "日本語"):
            ANNOUNCE_LANGUAGE = "ja"
        elif lang_val in ("en", "english", "eng", "英語"):
            ANNOUNCE_LANGUAGE = "en"
        else:
            print(f"⚠️ 不明な言語指定 '{args.lang}' のため、デフォルトの英語モード (en) を使用します。")
            ANNOUNCE_LANGUAGE = "en"
    else:
        ANNOUNCE_LANGUAGE = "en"  # デフォルト: 英語DJモード

    if args.audio_dev:
        AUDIO_OUTPUT_DEV = args.audio_dev
    else:
        AUDIO_OUTPUT_DEV = detect_alsa_output_device(AUDIO_OUTPUT_NAME)

    lang_banner = "🎙️ ナレーション: 英語 DJ モード (English - description_en 読み上げ)" if ANNOUNCE_LANGUAGE == "en" else "🎙️ ナレーション: 日本語モード (Japanese - description_ja 読み上げ)"
    print("=" * 70)
    print(" 🎵 moOde AI Master (Voice & Web Chat Assistant)")
    print(f" 📡 moOde IP   : {MOODE_IP}:{MOODE_PORT}")
    print(f" 🤖 LLM モデル : {LLM_MODEL} (Ollama)")
    print(f" 🔊 音声出力   : {AUDIO_OUTPUT_DEV}")
    print(f" {lang_banner}")
    print(f" 🌐 Web UI     : http://{args.host}:{args.port} (ブラウザでアクセス)")
    print(f" 🎙️ 音声入力   : {'無効 (--no-voice)' if args.no_voice else '有効 (ヘイ、マスター)'}")
    print("=" * 70)

    # 自動トラック変更監視スレッド起動（2曲目以降の自動曲紹介）
    watcher_thread = threading.Thread(target=run_track_watcher_loop, daemon=True)
    watcher_thread.start()

    # 音声リスナースレッド起動
    if not args.no_voice:
        voice_thread = threading.Thread(target=run_voice_loop, daemon=True)
        voice_thread.start()
    else:
        # no-voice時も起動案内を発話（言語連動）
        threading.Thread(target=play_startup_greeting, daemon=True).start()

    # Webサーバー (FastAPI + Uvicorn) 起動
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
