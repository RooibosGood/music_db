"""音声合成 & 音声出力 (TTS) モジュール

voice_bot.py から切り出し。
- 日本語: VOICEVOX + aplay (speak / convert_english_to_katakana / ENGLISH_KATAKANA_DICT)
- 英語: edge-tts / Google TTS / espeak-ng / SAPI (speak_english)
- 出力デバイス: ALSA 自動検出・ffmpeg / mpg123 / mpv / ffplay フォールバック再生

音声出力先・接続先・共有ロックなどは voice_bot.main() の起動時に
tts モジュールへ同期される想定（循環 import 回避のため値・参照の注入方式）。
"""
import asyncio
import io
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import wave
from typing import Optional

try:
    import edge_tts
except ImportError:
    edge_tts = None

# voice_bot.main() から同期される設定値
VOICEVOX_URL = "http://localhost:50021"
SPEAKER_ID = 13  # 青山龍星（落ち着いた男性音声）
LLM_MODEL = "qwen2.5:1.5b"
OLLAMA_CHAT_URL = "http://localhost:11434/api/chat"
ENGLISH_VOICE = "en-US-ChristopherNeural"  # 英語ラジオDJ風ニューラル音声 (edge-tts)
AUDIO_OUTPUT_NAME = "Sennheiser"  # 再生デバイス名（部分一致で自動検索）
AUDIO_OUTPUT_DEV = None  # Noneの場合は自動検出、または "plughw:1,0" 等
VOICE_PRE_SILENCE_SEC = 0.3  # 再生開始時の音切れ防止用

# 発話排他制御・発話中フラグ。voice_bot から threading オブジェクトの参照を注入する。
# 注入前でも独立動作できるようローカルに初期化しておく。
import threading
voice_lock = threading.Lock()
is_speaking_event = threading.Event()

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


# ==================== 音声出力デバイス ====================
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


# ==================== 日本語テキスト整形 (VOICEVOX 用) ====================
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


def _http_post_json(url: str, data: dict, timeout: float = 30.0) -> dict:
    """JSONペイロードをPOSTしてJSONレスポンスを返す（tts 内部用）"""
    import json as _json
    json_bytes = _json.dumps(data).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=json_bytes,
        headers={"Content-Type": "application/json", "User-Agent": "moOde-AI-Master/1.0"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        resp_data = response.read().decode("utf-8")
        return _json.loads(resp_data)


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
        res_json = _http_post_json(OLLAMA_CHAT_URL, payload, timeout=4.0)
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
    track_info: dict,
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
            res = _http_post_json(OLLAMA_CHAT_URL, payload, timeout=2.5)
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
