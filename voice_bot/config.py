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
WEATHER_CITY: str = "Tokyo"  # 都市名（英語表記）
WEATHER_CITY_JA: str = "東京"  # 都市名（日本語表記）
WEATHER_LATITUDE: float = 35.6895  # 緯度（デフォルト: 東京）
WEATHER_LONGITUDE: float = 139.6917  # 経度（デフォルト: 東京）
WEATHER_TIMEZONE: str = "Asia/Tokyo"  # タイムゾーン

