"""moOde 音声ボット トラック監視・自動曲紹介モジュール。"""

import time
from .db import find_track_metadata
from . import config
from . import daily_info
from . import mpd_client
from . import state
from . import tts
from .broadcaster import (
    broadcast_event,
    broadcast_process_status,
    broadcast_status,
)


def play_startup_greeting():
    """起動時の初期案内アナウンス（日付・天気・今日のエピソードを統合、英語/日本語モード連動・Webチャット画面にも表示）"""
    try:
        if config.ANNOUNCE_LANGUAGE == "en":
            base_greeting = (
                "Hello! This is your moOde AI Assistant. "
                "Say 'Hey Master' to request a song, or use the web chat below to play your favorite music."
            )
            if config.ENABLE_DAILY_INFO:
                daily_intro = daily_info.generate_daily_intro(language="en")
                full_greeting = f"{base_greeting} {daily_intro}" if daily_intro else base_greeting
            else:
                full_greeting = base_greeting

            print(f"🎙️ [Greeting] 起動案内 (English): '{full_greeting}'", flush=True)
            msg_record = state.create_chat_message(
                sender="assistant",
                text=full_greeting,
                source="system",
                action="greeting",
            )
            state.append_chat_message(msg_record)
            broadcast_event({"type": "chat_message", "message": msg_record})

            broadcast_process_status("tts", "🎙️ Speaking startup greeting...")
            tts.speak_english(full_greeting)
            broadcast_process_status("idle", "🎙️ Ready for voice commands ('Hey Master')")
        else:
            base_greeting = (
                "こんにちは！moOde AI アシスタントです。"
                "マイクに向かって「ヘイ、マスター」と話しかけるか、下のチャット欄から曲やジャンルをリクエストしてください。"
            )
            if config.ENABLE_DAILY_INFO:
                daily_intro = daily_info.generate_daily_intro(language="ja")
                full_greeting = f"{base_greeting} {daily_intro}" if daily_intro else base_greeting
            else:
                full_greeting = base_greeting

            print(f"🎙️ [Greeting] 起動案内 (Japanese): '{full_greeting}'", flush=True)
            msg_record = state.create_chat_message(
                sender="assistant",
                text=full_greeting,
                source="system",
                action="greeting",
            )
            state.append_chat_message(msg_record)
            broadcast_event({"type": "chat_message", "message": msg_record})

            broadcast_process_status("tts", "🎙️ 起動案内を発話中...")
            tts.speak(full_greeting)
            broadcast_process_status("idle", "🎙️ 音声待機中 (「ヘイ、マスター」)")
    except Exception as e:
        print(f"⚠️ [Greeting] 起動アナウンスエラー: {e}", flush=True)


def announce_playback_finished():
    """キュー内の全楽曲再生終了時のアナウンス＆次曲リクエスト問いかけ"""
    try:
        if config.ANNOUNCE_LANGUAGE == "en":
            announce_text = (
                "All playback has finished. What would you like to listen to next? "
                "Say 'Hey Master' to request a song, or use the web chat below to make a request."
            )
            print(f"🎙️ [Watcher 再生終了案内 (English)] {announce_text}", flush=True)
            msg_record = state.create_chat_message(
                sender="assistant",
                text=announce_text,
                source="auto_announcer",
                action="playback_finished",
            )
            state.append_chat_message(msg_record)
            broadcast_event({"type": "chat_message", "message": msg_record})

            broadcast_process_status("tts", "🎙️ Speaking playback finished announcement...")
            tts.speak_english(announce_text)
            broadcast_process_status("idle", "🎙️ Ready for voice commands ('Hey Master')")
        else:
            announce_text = (
                "すべての曲の再生が終了しました。今度はどんな曲をかけますか？"
                "マイクに向かってリクエストするか、下のチャット欄からお知らせください。"
            )
            print(f"📖 [Watcher 再生終了案内 (Japanese)] {announce_text}", flush=True)
            msg_record = state.create_chat_message(
                sender="assistant",
                text=announce_text,
                source="auto_announcer",
                action="playback_finished",
            )
            state.append_chat_message(msg_record)
            broadcast_event({"type": "chat_message", "message": msg_record})

            broadcast_process_status("tts", "🎙️ 再生完了案内を発話中...")
            tts.speak(announce_text)
            broadcast_process_status("idle", "🎙️ 音声待機中 (「ヘイ、マスター」)")
    except Exception as e:
        print(f"⚠️ [Watcher] 再生終了アナウンスエラー: {e}", flush=True)


def run_track_watcher_loop():
    """moOde の再生進行を監視し、2曲目以降のトラック切り替わり時の自動曲紹介および全曲再生終了案内を読み上げるスレッド"""
    time.sleep(3.0)  # 起動初期化待ち
    print("🎧 [Watcher] トラック変更監視ループを開始しました。(2曲目以降の自動解説 & 再生終了案内)", flush=True)

    was_playing = False
    has_announced_finished = False

    while True:
        time.sleep(1.0)
        try:
            # システム発話中（TTS再生中）は監視スキップ
            if tts.is_speaking_event.is_set():
                continue

            status_data = mpd_client.get_moode_status()
            if not status_data.get("connected"):
                continue

            play_state = status_data.get("state")
            song = status_data.get("song") or {}
            cur_file = song.get("file")
            cur_id = song.get("id")

            # 再生中フラグの更新
            if play_state == "play" and cur_file:
                was_playing = True
                has_announced_finished = False

            # 全曲再生完了の検知（直前まで再生中で、現在停止状態 'stop' に遷移した場合）
            if play_state == "stop" and was_playing and not has_announced_finished:
                print("\n🏁 [Watcher] 全曲の再生完了を検知しました。", flush=True)
                was_playing = False
                has_announced_finished = True
                mpd_client.last_announced_file = None
                mpd_client.last_announced_songid = None
                announce_playback_finished()
                continue

            if not cur_file or play_state != "play":
                continue

            # 初回起動直後など、まだ何も記録されていない場合は記録のみ
            if mpd_client.last_announced_file is None:
                mpd_client.last_announced_file = cur_file
                mpd_client.last_announced_songid = cur_id
                continue

            # 同一曲判定（MPD ID または パス末尾/ファイル名が一致していれば同一曲とみなしスキップ）
            if state.is_same_track(cur_file, mpd_client.last_announced_file, cur_id, mpd_client.last_announced_songid):
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
            description = (
                description_en if config.ANNOUNCE_LANGUAGE == "en" and description_en else description_ja
            ) or description_en or description_ja
            if db_meta:
                song["description"] = description
                song["description_ja"] = description_ja
                song["description_en"] = description_en
                song["title_en"] = db_meta.get("title_en", song.get("title_en", ""))
                song["artist_en"] = db_meta.get("artist_en", song.get("artist_en", ""))
                song["genre"] = db_meta.get("genre", song.get("genre", ""))
                song["mood"] = db_meta.get("mood", song.get("mood", ""))

            if config.ANNOUNCE_LANGUAGE == "en":
                announce_text = tts.build_english_track_announcement(song, is_next=True)
                print(f"🎙️ [Watcher 英語曲紹介] {announce_text}", flush=True)
            else:
                announce_text = tts.build_japanese_track_announcement(song, description=description, prefix="続いては、")
                print(f"📖 [Watcher 自動曲紹介] {announce_text}", flush=True)

            # チャット履歴・Web UI にもプッシュ
            msg_record = state.create_chat_message(
                sender="assistant",
                text=announce_text,
                source="auto_announcer",
                action="track_transition",
                track_info=song,
                description=description,
            )
            state.append_chat_message(msg_record)
            broadcast_event({"type": "chat_message", "message": msg_record})

            # 3. 発話を実行（排他ロックで安全に発話）
            switch_time = time.time()
            if config.ANNOUNCE_LANGUAGE == "en":
                broadcast_process_status("tts", f"🎙️ DJ曲紹介アナウンス中: {t_title}")
                tts.speak_english(announce_text)
            else:
                broadcast_process_status("tts", f"🎙️ 曲紹介アナウンス中: {t_title}")
                tts.speak(announce_text)

            # 4. 発話完了後に ReplayGain 反映待ち時間を確保して音楽再生を再開
            elapsed = time.time() - switch_time
            remaining_delay = max(0.0, config.PLAY_DELAY_SEC - elapsed)
            if remaining_delay > 0.1:
                broadcast_process_status("playing", f"⏳ ReplayGain 適用待機中 ({remaining_delay:.1f}秒)...")
                time.sleep(remaining_delay)

            mpd_cli = mpd_client.get_mpd_client()
            if mpd_cli:
                try:
                    broadcast_process_status("playing", f"▶️ 音楽再生を再開しました: {t_title}", auto_idle_sec=3.5)
                    mpd_cli.play()
                    mpd_cli.close()
                    mpd_cli.disconnect()
                    print("▶️ [moOde] 曲紹介完了後に音楽再生を再開しました。", flush=True)
                    broadcast_status()
                except Exception as e:
                    print(f"⚠️ [moOde] 再生再開エラー: {e}")

        except Exception:
            time.sleep(2.0)
