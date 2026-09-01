"""音声合成 & 音声出力 (TTS) モジュール

voice_bot.py から切り出し。
- 日本語: VOICEVOX + aplay (speak / convert_english_to_katakana / ENGLISH_KATAKANA_DICT)
- 英語: edge-tts / Google TTS / espeak-ng / SAPI (speak_english)
- 出力デバイス: ALSA 自動検出・ffmpeg / mpg123 / mpv / ffplay フォールバック再生

設定値は config モジュールを直接参照する。
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

# 日本語文字（ひらがな、カタカナ、漢字、長音符等）判定用正規表現
RE_JAPANESE = re.compile(r'[\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFF\u30FC]')

try:
    import pykakasi
    _kakasi = pykakasi.kakasi()
except ImportError:
    _kakasi = None

# 設定値は config モジュールを直接参照する
from . import config

# 発話排他制御・発話中フラグ。
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
    if not os.path.exists(wav_path) or os.path.getsize(wav_path) < 100:
        print(f"⚠️ [play_wav_file] WAVファイルが無効または空です: {wav_path}", flush=True)
        return False

    dev = target_dev or config.AUDIO_OUTPUT_DEV or detect_alsa_output_device(config.AUDIO_OUTPUT_NAME)
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


def add_silence_padding_to_wav(source_wav_path: str, output_wav_path: str, silence_sec: Optional[float] = None) -> bool:
    """WAVファイルの先頭に無音フレームを付加してスピーカー（Sennheiser SP 20等）の頭切れ・音切れを防止"""
    if silence_sec is None:
        silence_sec = config.VOICE_PRE_SILENCE_SEC
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
    target_dev = config.AUDIO_OUTPUT_DEV or detect_alsa_output_device(config.AUDIO_OUTPUT_NAME)

    # 1. ffmpeg で WAV 変換 ➔ 無音パディング付加 ➔ aplay
    if shutil.which("ffmpeg") and os.path.exists(mp3_path) and os.path.getsize(mp3_path) > 100:
        conv = subprocess.run(
            ["ffmpeg", "-y", "-i", mp3_path, "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le", raw_wav_path],
            capture_output=True, timeout=6
        )
        if conv.returncode == 0 and os.path.exists(raw_wav_path):
            add_silence_padding_to_wav(raw_wav_path, padded_wav_path, silence_sec=config.VOICE_PRE_SILENCE_SEC)
            if play_wav_file(padded_wav_path, target_dev):
                return True

    # 2. mpg123 で WAV 変換 ➔ 無音パディング付加 ➔ aplay
    if shutil.which("mpg123") and os.path.exists(mp3_path) and os.path.getsize(mp3_path) > 100:
        conv = subprocess.run(["mpg123", "-w", raw_wav_path, mp3_path], capture_output=True, timeout=6)
        if conv.returncode == 0 and os.path.exists(raw_wav_path):
            add_silence_padding_to_wav(raw_wav_path, padded_wav_path, silence_sec=config.VOICE_PRE_SILENCE_SEC)
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

    # 2. llama.cpp (LLM) による文脈カタカナ化
    try:
        payload = {
            "model": config.LLM_MODEL,
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
            "temperature": 0,
            "max_tokens": 128,
        }
        res_json = _http_post_json(config.LLAMA_CPP_CHAT_URL, payload, timeout=6.0)
        llm_reply = res_json.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
        llm_reply = re.sub(r"<think>[\s\S]*?</think>", "", llm_reply).strip()
        llm_reply = llm_reply.replace("```", "").replace("\n", " ").strip()
        if llm_reply and len(llm_reply) >= len(converted) * 0.5:
            print(f"🔤 [Kana] 英語カタカナ変換: '{text}' ➔ '{llm_reply}'", flush=True)
            return llm_reply
    except Exception as e:
        # LLMでの変換がタイムアウト・エラーの場合は辞書置換結果のまま進める
        pass

    return converted


def speak(text: str):
    """VOICEVOX ➔ aplay で Jetson スピーカーから音声出力（英語のカタカナ化対応）"""
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
            query_url = f"{config.VOICEVOX_URL}/audio_query?text={encoded_text}&speaker={config.SPEAKER_ID}"
            req_q = urllib.request.Request(query_url, data=b"", headers={"User-Agent": "moOde-AI/1.0"}, method="POST")
            with urllib.request.urlopen(req_q, timeout=30) as res_q:
                query_data = res_q.read()

            # 2. synthesis (長文でも耐えられるよう timeout=60 に設定)
            synth_url = f"{config.VOICEVOX_URL}/synthesis?speaker={config.SPEAKER_ID}"
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
                    silence_frames = int(source_wav.getframerate() * config.VOICE_PRE_SILENCE_SEC)
                    output_wav.writeframes(
                        b"\0" * silence_frames * source_wav.getnchannels() * source_wav.getsampwidth()
                    )
                    output_wav.writeframes(source_wav.readframes(source_wav.getnframes()))

            # 4. 音声再生
            play_wav_file(temp_wav)
            print(f"🔊 [VOICEVOX] 音声出力完了（{time.monotonic() - started_at:.1f}秒）", flush=True)
        except urllib.error.URLError as url_err:
            print(f"❌ [VOICEVOX] 接続エラー ({config.VOICEVOX_URL}): {url_err}")
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


def to_roman_if_japanese(text: str) -> str:
    """日本語文字（漢字・ひらがな・カタカナ）が含まれている場合、pykakasiを用いてヘボン式ローマ字に変換する"""
    if not text:
        return ""
    text_str = str(text).strip()
    if not RE_JAPANESE.search(text_str):
        return text_str

    if _kakasi is None:
        return text_str

    try:
        result = _kakasi.convert(text_str)
        words = []
        for item in result:
            hep = item.get("hepburn", "").strip()
            if hep:
                words.append(hep.capitalize())
            else:
                orig = item.get("orig", "").strip()
                if orig:
                    words.append(orig)
        roman_text = " ".join(words).strip()
        roman_text = re.sub(r"\s+", " ", roman_text)
        return roman_text if roman_text else text_str
    except Exception as e:
        print(f"⚠️ [tts] pykakasi ローマ字変換エラー: {e}", flush=True)
        return text_str


def build_english_track_announcement(
    track_info: dict,
    is_next: bool = False,
    is_skip: bool = False,
) -> str:
    """曲情報からスマートで自然な英語FMラジオDJ曲紹介文を生成（title_en / artist_en / description_en を最優先活用）"""
    if not track_info:
        return "Now playing the next track. Enjoy the music." if is_next else "Now playing music."

    raw_title = (track_info.get("title") or "Unknown Track").strip()
    raw_artist = (track_info.get("artist") or "").strip()
    title_en = (track_info.get("title_en") or "").strip()
    artist_en = (track_info.get("artist_en") or "").strip()
    genre = (track_info.get("genre") or "").strip()
    mood = (track_info.get("mood") or "").strip()
    desc_en = (track_info.get("description_en") or "").strip()
    desc_ja = (track_info.get("description_ja") or track_info.get("description") or "").strip()

    # 日本語/未設定表記のクリーンアップ
    if raw_artist in ("アーティスト未設定", "Unknown", "unknown", "None", "none", "null", ""):
        raw_artist = ""
    if artist_en in ("アーティスト未設定", "Unknown", "unknown", "None", "none", "null", ""):
        artist_en = ""

    # タイトル決定: title_en が存在し日本語が含まれていない場合は最優先、そうでなければ to_roman_if_japanese でローマ字変換
    if title_en and not RE_JAPANESE.search(title_en):
        title = title_en
    else:
        title = to_roman_if_japanese(title_en or raw_title)

    # アーティスト名決定: artist_en が存在し日本語が含まれていない場合は最優先、そうでなければ to_roman_if_japanese でローマ字変換
    if artist_en and not RE_JAPANESE.search(artist_en):
        artist = artist_en
    else:
        artist = to_roman_if_japanese(artist_en or raw_artist)

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

    # 2. description_en は無いが description_ja がある場合：LLM (llama.cpp) で英語DJ紹介文を生成
    if desc_ja:
        try:
            prompt = (
                f"You are a sophisticated FM Radio DJ. "
                f"Write a smooth, natural single-sentence track introduction in English based on: "
                f"Title: {title}, Artist: {artist}, Genre: {en_genre}, Description: {desc_ja[:150]}. "
                f"Keep it under 25 words. Output ONLY the single DJ sentence without quotes or preamble."
            )
            payload = {
                "model": config.LLM_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "temperature": 0.3,
                "max_tokens": 64,
            }
            res = _http_post_json(config.LLAMA_CPP_CHAT_URL, payload, timeout=8.0)
            dj_line = res.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
            dj_line = re.sub(r"<think>[\s\S]*?</think>", "", dj_line).strip()
            dj_line = dj_line.replace('"', '').replace('```', '').replace('\n', ' ').strip()
            if dj_line and len(dj_line) > 10:
                print(f"🎙️ [English DJ ナレーション (LLM生成)] '{dj_line}'", flush=True)
                return dj_line
        except Exception as e:
            print(f"⚠️ [tts] llama.cpp 英語DJ紹介文生成スキップ/フォールバック: {e}", flush=True)

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


def build_japanese_track_announcement(
    track_info: dict,
    description: str = "",
    prefix: str = "",
) -> str:
    """曲情報から日本語の単曲紹介文を生成（description_ja を活用・llm.py / watcher.py 共通）"""
    t_title = (track_info.get("title") or "楽曲").strip()
    t_artist = (track_info.get("artist") or "").strip()
    desc = description or track_info.get("description") or track_info.get("description_ja") or ""
    clean_desc = clean_text_for_speech(desc, max_chars=100)

    has_artist = t_artist and t_artist not in ("アーティスト未設定", "Unknown", "unknown")
    if clean_desc:
        if has_artist:
            return f"{prefix}『{t_title}』（{t_artist}）を再生します。{clean_desc}"
        return f"{prefix}『{t_title}』を再生します。{clean_desc}"
    if has_artist:
        return f"{prefix}『{t_title}』（{t_artist}）を再生します。"
    return f"{prefix}『{t_title}』を再生します。"


def build_playlist_overview_announcement(
    selected_tracks: list,
    query: str = "",
    first_track: dict | None = None,
    language: str = "ja",
) -> str:
    """
    選曲された曲群（プレイリスト）全体を俯瞰し、選曲概要＋1曲目紹介のナレーション文を生成
    （日本語 VOICEVOX / 英語 DJ モード両対応、LLM動的生成＆テンプレートフォールバック）
    """
    if not selected_tracks:
        if first_track:
            return (
                build_english_track_announcement(first_track)
                if language == "en"
                else build_japanese_track_announcement(first_track)
            )
        return "Now playing music." if language == "en" else "音楽を再生します。"

    total_count = len(selected_tracks)
    first_song = first_track or selected_tracks[0]

    # 1. アーティスト一覧の抽出（重複排除、未設定除外）
    artists_raw = []
    for t in selected_tracks:
        a = (t.get("artist") or "").strip()
        if a and a not in ("アーティスト未設定", "Unknown", "unknown", "None", "none", ""):
            if a not in artists_raw:
                artists_raw.append(a)

    # 2. ジャンル・ムードの抽出
    genres_raw = []
    moods_raw = []
    for t in selected_tracks:
        g = (t.get("genre") or "").strip()
        if g and g not in ("その他", "None", ""):
            for g_item in g.split(","):
                g_clean = g_item.strip()
                if g_clean and g_clean not in genres_raw:
                    genres_raw.append(g_clean)
        m = (t.get("mood") or "").strip()
        if m:
            for m_item in m.split(","):
                m_clean = m_item.strip()
                if m_clean and m_clean not in moods_raw:
                    moods_raw.append(m_clean)

    # 1曲目の情報
    first_title = (first_song.get("title") or "楽曲").strip()
    first_artist = (first_song.get("artist") or "").strip()
    first_title_en = (first_song.get("title_en") or "").strip()
    first_artist_en = (first_song.get("artist_en") or "").strip()
    desc_ja = (first_song.get("description_ja") or first_song.get("description") or "").strip()
    desc_en = (first_song.get("description_en") or "").strip()

    # =========================================================================
    # 言語別処理
    # =========================================================================
    if language == "en":
        # 英語モード
        if first_title_en and not RE_JAPANESE.search(first_title_en):
            en_title = first_title_en
        else:
            en_title = to_roman_if_japanese(first_title_en or first_title)

        if first_artist_en and not RE_JAPANESE.search(first_artist_en):
            en_artist = first_artist_en
        else:
            en_artist = to_roman_if_japanese(first_artist_en or first_artist)

        en_artists = [
            (to_roman_if_japanese(a) if RE_JAPANESE.search(a) else a) for a in artists_raw[:3]
        ]
        if len(artists_raw) == 1:
            artist_phrase = f"by {en_artists[0]}"
        elif len(artists_raw) == 2:
            artist_phrase = f"featuring {en_artists[0]} and {en_artists[1]}"
        elif len(artists_raw) >= 3:
            artist_phrase = f"featuring {en_artists[0]}, {en_artists[1]}, and more"
        else:
            artist_phrase = ""

        theme_en = query.strip() if query else (genres_raw[0] if genres_raw else "music")
        for ja_g, en_g in GENRE_JA_TO_EN.items():
            if ja_g in theme_en:
                theme_en = en_g
                break

        # 1. LLMによる英語DJトーク生成
        if config.LLAMA_CPP_CHAT_URL:
            try:
                prompt = (
                    f"You are a cool FM Radio DJ. "
                    f"Write a smooth, natural 2-sentence opening radio announcement introducing the selected playlist and the first track. "
                    f"Request/Theme: {theme_en}, Total Tracks: {total_count}, "
                    f"Artists: {', '.join(en_artists) if en_artists else en_artist}, "
                    f"First Track: '{en_title}' by {en_artist}, "
                    f"First Track Info: {desc_en or desc_ja[:120]}. "
                    f"Keep it under 35 words. Output ONLY the radio spoken line without quotes."
                )
                payload = {
                    "model": config.LLM_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                    "temperature": 0.3,
                    "max_tokens": 80,
                }
                res = _http_post_json(config.LLAMA_CPP_CHAT_URL, payload, timeout=6.0)
                dj_line = res.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
                dj_line = re.sub(r"<think>[\s\S]*?</think>", "", dj_line).strip()
                dj_line = dj_line.replace('"', '').replace('```', '').replace('\n', ' ').strip()
                if dj_line and len(dj_line) > 15:
                    print(f"🎙️ [English DJ 選曲俯瞰ナレーション (LLM)] '{dj_line}'", flush=True)
                    return dj_line
            except Exception:
                pass

        # 2. 英語テンプレートフォールバック
        clean_en_desc = clean_english_text_for_speech(desc_en, max_chars=140) if desc_en else ""
        if artist_phrase:
            intro = f"I've lined up {total_count} great tracks {artist_phrase}."
        elif theme_en:
            intro = f"Here are {total_count} {theme_en} tracks for you."
        else:
            intro = f"I've selected {total_count} tracks for you."

        kickoff = f"Let's get started with '{en_title}' by {en_artist}." if en_artist else f"Let's get started with '{en_title}'."
        announcement = f"{intro} {kickoff}"
        if clean_en_desc:
            announcement += f" {clean_en_desc}"
        print(f"🎙️ [English DJ 選曲俯瞰ナレーション (Template)] {announcement}", flush=True)
        return announcement

    else:
        # 日本語モード
        if len(artists_raw) == 1:
            artist_phrase = f"{artists_raw[0]}のナンバー"
        elif len(artists_raw) == 2:
            artist_phrase = f"{artists_raw[0]}や{artists_raw[1]}"
        elif len(artists_raw) >= 3:
            artist_phrase = f"{artists_raw[0]}や{artists_raw[1]}をはじめとするアーティスト"
        else:
            artist_phrase = ""

        theme_ja = query.strip() if query else (genres_raw[0] if genres_raw else "")
        if theme_ja:
            theme_phrase = f"「{theme_ja}」"
        elif moods_raw:
            theme_phrase = f"{moods_raw[0]}な雰囲気"
        else:
            theme_phrase = "おすすめの楽曲"

        # 1. LLM (llama.cpp) による日本語選曲俯瞰トーク生成
        if config.LLAMA_CPP_CHAT_URL:
            try:
                prompt = (
                    f"あなたはFMラジオのパーソナリティ（DJ）です。"
                    f"リスナーのリクエストに基づいて選曲されたプレイリストについて、選曲の全体像を俯瞰した自然で魅力的なオープニングコメント（選曲のテーマ・アーティスト・曲数）と、1曲目の紹介文を作成してください。\n"
                    f"リクエスト/テーマ: {theme_ja or 'おすすめ'}\n"
                    f"選曲数: 全{total_count}曲\n"
                    f"主なアーティスト: {', '.join(artists_raw[:3]) if artists_raw else first_artist}\n"
                    f"1曲目: 『{first_title}』{f'（{first_artist}）' if first_artist else ''}\n"
                    f"1曲目の背景・解説: {desc_ja[:120]}\n"
                    f"【条件】\n"
                    f"- ラジオの曲紹介として自然な話し言葉で、2〜3文（80〜120文字程度）で出力してください。\n"
                    f"- 余計な前置きやマークダウン、引用符は出力せず、発話するナレーション文のみを出力してください。"
                )
                payload = {
                    "model": config.LLM_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                    "temperature": 0.4,
                    "max_tokens": 128,
                }
                res = _http_post_json(config.LLAMA_CPP_CHAT_URL, payload, timeout=6.0)
                dj_line = res.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
                dj_line = re.sub(r"<think>[\s\S]*?</think>", "", dj_line).strip()
                dj_line = dj_line.replace('"', '').replace('```', '').replace('\n', ' ').strip()
                if dj_line and len(dj_line) > 20:
                    print(f"📖 [日本語 選曲俯瞰ナレーション (LLM)] '{dj_line}'", flush=True)
                    return dj_line
            except Exception:
                pass

        # 2. 日本語テンプレートフォールバック
        clean_desc = clean_text_for_speech(desc_ja, max_chars=100) if desc_ja else ""
        has_first_artist = first_artist and first_artist not in ("アーティスト未設定", "Unknown", "unknown")

        if len(artists_raw) == 1:
            # 単一アーティスト（例: ビートルズのみ）
            artist_name = artists_raw[0]
            if theme_ja and theme_ja.lower() not in artist_name.lower():
                intro = f"「{theme_ja}」から、{artist_name}の楽曲全{total_count}曲をセレクトしました。"
            else:
                intro = f"{artist_name}の代表曲全{total_count}曲をセレクトしました。"
            # 単一アーティストの場合は1曲目のアーティスト名を省略してスッキリ
            track_part = f"まずは1曲目、『{first_title}』からお届けします。"
        elif len(artists_raw) == 2:
            # 2アーティスト
            a1, a2 = artists_raw[0], artists_raw[1]
            if theme_ja:
                intro = f"「{theme_ja}」から、{a1}や{a2}などの名曲全{total_count}曲をセレクトしました。"
            else:
                intro = f"{a1}や{a2}などの楽曲全{total_count}曲をセレクトしました。"
            track_part = f"まずは1曲目、『{first_title}』{f'（{first_artist}）' if has_first_artist else ''}からお届けします。"
        elif len(artists_raw) >= 3:
            # 3人以上
            a1, a2 = artists_raw[0], artists_raw[1]
            if theme_ja:
                intro = f"「{theme_ja}」から、{a1}や{a2}をはじめとする名曲全{total_count}曲をセレクトしました。"
            else:
                intro = f"{a1}や{a2}をはじめとするアーティストの名曲全{total_count}曲をセレクトしました。"
            track_part = f"まずは1曲目、『{first_title}』{f'（{first_artist}）' if has_first_artist else ''}からお届けします。"
        else:
            # アーティスト情報なし
            if theme_ja:
                intro = f"「{theme_ja}」に合わせた楽曲全{total_count}曲をセレクトしました。"
            else:
                intro = f"おすすめの楽曲全{total_count}曲をセレクトしました。"
            track_part = f"まずは1曲目、『{first_title}』からお届けします。"

        announcement = f"{intro} {track_part}"
        if clean_desc:
            announcement += f" {clean_desc}"
        print(f"📖 [日本語 選曲俯瞰ナレーション (Template)] {announcement}", flush=True)
        return announcement


def speak_english(text: str):
    """英語テキストを Jetson スピーカーからネイティブ英語音声で出力 (edge-tts / Google TTS / espeak-ng)"""
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
            print(f"🎙️ [English DJ] 1. Microsoft edge-tts 音声合成を試行中... (ボイス: {config.ENGLISH_VOICE})", flush=True)
            # 1-1. Python モジュールとしての edge_tts 呼び出し
            if edge_tts is not None:
                try:
                    async def _gen_edge_tts():
                        communicate = edge_tts.Communicate(speech_text, config.ENGLISH_VOICE)
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
                    "--voice", config.ENGLISH_VOICE,
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
                        "--voice", config.ENGLISH_VOICE,
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
                            add_silence_padding_to_wav(temp_raw_wav, temp_padded_wav, silence_sec=config.VOICE_PRE_SILENCE_SEC)
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
