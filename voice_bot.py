import io
import json
import re
import os
import subprocess
import struct
import time
import wave
from faster_whisper import WhisperModel
from mpd import MPDClient
import pyaudio
import requests

# ==================== 設定領域 ====================
# moOde (Raspberry Pi 5) の IP アドレスを設定してください
MOODE_IP = "192.168.68.198"  # ★ご自身の環境のIPに変更
MOODE_PORT = 6600

VOICEVOX_URL = "http://localhost:50021"
OLLAMA_URL = "http://localhost:11434/api/generate"
OLLAMA_CHAT_URL = "http://localhost:11434/api/chat"
SPEAKER_ID = 13  # 青山龍星（落ち着いた男性音声）
LLM_MODEL = "qwen3.5:2b"
AUDIO_OUTPUT_DEV = "plughw:0,0"  # Sennheiser SP 20 (カード0, デバイス0)
VOICE_PRE_SILENCE_SEC = 0.3  # 再生開始時の音切れ防止用
INPUT_DEVICE_NAME = "Sennheiser SP 20"  # PyAudioの表示名に含まれる文字列
INPUT_DEVICE_INDEX = None  # 名前で見つからない場合に使うPyAudio番号
WAKE_WORD_PATTERNS = (
    r"ヘイ[\s、,。！？!?]*マスター",
    r"へい[\s、,。！？!?]*ますたー",
    r"hey[\s、,。！？!?]*master",
)

# マイク録音設定
FORMAT = pyaudio.paInt16
CHANNELS = 1
RATE = 16000
CHUNK = 1024
RECORD_SECONDS = 4
# ==================================================

# Whisper STT (CPU + int8量子化で高速動作)
print("Whisper STT モデルを初期化中...")
stt_model = WhisperModel("small", device="cpu", compute_type="int8")


def record_audio():
    """マイクから音声を録音"""
    p = pyaudio.PyAudio()
    print("🎙️ 使用可能な入力デバイス:", flush=True)
    for device_index in range(p.get_device_count()):
        device_info = p.get_device_info_by_index(device_index)
        if device_info.get("maxInputChannels", 0) > 0:
            print(
                f"  [{device_index}] {device_info['name']} "
                f"(channels={device_info['maxInputChannels']})",
                flush=True,
            )

    input_device = None
    if INPUT_DEVICE_NAME:
        for device_index in range(p.get_device_count()):
            device_info = p.get_device_info_by_index(device_index)
            if (
                device_info.get("maxInputChannels", 0) > 0
                and INPUT_DEVICE_NAME.lower() in device_info["name"].lower()
            ):
                input_device = device_info
                break

    if input_device is None and INPUT_DEVICE_INDEX is not None:
        input_device = p.get_device_info_by_index(INPUT_DEVICE_INDEX)
    if input_device is None:
        input_device = p.get_default_input_device_info()

    selected_input_index = int(input_device["index"])
    print(
        f"🎙️ 使用する入力デバイス: {selected_input_index} / "
        f"{input_device['name']}",
        flush=True,
    )
    stream = p.open(
        format=FORMAT,
        channels=CHANNELS,
        rate=RATE,
        input=True,
        frames_per_buffer=CHUNK,
        input_device_index=selected_input_index,
    )

    print("\n🎤 お話ください（録音中...）", flush=True)
    frames = []
    for _ in range(0, int(RATE / CHUNK * RECORD_SECONDS)):
        data = stream.read(CHUNK)
        frames.append(data)

    print("🎙️ 録音終了。音声認識を開始します...", flush=True)
    sample_width = p.get_sample_size(FORMAT)
    stream.stop_stream()
    stream.close()
    p.terminate()

    wav_io = io.BytesIO()
    wf = wave.open(wav_io, "wb")
    wf.setnchannels(CHANNELS)
    wf.setsampwidth(sample_width)
    wf.setframerate(RATE)
    audio_bytes = b"".join(frames)
    wf.writeframes(audio_bytes)
    wav_io.seek(0)
    sample_count = len(audio_bytes) // 2
    sample_values = struct.unpack(f"<{sample_count}h", audio_bytes)
    peak = max((abs(value) for value in sample_values), default=0)
    print(f"🎙️ 録音レベル: peak={peak}/32767", flush=True)
    return wav_io


def speech_to_text(audio_stream):
    """音声 ➔ テキスト (STT)"""
    started_at = time.monotonic()
    print("📝 [1/4] Whisperで音声を文字に変換中...", flush=True)
    segments, _ = stt_model.transcribe(
        audio_stream,
        language="ja",
        beam_size=1,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500},
        condition_on_previous_text=False,
    )
    text = "".join([segment.text for segment in segments]).strip()
    print(
        f"📝 [1/4] 音声認識完了（{time.monotonic() - started_at:.1f}秒）: "
        f"{text or '(聞き取れませんでした)'}",
        flush=True,
    )

    # Whisperの代表的な無音ハルシネーションを無視・除外する
    hallucination_words = [
        "ご視聴ありがとうございました",
        "ご視聴ありがとうございます",
        "チャンネル登録",
        "高評価",
        "字幕",
        "おかりな",
    ]

    for word in hallucination_words:
        if word in text:
            print(f"⚠️ [STT] ハルシネーション（無音）を検知し無視しました: '{text}'")
            return ""

    return text


def command_after_wake_word(text):
    """ウェイクワードの後ろに続く発話だけを取り出す"""
    for wake_pattern in WAKE_WORD_PATTERNS:
        match = re.search(wake_pattern, text, flags=re.IGNORECASE)
        if match:
            command = text[match.end():].strip(" \t、,。！？!?")
            print(f"✅ ウェイクワード検出: {match.group(0)}", flush=True)
            return command
    return None



def parse_intent_with_llm(user_text):
    """テキスト ➔ 意図抽出 (LLM - トークン制限緩和版)"""
    started_at = time.monotonic()
    print(f"🤖 [2/4] Ollama ({LLM_MODEL}) に問い合わせ中...", flush=True)

    system_prompt = """あなたは日本語の音声アシスタントです。音楽操作が必要なときだけmoOdeを操作し、それ以外は普通に会話してください。
出力は必ず、説明文やMarkdownを含まない1つのJSONオブジェクトだけにしてください。

【出力形式】
{"action":"play_search"|"pause"|"stop"|"next"|"unknown","query":"検索ワード","reply":"日本語返答"}

【ルール】
- 「〜をかけて」「〜を流して」「静かな曲」 ➔ action: "play_search", query: "検索語(Ambient/Jazzなど)", reply: "〜を再生します。"
- 「止めて」「ストップ」 ➔ action: "pause", query: "", reply: "音楽を停止します。"
- 「はい」「こんにちは」などの雑談・日常会話 ➔ action: "unknown", query: "", reply: "こんにちは。今日はどうしましたか？"
- 音楽以外の質問や依頼 ➔ action: "unknown", query: "", reply: "内容に応じた自然な日本語で回答してください。"
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
        # Jetsonではモデルの初回ロードに時間がかかるため短すぎるタイムアウトにしない
        res = requests.post(OLLAMA_CHAT_URL, json=payload, timeout=90)
        res.raise_for_status()

        response_json = res.json()
        message = response_json.get("message", {})
        response_text = message.get("content", "").strip()
        print(
            f"🤖 [2/4] LLM応答受信（{time.monotonic() - started_at:.1f}秒）",
            flush=True,
        )
        if not response_text:
            print(
                f"⚠️ [LLM] message.contentが空です。messageのキー: "
                f"{list(message.keys())}",
                flush=True,
            )
        print(f"\n🔍 [DEBUG] LLM Output Raw:\n{response_text}\n")

        # Qwenが思考過程やコードブロックを返しても、JSON本体だけを取り出す
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

        print("❌ [ERROR] 完全な JSON 構造を切り出せませんでした。")

    except requests.exceptions.Timeout:
        print("❌ [ERROR] Ollama API タイムアウト")
    except json.JSONDecodeError as e:
        print(f"❌ [ERROR] JSONパース失敗: {e}")
    except Exception as e:
        print(f"❌ [ERROR] 予期せぬエラー: {e}")

    return {
        "action": "unknown",
        "query": "",
        "reply": "すみません、うまく理解できませんでした。",
    }


def control_moode(command):
    """MPD 経由で moOde audio を操作"""
    action = command.get("action")
    query = command.get("query")

    if action == "unknown":
        print("🎵 [4/4] 音楽操作はありません。待機に戻ります。", flush=True)
        return

    print(f"🎵 [4/4] moOdeを操作中（action={action}）...", flush=True)

    try:
        client = MPDClient()
        client.timeout = 5
        client.connect(MOODE_IP, MOODE_PORT)

        if action == "play_search":
            client.clear()
            # moOde 内のライブラリ（ジャンル等）を検索してプレイリストに追加
            search_results = client.search("genre", query)
            if not search_results:
                # ジャンルで見つからない場合はタイトルやアーティストから曖昧検索
                search_results = client.search("any", query)

            if search_results:
                for song in search_results[:10]:  # 上位10曲を追加
                    client.add(song["file"])
                client.play()
                print(f"🎵 [4/4] moOde: '{query}' に該当する曲を再生開始", flush=True)
            else:
                print(
                    f"⚠️ moOde: '{query}' に該当する曲がライブラリに見つかりません"
                    , flush=True
                )

        elif action == "pause":
            client.pause(1)
        elif action == "stop":
            client.stop()
        elif action == "next":
            client.next()

        print("🎵 [4/4] moOde操作完了。待機に戻ります。", flush=True)

        client.close()
        client.disconnect()
    except Exception as e:
        print(f"❌ moOde 接続エラー: {e}")


def speak(text):
    """VOICEVOX ➔ aplay で音声出力"""
    if not text:
        print("🔊 [3/4] 読み上げる返答がありません。", flush=True)
        return
    started_at = time.monotonic()
    print(f"🔊 [3/4] VOICEVOXで読み上げ中: {text}", flush=True)
    q_res = requests.post(
        f"{VOICEVOX_URL}/audio_query",
        params={"text": text, "speaker": SPEAKER_ID},
        timeout=30,
    )
    s_res = requests.post(
        f"{VOICEVOX_URL}/synthesis",
        params={"speaker": SPEAKER_ID},
        headers={"Content-Type": "application/json"},
        data=json.dumps(q_res.json()),
        timeout=30,
    )

    temp_wav = "/tmp/voice_reply.wav"
    with wave.open(io.BytesIO(s_res.content), "rb") as source_wav:
        with wave.open(temp_wav, "wb") as output_wav:
            output_wav.setparams(source_wav.getparams())
            silence_frames = int(
                source_wav.getframerate() * VOICE_PRE_SILENCE_SEC
            )
            output_wav.writeframes(
                b"\0"
                * silence_frames
                * source_wav.getnchannels()
                * source_wav.getsampwidth()
            )
            output_wav.writeframes(
                source_wav.readframes(source_wav.getnframes())
            )

    subprocess.run(["aplay", "-D", AUDIO_OUTPUT_DEV, "-q", temp_wav], check=True)
    if os.path.exists(temp_wav):
        os.remove(temp_wav)
    print(
        f"🔊 [3/4] 音声出力完了（{time.monotonic() - started_at:.1f}秒）",
        flush=True,
    )


# ==================== メインループ ====================
if __name__ == "__main__":
    print("=== moOde 音声操作AIシステム 起動 ===")
    try:
        speak("「ヘイ、マスター」と呼びかけてください。")
        while True:
            # 1. ウェイクワードを待機
            print("🎧 ウェイクワード待機中（ヘイ、マスター）...", flush=True)
            audio_data = record_audio()
            wake_text = speech_to_text(audio_data)

            if not wake_text:
                print("⏭️ 呼びかけを聞き取れなかったため、待機に戻ります。", flush=True)
                continue

            user_text = command_after_wake_word(wake_text)
            if user_text is None:
                print(f"⏭️ ウェイクワードなし: {wake_text}", flush=True)
                continue

            # 呼びかけと依頼が別録音になった場合は、次の録音を受け付ける
            if not user_text:
                speak("はい、どうぞ。")
                command_audio = record_audio()
                user_text = speech_to_text(command_audio)
                if not user_text:
                    print("⏭️ 依頼を聞き取れなかったため、待機に戻ります。", flush=True)
                    continue

            print(f"👤 ユーザー: {user_text}", flush=True)

            # 2. LLM による意図解析と応答生成
            cmd = parse_intent_with_llm(user_text)
            print(f"✅ 解析完了: {cmd}", flush=True)

            # 3. 音声で回答を返す
            speak(cmd.get("reply"))

            # 4. moOde audio (MPD) へコマンド発行
            control_moode(cmd)

    except KeyboardInterrupt:
        print("\n終了します。")
