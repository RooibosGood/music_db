import argparse
import asyncio
import io
import json
import os
import re
import sqlite3
import struct
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import wave
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

# 音声系ライブラリの安全なインポート (環境による欠落対策)
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


# ==================== 設定領域 ====================
MOODE_IP = "192.168.68.198"  # moOde (Raspberry Pi 5) の IP アドレス
MOODE_PORT = 6600

VOICEVOX_URL = "http://localhost:50021"
OLLAMA_CHAT_URL = "http://localhost:11434/api/chat"
SPEAKER_ID = 13  # 青山龍星（落ち着いた男性音声）
LLM_MODEL = "qwen3.5:2b"
AUDIO_OUTPUT_DEV = "plughw:0,0"  # Sennheiser SP 20 (カード0, デバイス0)
VOICE_PRE_SILENCE_SEC = 0.3  # 再生開始時の音切れ防止用
INPUT_DEVICE_NAME = "Sennheiser SP 20"  # PyAudioの表示名に含まれる文字列
INPUT_DEVICE_INDEX = None  # 名前で見つからない場合に使うPyAudio番号
DB_PATH = "music_meta.db"

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
voice_state = {
    "is_listening": False,
    "state": "idle",
    "last_text": "",
    "error": None,
}


# ==================== HTTP ヘルパー (標準ライブラリ利用) ====================
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


# ==================== SQLite DB ヘルパー ====================
def query_db_tracks(keyword: str, limit: int = 10) -> List[Dict[str, Any]]:
    """music_meta.db からジャンル・タイトル・アーティスト・ムード等で曲を検索"""
    if not os.path.exists(DB_PATH):
        return []
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        param = f"%{keyword}%"
        cur.execute(
            """
            SELECT id, title, artist, album, relative_path, file_path, genre, mood, energy_level, is_hires, description
            FROM tracks
            WHERE genre LIKE ? OR mood LIKE ? OR title LIKE ? OR artist LIKE ? OR description LIKE ?
            ORDER BY RANDOM()
            LIMIT ?;
        """,
            (param, param, param, param, param, limit),
        )
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows
    except Exception as e:
        print(f"⚠️ DB検索エラー: {e}")
        return []


def find_track_metadata(
    file_path: Optional[str] = None,
    title: Optional[str] = None,
    artist: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """music_meta.db からファイルパスやタイトル・アーティスト名で楽曲情報・解説文を取得"""
    if not os.path.exists(DB_PATH):
        return None
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # 1. ファイル名/相対パスで完全一致または部分一致検索
        if file_path:
            norm_path = file_path.replace("\\", "/")
            fname = norm_path.split("/")[-1]
            cur.execute(
                """
                SELECT id, title, artist, album, relative_path, file_path, genre, mood, energy_level, is_hires, description
                FROM tracks
                WHERE relative_path = ? OR file_path LIKE ? OR relative_path LIKE ?
                LIMIT 1;
            """,
                (norm_path, f"%{fname}", f"%{fname}"),
            )
            row = cur.fetchone()
            if row:
                conn.close()
                return dict(row)

        # 2. タイトルとアーティストで検索
        if title and artist and artist != "アーティスト未設定":
            cur.execute(
                """
                SELECT id, title, artist, album, relative_path, file_path, genre, mood, energy_level, is_hires, description
                FROM tracks
                WHERE title LIKE ? AND artist LIKE ?
                LIMIT 1;
            """,
                (f"%{title}%", f"%{artist}%"),
            )
            row = cur.fetchone()
            if row:
                conn.close()
                return dict(row)

        # 3. タイトルのみで検索
        if title and title != "未選択":
            cur.execute(
                """
                SELECT id, title, artist, album, relative_path, file_path, genre, mood, energy_level, is_hires, description
                FROM tracks
                WHERE title LIKE ?
                LIMIT 1;
            """,
                (f"%{title}%",),
            )
            row = cur.fetchone()
            if row:
                conn.close()
                return dict(row)

        conn.close()
    except Exception as e:
        print(f"⚠️ DB詳細取得エラー: {e}")
    return None


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

        # ハイレゾ情報の判定（サンプリングレート・ビット数）
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

        # DBから詳細・解説文 (description) を補完
        db_meta = find_track_metadata(file_path=song_file, title=song_title, artist=song_artist)
        description = db_meta.get("description", "") if db_meta else ""

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
    """MPD 経由で moOde audio を操作し、結果詳細および1曲目の解説文を返す"""
    action = command.get("action")
    query = command.get("query", "").strip()
    result = {
        "success": False,
        "action": action,
        "tracks_added": [],
        "track_info": None,
        "description": "",
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

    try:
        if action == "play_search":
            client.clear()
            search_results = []

            # 1. まず SQLite DB (music_meta.db) から検索
            db_tracks = query_db_tracks(query, limit=10)
            if db_tracks:
                for track in db_tracks:
                    rel = track.get("relative_path")
                    if rel:
                        search_results.append({
                            "file": rel,
                            "title": track.get("title"),
                            "artist": track.get("artist"),
                            "description": track.get("description", ""),
                        })

            # 2. DBで見つからない、またはMPDで直接検索する場合
            if not search_results:
                genre_map = {
                    "ジャズ": "Jazz", "jazz": "Jazz", "JAZZ": "Jazz",
                    "ロック": "Rock", "rock": "Rock",
                    "ポップ": "Pop", "ポップス": "Pop",
                    "クラシック": "Classical",
                    "ブルース": "Blues",
                }
                genre_query = genre_map.get(query, query)

                mpd_res = client.search("genre", genre_query)
                if not mpd_res and genre_query != query:
                    mpd_res = client.search("genre", query)
                if not mpd_res:
                    mpd_res = client.search("any", query)
                if mpd_res:
                    for s in mpd_res[:10]:
                        s_file = s.get("file", "")
                        s_title = s.get("title", s_file.split("/")[-1])
                        s_artist = s.get("artist", "")
                        # DBから解説文を取得
                        db_meta = find_track_metadata(file_path=s_file, title=s_title, artist=s_artist)
                        s_desc = db_meta.get("description", "") if db_meta else ""
                        search_results.append({
                            "file": s_file,
                            "title": s_title,
                            "artist": s_artist,
                            "description": s_desc,
                        })

            if search_results:
                added_count = 0
                for song in search_results:
                    try:
                        client.add(song["file"])
                        result["tracks_added"].append(song.get("title") or song["file"])
                        added_count += 1
                    except Exception:
                        fname = song["file"].split("/")[-1]
                        fallback_find = client.search("filename", fname)
                        if fallback_find:
                            client.add(fallback_find[0]["file"])
                            result["tracks_added"].append(fallback_find[0].get("title") or fname)
                            added_count += 1

                client.play()
                result["success"] = True

                # 1曲目のメタデータとdescriptionをセット
                first_track = search_results[0]
                result["track_info"] = {
                    "title": first_track.get("title"),
                    "artist": first_track.get("artist"),
                    "file": first_track.get("file"),
                }
                result["description"] = first_track.get("description", "")

                result["message"] = f"「{query}」に該当する楽曲 ({added_count}曲) を再生開始しました。"
                print(f"🎵 [moOde] '{query}' の再生を開始しました ({added_count}曲 追加)", flush=True)
                if result["description"]:
                    print(f"📖 [Description] {result['description']}", flush=True)
            else:
                result["message"] = f"「{query}」に該当する曲がライブラリに見つかりませんでした。"
                print(f"⚠️ [moOde] '{query}' に該当する曲が見つかりません", flush=True)

        elif action == "play":
            client.play()
            result["success"] = True
            result["message"] = "音楽の再生を再開しました。"
        elif action == "pause":
            client.pause(1)
            result["success"] = True
            result["message"] = "音楽を一時停止しました。"
        elif action == "stop":
            client.stop()
            result["success"] = True
            result["message"] = "音楽を停止しました。"
        elif action == "next":
            client.next()
            result["success"] = True
            result["message"] = "次の曲にスキップしました。"
        elif action == "previous":
            client.previous()
            result["success"] = True
            result["message"] = "前の曲に戻りました。"
        elif action == "volume":
            vol = command.get("value", 50)
            client.setvol(int(vol))
            result["success"] = True
            result["message"] = f"音量を {vol}% に設定しました。"

        client.close()
        client.disconnect()
    except Exception as e:
        print(f"❌ moOde 操作エラー: {e}")
        result["message"] = f"moOde 操作エラー: {e}"

    return result


# ==================== 音声合成 & 出力 (VOICEVOX) ====================
def speak(text: str):
    """VOICEVOX ➔ aplay で Jetson スピーカーから音声出力 (ALSA排他制御付き)"""
    if not text:
        return
    with voice_lock:
        is_speaking_event.set()
        started_at = time.monotonic()
        print(f"🔊 [VOICEVOX] 読み上げ開始: {text}", flush=True)
        try:
            # 1. audio_query
            query_url = f"{VOICEVOX_URL}/audio_query?text={urllib.parse.quote(text)}&speaker={SPEAKER_ID}"
            req_q = urllib.request.Request(query_url, method="POST")
            with urllib.request.urlopen(req_q, timeout=10) as res_q:
                query_data = res_q.read()

            # 2. synthesis
            synth_url = f"{VOICEVOX_URL}/synthesis?speaker={SPEAKER_ID}"
            req_s = urllib.request.Request(
                synth_url,
                data=query_data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req_s, timeout=20) as res_s:
                wav_bytes = res_s.read()

            temp_wav = "/tmp/voice_reply.wav" if os.name != "nt" else os.path.join(os.environ.get("TEMP", "."), "voice_reply.wav")
            with wave.open(io.BytesIO(wav_bytes), "rb") as source_wav:
                with wave.open(temp_wav, "wb") as output_wav:
                    output_wav.setparams(source_wav.getparams())
                    silence_frames = int(source_wav.getframerate() * VOICE_PRE_SILENCE_SEC)
                    output_wav.writeframes(
                        b"\0" * silence_frames * source_wav.getnchannels() * source_wav.getsampwidth()
                    )
                    output_wav.writeframes(source_wav.readframes(source_wav.getnframes()))

            if os.name != "nt":
                played = False
                dev_candidates = [AUDIO_OUTPUT_DEV, "default", "sysdefault"]
                for dev in dev_candidates:
                    try:
                        res = subprocess.run(["aplay", "-D", dev, "-q", temp_wav], capture_output=True, timeout=15)
                        if res.returncode == 0:
                            played = True
                            break
                    except Exception:
                        continue
                if not played:
                    subprocess.run(["aplay", "-q", temp_wav], check=False)
            else:
                try:
                    import winsound
                    winsound.PlaySound(temp_wav, winsound.SND_FILENAME)
                except Exception:
                    pass

            if os.path.exists(temp_wav):
                os.remove(temp_wav)

            print(f"🔊 [VOICEVOX] 音声出力完了（{time.monotonic() - started_at:.1f}秒）", flush=True)
        except Exception as e:
            print(f"⚠️ [VOICEVOX] 発話スキップ: {e}")
        finally:
            is_speaking_event.clear()


# ==================== LLM 意図解析 (Ollama) ====================
def parse_intent_with_llm(user_text: str) -> Dict[str, Any]:
    """テキスト ➔ 意図抽出 (LLM - JSON構造化)"""
    started_at = time.monotonic()
    print(f"🤖 [LLM] 解析要求: '{user_text}'", flush=True)

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

    # 1. LLMによる意図抽出
    cmd = parse_intent_with_llm(user_text)

    # 2. moOde (MPD) 操作 & DB解説取得
    control_res = control_moode(cmd)

    reply_text = cmd.get("reply", "承知いたしました。")
    description = control_res.get("description", "")
    track_info = control_res.get("track_info") or {}

    # 3. 再生時、DBに description が存在すれば返答・音声読み上げ文に組み込む
    if cmd.get("action") == "play_search" and control_res.get("success"):
        t_title = track_info.get("title")
        t_artist = track_info.get("artist")

        if description:
            if t_title and t_artist and t_artist != "アーティスト未設定":
                reply_text = f"『{t_title}』（{t_artist}）を再生します。{description}"
            elif t_title:
                reply_text = f"『{t_title}』を再生します。{description}"
            else:
                reply_text = f"音楽を再生します。{description}"
        else:
            if t_title and t_artist and t_artist != "アーティスト未設定":
                reply_text = f"『{t_title}』（{t_artist}）を再生します。"
            elif t_title:
                reply_text = f"『{t_title}』を再生します。"

    # 4. 音声読み上げ (VOICEVOX) - speak_voiceが有効な場合
    if speak_voice:
        threading.Thread(target=speak, args=(reply_text,), daemon=True).start()

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


def broadcast_status():
    """現在の moOde 再生状態と音声ステータスをプッシュ"""
    status = get_moode_status()
    broadcast_event({
        "type": "status_update",
        "player_status": status,
        "voice_status": voice_state,
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


def run_voice_loop():
    """音声待機・認識バックグラウンドスレッド"""
    init_whisper()
    if stt_model is None or pyaudio is None:
        print("🎙️ 音声入力デバイスまたはモデルが利用できないため、音声リスナーを停止します。（Web Chatは利用可能です）")
        return

    print("🎙️ 音声アシスタント待機ループを開始しました。(「ヘイ、マスター」)", flush=True)
    try:
        speak("「ヘイ、マスター」と呼びかけてください。")
    except Exception:
        pass

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

            wake_text = speech_to_text(audio_data)
            if not wake_text:
                continue

            user_text = command_after_wake_word(wake_text)
            if user_text is None:
                continue

            if not user_text:
                speak("はい、どうぞ。")
                cmd_audio = record_audio_stream()
                user_text = speech_to_text(cmd_audio)
                if not user_text:
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
    })


@app.post("/api/player/control")
async def api_player_control(req: ControlRequest):
    """プレイヤーの直接操作 (play, pause, next, previous, stop, volume)"""
    cmd = {"action": req.action, "value": req.value}
    res = control_moode(cmd)
    broadcast_status()
    return JSONResponse({"result": res, "status": get_moode_status()})


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
    global MOODE_IP, MOODE_PORT

    parser = argparse.ArgumentParser(description="moOde AI Master (Voice & Web Chat Assistant)")
    parser.add_argument("--moode-ip", type=str, default=MOODE_IP, help="moOde (MPD) IP address")
    parser.add_argument("--moode-port", type=int, default=MOODE_PORT, help="moOde (MPD) port")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Web server host")
    parser.add_argument("--port", type=int, default=8000, help="Web server port")
    parser.add_argument("--no-voice", action="store_true", help="Disable microphone voice listener thread")
    args = parser.parse_args()

    MOODE_IP = args.moode_ip
    MOODE_PORT = args.moode_port

    print("=" * 60)
    print(" 🎵 moOde AI Master (Voice & Web Chat Assistant)")
    print(f" 📡 moOde IP : {MOODE_IP}:{MOODE_PORT}")
    print(f" 🌐 Web UI   : http://{args.host}:{args.port} (ブラウザでアクセス)")
    print(f" 🎙️ 音声入力 : {'無効 (--no-voice)' if args.no_voice else '有効 (ヘイ、マスター)'}")
    print("=" * 60)

    # 音声リスナースレッド起動
    if not args.no_voice:
        voice_thread = threading.Thread(target=run_voice_loop, daemon=True)
        voice_thread.start()

    # Webサーバー (FastAPI + Uvicorn) 起動
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
