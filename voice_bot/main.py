import argparse
import asyncio
import io
import json
import os
import re
import threading
import time
import urllib.request
import wave
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel

# 分割モジュール (db.py / coverart.py / mpd_client.py / tts.py)
from . import coverart
from . import mpd_client
from . import tts
from .coverart import get_album_cover_bytes
from db import find_track_metadata
from .mpd_client import control_moode, get_moode_status
from .tts import (
    build_english_track_announcement,
    clean_text_for_speech,
    detect_alsa_output_device,
    speak,
    speak_english,
)

# 音声系ライブラリの安全なインポート
try:
    import pyaudio
except ImportError:
    pyaudio = None

try:
    from faster_whisper import WhisperModel
except ImportError:
    WhisperModel = None


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
# AUDIO_OUTPUT_DEV は tts モジュールに集約 (tts.AUDIO_OUTPUT_DEV)
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
# voice_lock / is_speaking_event / last_announced_file / last_announced_songid は
# mpd_client.py / tts.py に移動（main() で参照を共有する）
stt_model = None
chat_history: List[Dict[str, Any]] = []
active_websockets: List[WebSocket] = []
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


# get_mpd_client / get_moode_status / control_moode は mpd_client.py に移動
# TTS関連 (speak / speak_english / detect_alsa_output_device 等) は tts.py に移動



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
        mpd_cli = mpd_client.get_mpd_client()
        if mpd_cli:
            try:
                broadcast_process_status("playing", "▶️ moOde 音楽再生をスタートしました", auto_idle_sec=3.5)
                mpd_cli.play()
                mpd_cli.close()
                mpd_cli.disconnect()
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

    while tts.is_speaking_event.is_set():
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
        if tts.is_speaking_event.is_set():
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
    time.sleep(3.0)  # 起動初期化待ち
    print("🎧 [Watcher] トラック変更監視ループを開始しました。(2曲目以降の自動解説)", flush=True)

    while True:
        time.sleep(1.0)
        try:
            # システム発話中（TTS再生中）は監視スキップ
            if tts.is_speaking_event.is_set():
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
            if mpd_client.last_announced_file is None:
                mpd_client.last_announced_file = cur_file
                mpd_client.last_announced_songid = cur_id
                continue

            # 同一曲判定（MPD ID または パス末尾/ファイル名が一致していれば同一曲とみなしスキップ）
            if is_same_track(cur_file, mpd_client.last_announced_file, cur_id, mpd_client.last_announced_songid):
                continue

            # トラックが実際に切り替わったことを検出！
            print(f"\n🔄 [Watcher] トラック切り替わり検知: {cur_file} (前曲={mpd_client.last_announced_file})", flush=True)
            mpd_client.last_announced_file = cur_file
            mpd_client.last_announced_songid = cur_id

            # 1. 音楽を一旦一時停止して、曲紹介を発話
            mpd_cli = mpd_client.get_mpd_client()
            if mpd_cli:
                try:
                    mpd_cli.pause(1)
                    mpd_cli.close()
                    mpd_cli.disconnect()
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
            mpd_cli = mpd_client.get_mpd_client()
            if mpd_cli:
                try:
                    broadcast_process_status("playing", f"▶️ 音楽再生を再開しました: {t_title}", auto_idle_sec=3.5)
                    mpd_cli.play()
                    mpd_cli.close()
                    mpd_cli.disconnect()
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
    project_root = os.path.dirname(os.path.dirname(__file__))
    index_path = os.path.join(project_root, "web", "index.html")
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
    global MOODE_IP, MOODE_PORT, ANNOUNCE_LANGUAGE, LLM_MODEL

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

    # 分割したモジュールに設定値・共有状態を同期（起動時に一度だけ）
    coverart.MOODE_IP = MOODE_IP
    coverart.MOODE_PORT = MOODE_PORT

    mpd_client.MOODE_IP = MOODE_IP
    mpd_client.MOODE_PORT = MOODE_PORT
    mpd_client.ANNOUNCE_LANGUAGE = ANNOUNCE_LANGUAGE
    mpd_client.broadcast_process_status = broadcast_process_status

    tts.LLM_MODEL = LLM_MODEL

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

    # 言語確定後に mpd_client 側も再同期
    mpd_client.ANNOUNCE_LANGUAGE = ANNOUNCE_LANGUAGE

    if args.audio_dev:
        tts.AUDIO_OUTPUT_DEV = args.audio_dev
    else:
        tts.AUDIO_OUTPUT_DEV = detect_alsa_output_device(AUDIO_OUTPUT_NAME)

    lang_banner = "🎙️ ナレーション: 英語 DJ モード (English - description_en 読み上げ)" if ANNOUNCE_LANGUAGE == "en" else "🎙️ ナレーション: 日本語モード (Japanese - description_ja 読み上げ)"
    print("=" * 70)
    print(" 🎵 moOde AI Master (Voice & Web Chat Assistant)")
    print(f" 📡 moOde IP   : {MOODE_IP}:{MOODE_PORT}")
    print(f" 🤖 LLM モデル : {LLM_MODEL} (Ollama)")
    print(f" 🔊 音声出力   : {tts.AUDIO_OUTPUT_DEV}")
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
