"""moOde 音声ボット メインエントリーポイント。"""

import argparse
import threading
import uvicorn

from . import config
from . import mpd_client
from . import tts
from .api import app
from .broadcaster import broadcast_process_status
from .stt import run_voice_loop
from .watcher import play_startup_greeting, run_track_watcher_loop


def main():
    # 1. 予備パースで --config を取得し、設定ファイルを先行読み込み
    pre_parser = argparse.ArgumentParser(add_help=False)
    pre_parser.add_argument("--config", "-c", type=str, default=None)
    pre_args, _ = pre_parser.parse_known_args()

    loaded_cfg_path = config.load_config_from_file(pre_args.config)

    # 2. 全体引数の定義（デフォルト値は設定ファイル適用後の config 値）
    parser = argparse.ArgumentParser(
        description="moOde AI Master (Voice & Web Chat Assistant with DJ Announcements)",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument(
        "--config", "-c",
        type=str,
        default=pre_args.config,
        help="Path to JSON configuration file (default: voice_bot_config.json)",
    )
    parser.add_argument(
        "--moode-ip",
        type=str,
        default=None,
        help=f"moOde (MPD) IP address (default: {config.MOODE_IP})",
    )
    parser.add_argument(
        "--moode-port",
        type=int,
        default=None,
        help=f"moOde (MPD) port (default: {config.MOODE_PORT})",
    )
    parser.add_argument(
        "--model", "--llm-model",
        type=str,
        default=None,
        help=f"llama.cpp LLM model name (default: {config.LLM_MODEL})",
    )
    parser.add_argument(
        "--audio-dev",
        type=str,
        default=None,
        help="Audio output ALSA device (e.g. plughw:1,0, default)",
    )
    parser.add_argument(
        "--lang", "-l",
        type=str,
        default=None,
        help=f"Announcement language: 'en' (English DJ mode) or 'ja' (Japanese mode) [Default: {config.ANNOUNCE_LANGUAGE}]",
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
    parser.add_argument(
        "--host",
        type=str,
        default=None,
        help=f"Web server host (default: {config.SERVER_HOST})",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help=f"Web server port (default: {config.SERVER_PORT})",
    )
    parser.add_argument(
        "--mic", "--enable-mic", "--voice",
        action="store_true",
        help="Enable microphone voice listener thread (default: disabled)",
    )
    parser.add_argument(
        "--no-mic", "--no-voice",
        action="store_true",
        help="Disable microphone voice listener thread",
    )
    parser.add_argument(
        "--city",
        type=str,
        default=None,
        help=f"City name for weather forecast (default: {config.WEATHER_CITY})",
    )
    parser.add_argument(
        "--city-ja",
        type=str,
        default=None,
        help=f"Japanese city name for weather forecast (default: {config.WEATHER_CITY_JA})",
    )
    parser.add_argument(
        "--lat",
        type=float,
        default=None,
        help=f"Latitude for weather forecast (default: {config.WEATHER_LATITUDE})",
    )
    parser.add_argument(
        "--lon",
        type=float,
        default=None,
        help=f"Longitude for weather forecast (default: {config.WEATHER_LONGITUDE})",
    )
    parser.add_argument(
        "--no-daily-info",
        action="store_true",
        help="Disable startup date, weather and episode announcement",
    )
    args = parser.parse_args()

    # 3. CLI 引数で明示的に指定された項目を設定値に上書き
    if args.moode_ip is not None:
        config.MOODE_IP = args.moode_ip
    if args.moode_port is not None:
        config.MOODE_PORT = args.moode_port
    if args.model is not None:
        config.LLM_MODEL = args.model
    if args.city is not None:
        config.WEATHER_CITY = args.city
    if args.city_ja is not None:
        config.WEATHER_CITY_JA = args.city_ja
    if args.lat is not None:
        config.WEATHER_LATITUDE = args.lat
    if args.lon is not None:
        config.WEATHER_LONGITUDE = args.lon
    if args.no_daily_info:
        config.ENABLE_DAILY_INFO = False
    if args.host is not None:
        config.SERVER_HOST = args.host
    if args.port is not None:
        config.SERVER_PORT = args.port

    # マイク入力の判定 (CLI 引数が優先)
    if args.mic or getattr(args, "enable_mic", False) or getattr(args, "voice", False):
        config.ENABLE_VOICE_LISTENER = True
    elif args.no_mic or getattr(args, "no_voice", False):
        config.ENABLE_VOICE_LISTENER = False

    # 言語モードの判定
    if args.ja:
        config.ANNOUNCE_LANGUAGE = "ja"
    elif args.en:
        config.ANNOUNCE_LANGUAGE = "en"
    elif args.lang:
        lang_val = args.lang.lower().strip()
        if lang_val in ("ja", "japanese", "jp", "nihongo", "日本語"):
            config.ANNOUNCE_LANGUAGE = "ja"
        elif lang_val in ("en", "english", "eng", "英語"):
            config.ANNOUNCE_LANGUAGE = "en"
        else:
            print(f"⚠️ 不明な言語指定 '{args.lang}' のため、設定ファイル/デフォルトのモード ({config.ANNOUNCE_LANGUAGE}) を使用します。")

    # broadcaster 関数のみ注入（broadcaster → mpd_client の循環 import 回避のため）
    mpd_client.broadcast_process_status = broadcast_process_status

    if args.audio_dev:
        config.AUDIO_OUTPUT_DEV = args.audio_dev
    elif not config.AUDIO_OUTPUT_DEV:
        config.AUDIO_OUTPUT_DEV = tts.detect_alsa_output_device(config.AUDIO_OUTPUT_NAME)

    lang_banner = (
        "🎙️ ナレーション: 英語 DJ モード (English - description_en 読み上げ)"
        if config.ANNOUNCE_LANGUAGE == "en"
        else "🎙️ ナレーション: 日本語モード (Japanese - description_ja 読み上げ)"
    )
    daily_info_banner = (
        f"有効 (都市: {config.WEATHER_CITY_JA} / {config.WEATHER_CITY})"
        if config.ENABLE_DAILY_INFO
        else "無効"
    )
    cfg_banner = loaded_cfg_path if loaded_cfg_path else "未検出 (デフォルト設定を使用)"

    print("=" * 70)
    print(" 🎵 moOde AI Master (Voice & Web Chat Assistant)")
    print(f" 📂 設定ファイル: {cfg_banner}")
    print(f" 📡 moOde IP   : {config.MOODE_IP}:{config.MOODE_PORT}")
    print(f" 🤖 LLM モデル : {config.LLM_MODEL} (llama.cpp)")
    print(f" 🔊 音声出力   : {config.AUDIO_OUTPUT_DEV}")
    print(f" {lang_banner}")
    print(f" ☀️ デイリー情報: {daily_info_banner}")
    print(f" 🌐 Web UI     : http://{config.SERVER_HOST}:{config.SERVER_PORT} (ブラウザでアクセス)")
    print(f" 🎙️ マイク入力 : {'有効 (ヘイ、マスター)' if config.ENABLE_VOICE_LISTENER else '無効 (Default: OFF / Webチャットをご利用ください)'}")
    print("=" * 70)

    # 1. 起動案内アナウンス（マイクON/OFFに関わらず必ず実行）
    threading.Thread(target=play_startup_greeting, daemon=True).start()

    # 2. 自動トラック変更監視スレッド起動（2曲目以降の自動曲紹介 & 再生終了案内）
    watcher_thread = threading.Thread(target=run_track_watcher_loop, daemon=True)
    watcher_thread.start()

    # 3. 音声リスナースレッド起動（マイク有効時のみ）
    if config.ENABLE_VOICE_LISTENER:
        voice_thread = threading.Thread(target=run_voice_loop, daemon=True)
        voice_thread.start()

    # 4. Webサーバー (FastAPI + Uvicorn) 起動
    uvicorn.run(app, host=config.SERVER_HOST, port=config.SERVER_PORT, log_level="info")


if __name__ == "__main__":
    main()
