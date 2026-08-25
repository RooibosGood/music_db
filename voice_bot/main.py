"""moOde 音声ボット メインエントリーポイント。"""

import argparse
import threading
import uvicorn

from . import config
from . import coverart
from . import mpd_client
from . import tts
from .api import app
from .broadcaster import broadcast_process_status
from .stt import run_voice_loop
from .watcher import play_startup_greeting, run_track_watcher_loop


def main():
    parser = argparse.ArgumentParser(
        description="moOde AI Master (Voice & Web Chat Assistant with DJ Announcements)",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument(
        "--moode-ip",
        type=str,
        default=config.MOODE_IP,
        help=f"moOde (MPD) IP address (default: {config.MOODE_IP})",
    )
    parser.add_argument(
        "--moode-port",
        type=int,
        default=config.MOODE_PORT,
        help=f"moOde (MPD) port (default: {config.MOODE_PORT})",
    )
    parser.add_argument(
        "--model", "--llm-model",
        type=str,
        default=config.LLM_MODEL,
        help=f"Ollama LLM model name (default: {config.LLM_MODEL})",
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
    parser.add_argument(
        "--host",
        type=str,
        default="0.0.0.0",
        help="Web server host (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Web server port (default: 8000)",
    )
    parser.add_argument(
        "--no-voice",
        action="store_true",
        help="Disable microphone voice listener thread",
    )
    args = parser.parse_args()

    config.MOODE_IP = args.moode_ip
    config.MOODE_PORT = args.moode_port
    config.LLM_MODEL = args.model

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
            print(f"⚠️ 不明な言語指定 '{args.lang}' のため、デフォルトの英語モード (en) を使用します。")
            config.ANNOUNCE_LANGUAGE = "en"
    else:
        config.ANNOUNCE_LANGUAGE = "en"  # デフォルト: 英語DJモード

    # 各モジュールへ設定値を同期
    coverart.MOODE_IP = config.MOODE_IP
    coverart.MOODE_PORT = config.MOODE_PORT

    mpd_client.MOODE_IP = config.MOODE_IP
    mpd_client.MOODE_PORT = config.MOODE_PORT
    mpd_client.ANNOUNCE_LANGUAGE = config.ANNOUNCE_LANGUAGE
    mpd_client.broadcast_process_status = broadcast_process_status

    tts.LLM_MODEL = config.LLM_MODEL

    if args.audio_dev:
        tts.AUDIO_OUTPUT_DEV = args.audio_dev
    else:
        tts.AUDIO_OUTPUT_DEV = tts.detect_alsa_output_device(config.AUDIO_OUTPUT_NAME)
    config.AUDIO_OUTPUT_DEV = tts.AUDIO_OUTPUT_DEV

    lang_banner = (
        "🎙️ ナレーション: 英語 DJ モード (English - description_en 読み上げ)"
        if config.ANNOUNCE_LANGUAGE == "en"
        else "🎙️ ナレーション: 日本語モード (Japanese - description_ja 読み上げ)"
    )
    print("=" * 70)
    print(" 🎵 moOde AI Master (Voice & Web Chat Assistant)")
    print(f" 📡 moOde IP   : {config.MOODE_IP}:{config.MOODE_PORT}")
    print(f" 🤖 LLM モデル : {config.LLM_MODEL} (Ollama)")
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
