"""moOde 音声ボット設定モジュール。"""

from typing import Optional, Tuple

try:
    import pyaudio
except ImportError:
    pyaudio = None


# ==================== moOde / MPD 設定 ====================
MOODE_IP: str = "192.168.68.198"  # moOde (Raspberry Pi 5) の IP アドレス
MOODE_PORT: int = 6600

# ==================== 音声合成 / LLM 設定 ====================
VOICEVOX_URL: str = "http://localhost:50021"
OLLAMA_CHAT_URL: str = "http://localhost:11434/api/chat"
SPEAKER_ID: int = 13  # 青山龍星（落ち着いた男性音声）
LLM_MODEL: str = "qwen2.5:1.5b"
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

    global MOODE_IP, MOODE_PORT, VOICEVOX_URL, OLLAMA_CHAT_URL, SPEAKER_ID, LLM_MODEL
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

        # moOde 設定
        moode_cfg = data.get("moode", {})
        if "ip" in moode_cfg:
            MOODE_IP = str(moode_cfg["ip"])
        if "port" in moode_cfg:
            MOODE_PORT = int(moode_cfg["port"])

        # LLM 設定
        llm_cfg = data.get("llm", {})
        if "model" in llm_cfg:
            LLM_MODEL = str(llm_cfg["model"])
        if "ollama_chat_url" in llm_cfg:
            OLLAMA_CHAT_URL = str(llm_cfg["ollama_chat_url"])

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



