"""moOde 音声ボット設定モジュール。"""

import os
from typing import Optional, Tuple

try:
    import pyaudio
except ImportError:
    pyaudio = None


# ==================== デモモード設定 ====================
DEMO_MODE: bool = False  # moOde実機なしで選曲・再生・解説・ステータス更新をシミュレーションするデモモード

# ==================== moOde / MPD 設定 ====================
MOODE_IP: str = "192.168.68.198"  # moOde (Raspberry Pi 5) の IP アドレス
MOODE_PORT: int = 6600
PLAY_DELAY_SEC: float = 0.0  # 曲選択から再生開始までの追加待機時間（safe_start_playback により自動制御）
PRE_DECODE_DELAY_SEC: float = 0.35  # ReplayGainタグ解析遅延によるバースト防止用プリデコード待機時間（秒）
REPLAYGAIN_MODE: str = "track"  # MPD ReplayGain モード ("track", "album", "auto", "off")
MUSIC_DIR: Optional[str] = "/mnt/music" if os.name != "nt" else None  # Jetson上のNASマウント先（デフォルト: /mnt/music）

# ==================== 音声合成 / LLM 設定 ====================
VOICEVOX_URL: str = "http://localhost:50021"
LLAMA_CPP_CHAT_URL: str = "http://localhost:8080/v1/chat/completions"
SPEAKER_ID: int = 13  # 青山龍星（落ち着いた男性音声）
LLM_MODEL: str = "google_gemma-4-E2B-it-Q4_K_M.gguf"
ANNOUNCE_LANGUAGE: str = "en"  # 曲紹介の言語: "en" (英語DJモード) または "ja" (日本語)
ENGLISH_VOICE: str = "en-US-ChristopherNeural"  # 英語ラジオDJ風ニューラル音声 (edge-tts)

# ==================== オーディオデバイス設定 ====================
AUDIO_OUTPUT_NAME: str = "Sennheiser"  # 再生デバイス名（部分一致で自動検索）
AUDIO_OUTPUT_DEV: Optional[str] = None  # ALSAデバイス名（例: "plughw:1,0"）
VOICE_PRE_SILENCE_SEC: float = 0.3  # 再生開始時の音切れ防止用
INPUT_DEVICE_NAME: str = "Sennheiser SP 20"  # PyAudioの表示名に含まれる文字列
INPUT_DEVICE_INDEX: Optional[int] = None  # 名前で見つからない場合に使うPyAudio番号

# ==================== ウェイクワード設定 ====================
WAKE_WORD_PATTERNS: Tuple[str, ...] = (
    r"ヘイ[\s、,。！？!?]*マスター",
    r"へい[\s、,。！？!?]*ますたー",
    r"hey[\s、,。！？!?]*master",
)

# ==================== マイク録音設定 ====================
FORMAT = pyaudio.paInt16 if pyaudio else 2
CHANNELS: int = 1
RATE: int = 16000
CHUNK: int = 1024
RECORD_SECONDS: int = 4

# ==================== 天気・デイリー情報設定 ====================
ENABLE_DAILY_INFO: bool = True  # 起動時に日付・天気・今日のエピソードを紹介するかどうか
WEATHER_CITY: str = "Ritto, Shiga"  # 都市名（英語表記: 滋賀県栗東市）
WEATHER_CITY_JA: str = "滋賀県栗東市"  # 都市名（日本語表記）
WEATHER_LATITUDE: float = 35.0163  # 緯度（デフォルト: 滋賀県栗東市付近）
WEATHER_LONGITUDE: float = 135.9733  # 経度（デフォルト: 滋賀県栗東市付近）
WEATHER_TIMEZONE: str = "Asia/Tokyo"  # タイムゾーン
# ==================== Webサーバー設定 ====================
SERVER_HOST: str = "0.0.0.0"
SERVER_PORT: int = 8000
ENABLE_VOICE_LISTENER: bool = False  # マイク音声入力リスナー（デフォルト: OFF）
CONFIG_FILE_PATH: Optional[str] = None


def load_config_from_file(config_path: Optional[str] = None) -> Optional[str]:
    """JSON設定ファイル (config.json) から設定値を読み込み、グローバル設定変数を更新する。

    Returns:
        読み込んだ設定ファイルのパス（見つからない・失敗時は None）
    """
    import json
    import os

    global DEMO_MODE, MOODE_IP, MOODE_PORT, PLAY_DELAY_SEC, MUSIC_DIR, VOICEVOX_URL, LLAMA_CPP_CHAT_URL, SPEAKER_ID, LLM_MODEL
    global ANNOUNCE_LANGUAGE, ENGLISH_VOICE, AUDIO_OUTPUT_NAME, AUDIO_OUTPUT_DEV
    global VOICE_PRE_SILENCE_SEC, INPUT_DEVICE_NAME, INPUT_DEVICE_INDEX
    global ENABLE_DAILY_INFO, WEATHER_CITY, WEATHER_CITY_JA, WEATHER_LATITUDE, WEATHER_LONGITUDE, WEATHER_TIMEZONE
    global SERVER_HOST, SERVER_PORT, ENABLE_VOICE_LISTENER, CONFIG_FILE_PATH

    # 検索対象パスの候補
    candidates = []
    if config_path:
        candidates.append(config_path)
    else:
        candidates.extend([
            "voice_bot_config.json",
            os.path.join(os.path.dirname(os.path.dirname(__file__)), "voice_bot_config.json"),
            "config.json",
            os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.json"),
        ])

    target_path = None
    for path in candidates:
        if path and os.path.isfile(path):
            target_path = os.path.abspath(path)
            break

    if not target_path:
        return None

    try:
        with open(target_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        # デモモード設定
        if "demo_mode" in data:
            DEMO_MODE = bool(data["demo_mode"])

        # moOde / 音楽ディレクトリ設定
        if "music_dir" in data:
            MUSIC_DIR = str(data["music_dir"])
        elif "MUSIC_DIR" in os.environ:
            MUSIC_DIR = os.environ["MUSIC_DIR"]

        moode_cfg = data.get("moode", {})
        if "demo_mode" in moode_cfg:
            DEMO_MODE = bool(moode_cfg["demo_mode"])
        if "ip" in moode_cfg:
            MOODE_IP = str(moode_cfg["ip"])
        if "port" in moode_cfg:
            MOODE_PORT = int(moode_cfg["port"])
        if "play_delay_sec" in moode_cfg:
            PLAY_DELAY_SEC = float(moode_cfg["play_delay_sec"])
        elif "play_delay_sec" in data:
            PLAY_DELAY_SEC = float(data["play_delay_sec"])
        if "pre_decode_delay_sec" in moode_cfg:
            PRE_DECODE_DELAY_SEC = float(moode_cfg["pre_decode_delay_sec"])
        elif "pre_decode_delay_sec" in data:
            PRE_DECODE_DELAY_SEC = float(data["pre_decode_delay_sec"])
        if "replaygain_mode" in moode_cfg:
            REPLAYGAIN_MODE = str(moode_cfg["replaygain_mode"]).lower()
        elif "replaygain_mode" in data:
            REPLAYGAIN_MODE = str(data["replaygain_mode"]).lower()
        if "music_dir" in moode_cfg:
            MUSIC_DIR = str(moode_cfg["music_dir"])

        # LLM 設定
        llm_cfg = data.get("llm", {})
        if "model" in llm_cfg:
            LLM_MODEL = str(llm_cfg["model"])
        if "llama_cpp_chat_url" in llm_cfg:
            LLAMA_CPP_CHAT_URL = str(llm_cfg["llama_cpp_chat_url"])
        elif "ollama_chat_url" in llm_cfg:
            print("⚠️ [config] 旧設定 'ollama_chat_url' が検出されました。llama-server 用エンドポイント (http://localhost:8080/v1/chat/completions) に自動移行します。")
            LLAMA_CPP_CHAT_URL = "http://localhost:8080/v1/chat/completions"

        # アナウンス・音声合成設定
        ann_cfg = data.get("announcement", {})
        if "language" in ann_cfg:
            ANNOUNCE_LANGUAGE = str(ann_cfg["language"])
        if "english_voice" in ann_cfg:
            ENGLISH_VOICE = str(ann_cfg["english_voice"])
        if "voicevox_url" in ann_cfg:
            VOICEVOX_URL = str(ann_cfg["voicevox_url"])
        if "speaker_id" in ann_cfg:
            SPEAKER_ID = int(ann_cfg["speaker_id"])

        # オーディオデバイス設定
        audio_cfg = data.get("audio", {})
        if "output_device_name" in audio_cfg:
            AUDIO_OUTPUT_NAME = str(audio_cfg["output_device_name"])
        if "output_alsa_dev" in audio_cfg and audio_cfg["output_alsa_dev"] is not None:
            AUDIO_OUTPUT_DEV = str(audio_cfg["output_alsa_dev"])
        if "input_device_name" in audio_cfg:
            INPUT_DEVICE_NAME = str(audio_cfg["input_device_name"])
        if "input_device_index" in audio_cfg and audio_cfg["input_device_index"] is not None:
            INPUT_DEVICE_INDEX = int(audio_cfg["input_device_index"])
        if "enable_voice_listener" in audio_cfg:
            ENABLE_VOICE_LISTENER = bool(audio_cfg["enable_voice_listener"])
        elif "enable_mic" in audio_cfg:
            ENABLE_VOICE_LISTENER = bool(audio_cfg["enable_mic"])

        # 天気・デイリー情報設定
        weather_cfg = data.get("weather_and_daily_info", {})
        if "enable" in weather_cfg:
            ENABLE_DAILY_INFO = bool(weather_cfg["enable"])
        if "city" in weather_cfg:
            WEATHER_CITY = str(weather_cfg["city"])
        if "city_ja" in weather_cfg:
            WEATHER_CITY_JA = str(weather_cfg["city_ja"])
        if "latitude" in weather_cfg:
            WEATHER_LATITUDE = float(weather_cfg["latitude"])
        if "longitude" in weather_cfg:
            WEATHER_LONGITUDE = float(weather_cfg["longitude"])
        if "timezone" in weather_cfg:
            WEATHER_TIMEZONE = str(weather_cfg["timezone"])

        # Webサーバー設定
        server_cfg = data.get("server", {})
        if "host" in server_cfg:
            SERVER_HOST = str(server_cfg["host"])
        if "port" in server_cfg:
            SERVER_PORT = int(server_cfg["port"])

        CONFIG_FILE_PATH = target_path
        return target_path

    except Exception as e:
        print(f"⚠️ [config] 設定ファイル読み込みエラー ({target_path}): {e}")
        return None


def get_current_settings() -> dict:
    """現在の設定値一覧を辞書形式で取得"""
    return {
        "demo_mode": DEMO_MODE,
        "moode": {
            "ip": MOODE_IP,
            "port": MOODE_PORT,
            "play_delay_sec": PLAY_DELAY_SEC,
            "pre_decode_delay_sec": PRE_DECODE_DELAY_SEC,
            "replaygain_mode": REPLAYGAIN_MODE,
            "music_dir": MUSIC_DIR,
        },
        "announcement": {
            "language": ANNOUNCE_LANGUAGE,
            "english_voice": ENGLISH_VOICE,
            "voicevox_url": VOICEVOX_URL,
            "speaker_id": SPEAKER_ID,
        },
        "llm": {
            "model": LLM_MODEL,
            "llama_cpp_chat_url": LLAMA_CPP_CHAT_URL,
        },
        "audio": {
            "output_device_name": AUDIO_OUTPUT_NAME,
            "output_alsa_dev": AUDIO_OUTPUT_DEV,
            "input_device_name": INPUT_DEVICE_NAME,
            "input_device_index": INPUT_DEVICE_INDEX,
            "enable_voice_listener": ENABLE_VOICE_LISTENER,
        },
        "weather_and_daily_info": {
            "enable": ENABLE_DAILY_INFO,
            "city": WEATHER_CITY,
            "city_ja": WEATHER_CITY_JA,
            "latitude": WEATHER_LATITUDE,
            "longitude": WEATHER_LONGITUDE,
            "timezone": WEATHER_TIMEZONE,
        },
        "server": {
            "host": SERVER_HOST,
            "port": SERVER_PORT,
        },
        "config_file_path": CONFIG_FILE_PATH,
    }


def save_config_to_file(updates: dict) -> bool:
    """設定変更を voice_bot_config.json に保存し、内部のグローバル設定変数を更新する。"""
    import json
    import os

    global DEMO_MODE, MOODE_IP, MOODE_PORT, PLAY_DELAY_SEC, PRE_DECODE_DELAY_SEC, MUSIC_DIR, VOICEVOX_URL, LLAMA_CPP_CHAT_URL, SPEAKER_ID, LLM_MODEL
    global ANNOUNCE_LANGUAGE, ENGLISH_VOICE, AUDIO_OUTPUT_NAME, AUDIO_OUTPUT_DEV
    global VOICE_PRE_SILENCE_SEC, INPUT_DEVICE_NAME, INPUT_DEVICE_INDEX
    global ENABLE_DAILY_INFO, WEATHER_CITY, WEATHER_CITY_JA, WEATHER_LATITUDE, WEATHER_LONGITUDE, WEATHER_TIMEZONE
    global SERVER_HOST, SERVER_PORT, ENABLE_VOICE_LISTENER, CONFIG_FILE_PATH

    target_path = CONFIG_FILE_PATH
    if not target_path:
        project_root = os.path.dirname(os.path.dirname(__file__))
        target_path = os.path.join(project_root, "voice_bot_config.json")

    data = {}
    if os.path.exists(target_path):
        try:
            with open(target_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}

    # デモモード
    if "demo_mode" in updates:
        val = bool(updates["demo_mode"])
        DEMO_MODE = val
        data["demo_mode"] = val

    # 音楽ディレクトリ
    if "music_dir" in updates:
        MUSIC_DIR = str(updates["music_dir"]) if updates["music_dir"] else None
        data["music_dir"] = MUSIC_DIR

    # moOde
    if "moode" in updates:
        moode_cfg = data.setdefault("moode", {})
        if "ip" in updates["moode"]:
            MOODE_IP = str(updates["moode"]["ip"])
            moode_cfg["ip"] = MOODE_IP
        if "port" in updates["moode"]:
            MOODE_PORT = int(updates["moode"]["port"])
            moode_cfg["port"] = MOODE_PORT
        if "play_delay_sec" in updates["moode"]:
            PLAY_DELAY_SEC = float(updates["moode"]["play_delay_sec"])
            moode_cfg["play_delay_sec"] = PLAY_DELAY_SEC
        if "pre_decode_delay_sec" in updates["moode"]:
            PRE_DECODE_DELAY_SEC = float(updates["moode"]["pre_decode_delay_sec"])
            moode_cfg["pre_decode_delay_sec"] = PRE_DECODE_DELAY_SEC
        if "replaygain_mode" in updates["moode"]:
            REPLAYGAIN_MODE = str(updates["moode"]["replaygain_mode"]).lower()
            moode_cfg["replaygain_mode"] = REPLAYGAIN_MODE
        if "music_dir" in updates["moode"]:
            MUSIC_DIR = str(updates["moode"]["music_dir"]) if updates["moode"]["music_dir"] else None
            moode_cfg["music_dir"] = MUSIC_DIR
        if "demo_mode" in updates["moode"]:
            DEMO_MODE = bool(updates["moode"]["demo_mode"])
            data["demo_mode"] = DEMO_MODE

    # アナウンス
    if "announcement" in updates:
        ann_cfg = data.setdefault("announcement", {})
        if "language" in updates["announcement"]:
            ANNOUNCE_LANGUAGE = str(updates["announcement"]["language"])
            ann_cfg["language"] = ANNOUNCE_LANGUAGE
        if "english_voice" in updates["announcement"]:
            ENGLISH_VOICE = str(updates["announcement"]["english_voice"])
            ann_cfg["english_voice"] = ENGLISH_VOICE
        if "voicevox_url" in updates["announcement"]:
            VOICEVOX_URL = str(updates["announcement"]["voicevox_url"])
            ann_cfg["voicevox_url"] = VOICEVOX_URL
        if "speaker_id" in updates["announcement"]:
            SPEAKER_ID = int(updates["announcement"]["speaker_id"])
            ann_cfg["speaker_id"] = SPEAKER_ID

    # 天気・デイリー情報
    if "weather_and_daily_info" in updates:
        w_cfg = data.setdefault("weather_and_daily_info", {})
        if "enable" in updates["weather_and_daily_info"]:
            ENABLE_DAILY_INFO = bool(updates["weather_and_daily_info"]["enable"])
            w_cfg["enable"] = ENABLE_DAILY_INFO

    # 音声リスナー
    if "audio" in updates:
        a_cfg = data.setdefault("audio", {})
        if "enable_voice_listener" in updates["audio"]:
            ENABLE_VOICE_LISTENER = bool(updates["audio"]["enable_voice_listener"])
            a_cfg["enable_voice_listener"] = ENABLE_VOICE_LISTENER

    try:
        with open(target_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        CONFIG_FILE_PATH = target_path
        print(f"✅ [config] 設定を保存しました: {target_path} (demo_mode={DEMO_MODE}, lang={ANNOUNCE_LANGUAGE})")
        return True
    except Exception as e:
        print(f"❌ [config] 設定保存エラー ({target_path}): {e}")
        return False



