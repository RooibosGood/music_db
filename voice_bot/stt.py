"""moOde 音声ボット 音声認識・録音・音声ループモジュール。"""

import io
import re
import time
import wave
from typing import Any, Optional

from . import config
from . import state
from . import tts
from .broadcaster import (
    broadcast_event,
    broadcast_process_status,
)
from .llm import process_user_message
from .watcher import play_startup_greeting

try:
    import pyaudio
except ImportError:
    pyaudio = None

try:
    from faster_whisper import WhisperModel
except ImportError:
    WhisperModel = None

stt_model: Optional[Any] = None


def init_whisper():
    """Whisper STT モデルの初期化"""
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


def open_input_stream(p: Any) -> Optional[Any]:
    """マイク入力ストリームをオープン（永続利用）"""
    if p is None:
        return None

    input_device = None
    if config.INPUT_DEVICE_NAME:
        for device_index in range(p.get_device_count()):
            try:
                device_info = p.get_device_info_by_index(device_index)
                if (
                    device_info.get("maxInputChannels", 0) > 0
                    and config.INPUT_DEVICE_NAME.lower() in device_info["name"].lower()
                ):
                    input_device = device_info
                    break
            except Exception:
                continue

    if input_device is None and config.INPUT_DEVICE_INDEX is not None:
        try:
            input_device = p.get_device_info_by_index(config.INPUT_DEVICE_INDEX)
        except Exception:
            pass

    if input_device is None:
        try:
            input_device = p.get_default_input_device_info()
        except Exception:
            return None

    selected_input_index = int(input_device["index"])
    dev_name = input_device.get("name", f"Device #{selected_input_index}")
    print(f"🎙️ [STT] マイク入力ストリームを開設: {dev_name} (Index: {selected_input_index})", flush=True)

    try:
        stream = p.open(
            format=config.FORMAT,
            channels=config.CHANNELS,
            rate=config.RATE,
            input=True,
            frames_per_buffer=config.CHUNK,
            input_device_index=selected_input_index,
        )
        return stream
    except Exception as e:
        print(f"⚠️ [STT] マイクストリームオープン失敗: {e}")
        return None


def record_audio_from_stream(stream: Any, p: Any, record_seconds: Optional[int] = None) -> Optional[io.BytesIO]:
    """常時オープンされたマイクストリームから指定秒数録音 (発話中はバッファを破棄ドレイン)"""
    if stream is None or p is None:
        return None

    duration = record_seconds or config.RECORD_SECONDS
    num_frames = int(config.RATE / config.CHUNK * duration)

    # システム発話中はマイク入力を読み捨て（ドレイン）して待機
    while tts.is_speaking_event.is_set():
        try:
            stream.read(config.CHUNK, exception_on_overflow=False)
        except Exception:
            pass
        time.sleep(0.1)

    frames = []
    for _ in range(0, num_frames):
        if tts.is_speaking_event.is_set():
            break
        try:
            data = stream.read(config.CHUNK, exception_on_overflow=False)
            frames.append(data)
        except Exception:
            break

    if not frames:
        return None

    sample_width = p.get_sample_size(config.FORMAT)
    wav_io = io.BytesIO()
    wf = wave.open(wav_io, "wb")
    wf.setnchannels(config.CHANNELS)
    wf.setsampwidth(sample_width)
    wf.setframerate(config.RATE)
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
    for wake_pattern in config.WAKE_WORD_PATTERNS:
        match = re.search(wake_pattern, text, flags=re.IGNORECASE)
        if match:
            cmd = text[match.end():].strip(" \t、,。！？!?")
            print(f"✅ ウェイクワード検出: {match.group(0)}", flush=True)
            return cmd
    return None


def run_voice_loop():
    """音声待機・認識バックグラウンドスレッド（ストリーム常時オープンによるポップノイズ防止）"""
    play_startup_greeting()

    init_whisper()
    if stt_model is None or pyaudio is None:
        print("🎙️ 音声入力デバイスまたはモデルが利用できないため、音声リスナーを停止します。（Web Chatは利用可能です）")
        return

    p = pyaudio.PyAudio()
    stream = open_input_stream(p)
    if stream is None:
        print("🎙️ マイク入力ストリームの初期化に失敗したため、音声リスナーを停止します。")
        try:
            p.terminate()
        except Exception:
            pass
        return

    print("🎙️ 音声アシスタント待機ループを開始しました。(「ヘイ、マスター」)", flush=True)

    try:
        while True:
            try:
                state.voice_state["is_listening"] = False
                state.voice_state["state"] = "idle"
                broadcast_event({"type": "voice_event", "event": "idle"})

                state.voice_state["is_listening"] = True
                state.voice_state["state"] = "listening"
                broadcast_event({"type": "voice_event", "event": "listening"})

                audio_data = record_audio_from_stream(stream, p, record_seconds=config.RECORD_SECONDS)
                if not audio_data:
                    time.sleep(0.3)
                    continue

                state.voice_state["state"] = "recognizing"
                broadcast_event({"type": "voice_event", "event": "recognizing"})
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
                    if config.ANNOUNCE_LANGUAGE == "en":
                        tts.speak_english("Yes, I'm listening.")
                    else:
                        tts.speak("はい、どうぞ。")
                    cmd_audio = record_audio_from_stream(stream, p, record_seconds=config.RECORD_SECONDS)
                    broadcast_process_status("stt", "🎙️ コマンドを認識中 (Whisper)...")
                    user_text = speech_to_text(cmd_audio)
                    if not user_text:
                        broadcast_process_status("idle", "🎙️ 音声待機中 (「ヘイ、マスター」)")
                        continue

                print(f"👤 [Voice Input] ユーザー発言: {user_text}", flush=True)
                state.voice_state["last_text"] = user_text

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
                # ストリーム異常時の自己再オープン
                try:
                    if stream and not stream.is_active():
                        stream = open_input_stream(p)
                except Exception:
                    pass

    finally:
        try:
            if stream:
                stream.stop_stream()
                stream.close()
            if p:
                p.terminate()
        except Exception:
            pass
