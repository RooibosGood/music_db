"""MPD (moOde Audio) 制御モジュール

voice_bot.py から切り出し。
- get_mpd_client: MPD 接続
- get_moode_status: 再生ステータスと現在曲情報の取得
- control_moode: 選曲・再生・停止・スキップ・音量などの操作

設定値は config モジュールを直接参照する。
broadcast_process_status のみ voice_bot.main() の起動時に注入される想定
（broadcaster → mpd_client の循環 import 回避のため）。
"""
import time
import traceback
from typing import Any, Dict, Optional

from .db import add_db_tracks_to_mpd, find_track_metadata, search_tracks_from_db, update_track_rating

from . import config

# ReplayGainタグ解析遅延によるバースト防止用プリデコード待機時間（秒）
PRE_DECODE_DELAY_SEC: float = getattr(config, "PRE_DECODE_DELAY_SEC", 0.35)

try:
    from mpd import MPDClient
except ImportError:
    MPDClient = None

# 処理ステータス通知関数（voice_bot から注入される。None の場合は print のみ）
broadcast_process_status = None

# 二重曲紹介防止用（voice_bot の watcher ループと共有するためモジュール変数で保持）
last_announced_file: Optional[str] = None
last_announced_songid: Optional[str] = None


def _broadcast(step: str, detail: str, auto_idle_sec: Optional[float] = None):
    """注入された broadcast_process_status があれば呼び出す"""
    if broadcast_process_status is not None:
        broadcast_process_status(step, detail, auto_idle_sec=auto_idle_sec)


class DemoPlayer:
    """moOde (MPD) 実機が無い環境向けの仮想プレイヤー（キュー管理・再生時間進行・自動曲送りシミュレーション）"""

    def __init__(self):
        self.playlist: list = []
        self.current_index: int = 0
        self.state: str = "stop"  # "play", "pause", "stop"
        self.volume: int = 50
        self.elapsed: float = 0.0
        self.duration: float = 210.0
        self.last_tick_time: float = time.time()
        self._song_id_counter: int = 1

    def _tick(self):
        """再生中なら経過時間を進め、曲終了時に自動で次の曲または停止へ遷移"""
        now = time.time()
        dt = now - self.last_tick_time
        self.last_tick_time = now

        if self.state == "play" and self.playlist:
            self.elapsed += dt
            if self.elapsed >= self.duration:
                # 曲が終了した
                if self.current_index < len(self.playlist) - 1:
                    self.current_index += 1
                    self.elapsed = 0.0
                    cur = self.get_current_song()
                    self.duration = float(cur.get("duration", 210.0))
                else:
                    self.state = "stop"
                    self.elapsed = 0.0

    def clear(self):
        self._tick()
        self.playlist = []
        self.current_index = 0
        self.state = "stop"
        self.elapsed = 0.0

    def add(self, file_path: str):
        self._tick()
        norm_path = file_path.replace("\\", "/")
        db_meta = find_track_metadata(file_path=norm_path)
        if not db_meta:
            fname = norm_path.split("/")[-1]
            title = fname.rsplit(".", 1)[0] if "." in fname else fname
            artist = "Demo Artist"
            album = "moOde Demo Library"
            is_hires = False
            title_en = ""
            artist_en = ""
            desc_ja = ""
            desc_en = ""
        else:
            title = db_meta.get("title", "")
            artist = db_meta.get("artist", "")
            album = db_meta.get("album", "")
            is_hires = bool(db_meta.get("is_hires", 0))
            title_en = db_meta.get("title_en", "")
            artist_en = db_meta.get("artist_en", "")
            desc_ja = db_meta.get("description_ja", "")
            desc_en = db_meta.get("description_en", "")

        song_id = str(self._song_id_counter)
        self._song_id_counter += 1

        item = {
            "file": norm_path,
            "id": song_id,
            "title": title,
            "artist": artist,
            "album": album,
            "title_en": title_en,
            "artist_en": artist_en,
            "description_ja": desc_ja,
            "description_en": desc_en,
            "duration": 210.0,
            "time": "210",
            "is_hires": is_hires,
            "audio": "96000:24:2" if is_hires else "44100:16:2",
        }
        self.playlist.append(item)
        if len(self.playlist) == 1:
            self.duration = item["duration"]

    def play(self, pos: Optional[int] = None):
        self._tick()
        if pos is not None and 0 <= int(pos) < len(self.playlist):
            self.current_index = int(pos)
            self.elapsed = 0.0
        if self.playlist:
            self.state = "play"
            cur = self.get_current_song()
            self.duration = float(cur.get("duration", 210.0))
        self.last_tick_time = time.time()

    def pause(self, pause_flag: int = 1):
        self._tick()
        if int(pause_flag) == 1:
            self.state = "pause"
        else:
            self.state = "play"
        self.last_tick_time = time.time()

    def stop(self):
        self._tick()
        self.state = "stop"
        self.elapsed = 0.0

    def next(self):
        self._tick()
        if self.playlist and self.current_index < len(self.playlist) - 1:
            self.current_index += 1
            self.elapsed = 0.0
            cur = self.get_current_song()
            self.duration = float(cur.get("duration", 210.0))
        else:
            self.state = "stop"
            self.elapsed = 0.0

    def previous(self):
        self._tick()
        if self.playlist and self.current_index > 0:
            self.current_index -= 1
            self.elapsed = 0.0
            cur = self.get_current_song()
            self.duration = float(cur.get("duration", 210.0))

    def setvol(self, vol: int):
        self.volume = max(0, min(100, int(vol)))

    def get_current_song(self) -> Dict[str, Any]:
        if 0 <= self.current_index < len(self.playlist):
            return self.playlist[self.current_index]
        return {}

    def status(self) -> Dict[str, Any]:
        self._tick()
        cur = self.get_current_song()
        return {
            "state": self.state,
            "volume": str(self.volume),
            "elapsed": f"{self.elapsed:.2f}",
            "duration": f"{self.duration:.2f}",
            "playlistlength": str(len(self.playlist)),
            "song": str(self.current_index) if self.playlist else "0",
            "songid": cur.get("id", ""),
            "audio": cur.get("audio", "44100:16:2"),
        }

    def currentsong(self) -> Dict[str, Any]:
        self._tick()
        return self.get_current_song()

    def playlistinfo(self) -> list:
        self._tick()
        return list(self.playlist)

    def search(self, search_type: str, query: str) -> list:
        # music_meta.db から検索
        db_tracks = search_tracks_from_db(query, limit=5)
        if db_tracks:
            return [
                {
                    "file": t.get("relative_path") or t.get("file_path") or query,
                    "title": t.get("title", ""),
                    "artist": t.get("artist", ""),
                }
                for t in db_tracks
            ]
        return [{"file": query, "title": query, "artist": "Demo"}]


# シングルトン DemoPlayer インスタンス
demo_player = DemoPlayer()


class MockMPDClient:
    """python-mpd2 MPDClient のインターフェース互換モック"""

    def __init__(self):
        self.player = demo_player
        self.replaygain_mode_val = "track"

    def connect(self, host: str, port: int):
        pass

    def close(self):
        pass

    def disconnect(self):
        pass

    def clear(self):
        self.player.clear()

    def add(self, uri: str):
        self.player.add(uri)

    def search(self, search_type: str, query: str):
        return self.player.search(search_type, query)

    def playlistinfo(self):
        return self.player.playlistinfo()

    def currentsong(self):
        return self.player.currentsong()

    def status(self):
        return self.player.status()

    def play(self, pos: Optional[int] = None):
        self.player.play(pos)

    def pause(self, pause_flag: int = 1):
        self.player.pause(pause_flag)

    def stop(self):
        self.player.stop()

    def next(self):
        self.player.next()

    def previous(self):
        self.player.previous()

    def setvol(self, vol: int):
        self.player.setvol(vol)

    def command_list_ok_begin(self):
        pass

    def command_list_end(self):
        pass

    def replay_gain_mode(self, mode: str):
        self.replaygain_mode_val = str(mode)

    def replay_gain_status(self) -> Dict[str, str]:
        return {"replay_gain_mode": self.replaygain_mode_val}

    def update(self) -> int:
        return 1


def get_mpd_client() -> Optional[Any]:
    """MPD クライアントの接続を取得（デモモード時は MockMPDClient を返却）"""
    if getattr(config, "DEMO_MODE", False):
        return MockMPDClient()

    if MPDClient is None:
        return None
    try:
        client = MPDClient()
        client.timeout = 5
        client.connect(config.MOODE_IP, config.MOODE_PORT)
        return client
    except Exception:
        return None


def get_moode_status() -> Dict[str, Any]:
    """moOde の再生ステータスと現在曲情報を取得"""
    client = get_mpd_client()
    is_demo = getattr(config, "DEMO_MODE", False) or isinstance(client, MockMPDClient)

    if client is None:
        return {
            "connected": False,
            "is_demo": False,
            "state": "stop",
            "volume": "50",
            "elapsed": 0,
            "duration": 0,
            "song": {},
        }
    try:
        status = client.status()
        song = client.currentsong()
        client.close()
        client.disconnect()

        audio_format = status.get("audio", "")
        sample_rate = ""
        bit_depth = ""
        if audio_format and ":" in audio_format:
            parts = audio_format.split(":")
            if len(parts) >= 2:
                sample_rate = parts[0]
                bit_depth = parts[1]

        song_file = song.get("file", "")
        song_title = song.get("title") or (song_file.split("/")[-1] if song_file else "未選択")
        song_artist = song.get("artist") or "アーティスト未設定"
        song_album = song.get("album") or "moOde Audio Library"

        db_meta = find_track_metadata(file_path=song_file, title=song_title, artist=song_artist)
        description_ja = (db_meta.get("description_ja", "") if db_meta else "") or song.get("description_ja", "")
        description_en = (db_meta.get("description_en", "") if db_meta else "") or song.get("description_en", "")
        title_en = (db_meta.get("title_en", "") if db_meta else "") or song.get("title_en", "")
        artist_en = (db_meta.get("artist_en", "") if db_meta else "") or song.get("artist_en", "")
        rating = (db_meta.get("rating") if db_meta else None) or song.get("rating")
        description = (description_en if config.ANNOUNCE_LANGUAGE == "en" and description_en else description_ja) or description_en or description_ja

        song_info = {
            "title": song_title,
            "artist": song_artist,
            "title_en": title_en,
            "artist_en": artist_en,
            "album": song_album,
            "file": song_file,
            "id": song.get("id", ""),
            "track_id": db_meta.get("id") if db_meta else song.get("track_id"),
            "sample_rate": sample_rate,
            "bit_depth": bit_depth,
            "is_hires": (int(sample_rate) > 48000 or int(bit_depth) > 16) if sample_rate.isdigit() and bit_depth.isdigit() else (db_meta.get("is_hires", 0) == 1 if db_meta else song.get("is_hires", False)),
            "rating": rating,
            "description": description,
            "description_ja": description_ja,
            "description_en": description_en,
        }

        return {
            "connected": True,
            "is_demo": is_demo,
            "state": status.get("state", "stop"),
            "volume": status.get("volume", "50"),
            "elapsed": float(status.get("elapsed", 0)),
            "duration": float(status.get("duration", 0)),
            "song": song_info,
        }
    except Exception as e:
        return {
            "connected": False,
            "is_demo": is_demo,
            "error": str(e),
            "state": "stop",
            "volume": "50",
            "elapsed": 0,
            "duration": 0,
            "song": {},
        }


def ensure_replaygain_mode(client: Optional[Any] = None) -> str:
    """MPD の ReplayGain モードを確認し、目標モード（config.REPLAYGAIN_MODE: デフォルト 'track'）を送信・有効化する。"""
    own_client = False
    if client is None:
        client = get_mpd_client()
        if client is None:
            return "off"
        own_client = True

    try:
        target_mode = getattr(config, "REPLAYGAIN_MODE", "track").lower()
        status = client.replay_gain_status()
        current_mode = status.get("replay_gain_mode", "").lower()
        if current_mode != target_mode:
            client.replay_gain_mode(target_mode)
            print(f"🎚️ [moOde] MPD ReplayGain モードを '{current_mode}' ➔ '{target_mode}' に更新・有効化しました。", flush=True)
            return target_mode
        return current_mode
    except Exception as e:
        # モックまたは非対応環境
        return getattr(config, "REPLAYGAIN_MODE", "track")
    finally:
        if own_client:
            try:
                client.close()
                client.disconnect()
            except Exception:
                pass


def update_mpd_database() -> Dict[str, Any]:
    """MPD にライブラリ更新（mpc update）を要求し、ReplayGain タグ等の最新メタデータを再読み込みさせる"""
    client = get_mpd_client()
    if client is None:
        return {"success": False, "message": "MPD に接続できませんでした。"}
    try:
        job_id = client.update()
        print(f"🔄 [moOde] MPD ライブラリ更新を開始しました (job id: {job_id})", flush=True)
        return {"success": True, "job_id": job_id, "message": f"MPD ライブラリ更新を開始しました (job: {job_id})"}
    except Exception as e:
        print(f"⚠️ [moOde] MPD update エラー: {e}", flush=True)
        return {"success": False, "message": str(e)}
    finally:
        try:
            client.close()
            client.disconnect()
        except Exception:
            pass


def safe_start_playback(
    client: Optional[Any] = None,
    pos: Optional[int] = None,
    pre_decode_delay_sec: Optional[float] = None,
) -> bool:
    """ReplayGain を確実に適用し、曲頭の音量飛び出しを完全防止する安全な再生開始処理。
    （ReplayGain 有効化 ＋ 初期ボリューム一時消音・復帰方式）

    シーケンス:
    1. MPD の ReplayGain モードが確実に有効化 ('track' 等) されていることを確認・設定。
    2. 現在の再生状態を確認。
       - すでに一時停止（pause）状態の場合:
         一時停止を解除（pause 0）して再生を再開。
       - 停止（stop）状態からの再生開始:
         a. 現在の音量を退避（orig_vol）。
         b. 一時的に音量を 0 (完全消音) に設定し、ReplayGain未補正の曲頭PCM出力を遮断。
         c. play(pos) で再生を開始（MPDのデコーダが起動し、ReplayGainヘッダ解析とスケール演算を開始）。
         d. 完全な無音状態で ReplayGain タグ解析完了まで微小待機（pre_decode_delay_sec: 0.35秒）。
         e. 本来の音量（orig_vol）に復帰。第1サンプルから ReplayGain が100%適用された状態で音楽がスタート。
    3. ボリューム制御不可（Bit-perfect / mixerなし）環境時は、アトミックポーズ方式に自動フォールバック。
    """
    delay = pre_decode_delay_sec if pre_decode_delay_sec is not None else getattr(config, "PRE_DECODE_DELAY_SEC", 0.35)
    own_client = False
    if client is None:
        client = get_mpd_client()
        if client is None:
            print("⚠️ [safe_start_playback] MPD クライアントに接続できませんでした。", flush=True)
            return False
        own_client = True

    try:
        # 1. MPD の ReplayGain モードが有効化されていることを保証
        ensure_replaygain_mode(client)

        status = {}
        try:
            status = client.status()
        except Exception:
            pass

        state = status.get("state", "stop")

        if state == "pause":
            client.pause(0)
            print("▶️ [moOde] 一時停止を解除して再生を開始しました (ReplayGain適用中)。", flush=True)
            return True

        elif state == "play" and pos is None:
            return True

        # stop 状態（または pos 指定での新規再生開始）:
        vol_str = str(status.get("volume", "-1"))
        has_volume = vol_str.isdigit() and int(vol_str) >= 0
        orig_vol = int(vol_str) if has_volume else None

        # ボリューム制御が利用可能な場合: 初期消音 ➔ play ➔ ディレイ ➔ 音量復帰
        if has_volume and orig_vol is not None and orig_vol > 0:
            try:
                # A. 音量を 0 に設定（DACへのPCM出力を完全消音）
                client.setvol(0)

                # B. 再生を開始（デコーダ起動・ReplayGainヘッダ解析開始）
                if pos is not None:
                    client.play(int(pos))
                else:
                    client.play()

                # C. 完全消音のまま ReplayGain 解析完了を待機
                if delay > 0:
                    time.sleep(delay)

                # D. 本来の音量に復帰（ReplayGain が適用されたクリーンな音量でスタート）
                client.setvol(orig_vol)
                print(f"▶️ [moOde] ReplayGain 事前適用完了 (音量復帰: {orig_vol}%, 待機: {delay:.2f}秒)。", flush=True)
                return True
            except Exception as vol_err:
                print(f"⚠️ [moOde] ボリューム一時消音フォールバック: {vol_err}", flush=True)

        # ボリューム制御不可（Bit-perfect / mixerなし）時のフォールバック:
        try:
            client.command_list_ok_begin()
            if pos is not None:
                client.play(int(pos))
            else:
                client.play()
            client.pause(1)
            client.command_list_end()
        except Exception:
            if pos is not None:
                client.play(int(pos))
            else:
                client.play()
            try:
                client.pause(1)
            except Exception:
                pass

        if delay > 0:
            time.sleep(delay)

        client.pause(0)
        print(f"▶️ [moOde] 再生を開始しました (ReplayGain有効化済み、待機: {delay:.2f}秒)。", flush=True)
        return True

    except Exception as e:
        print(f"❌ [safe_start_playback] 再生開始処理エラー: {e}", flush=True)
        try:
            client.stop()
        except Exception:
            pass
        return False
    finally:
        if own_client:
            try:
                client.close()
                client.disconnect()
            except Exception:
                pass


def play_single_track(
    file_path: str,
    pre_decode_delay_sec: Optional[float] = None,
) -> bool:
    """単曲再生（キューのクリア -> 追加 -> safe_start_playback）。
    ReplayGainを確実に適用し、曲頭の音量飛び出しを完全防止する。
    """
    delay = pre_decode_delay_sec if pre_decode_delay_sec is not None else getattr(config, "PRE_DECODE_DELAY_SEC", 0.35)
    client = get_mpd_client()
    if client is None:
        print(f"❌ [play_single_track] MPD ({config.MOODE_IP}:{config.MOODE_PORT}) に接続できませんでした。", flush=True)
        return False

    try:
        try:
            client.stop()
        except Exception:
            pass
        client.clear()
        client.add(file_path)
        return safe_start_playback(client, pos=0, pre_decode_delay_sec=delay)
    except Exception as e:
        print(f"❌ [play_single_track] 再生エラー: {e}", flush=True)
        return False
    finally:
        try:
            client.close()
            client.disconnect()
        except Exception:
            pass


def control_moode(command: Dict[str, Any]) -> Dict[str, Any]:
    """music_meta.db から選曲し、MPD 経由で moOde audio を操作"""
    action = command.get("action")
    query = command.get("query", "").strip()

    # "play" で query が指定されている場合は自動的に "play_search"（選曲再生）として扱う
    if action == "play" and query:
        action = "play_search"

    result = {
        "success": False,
        "action": action,
        "tracks_added": [],
        "track_info": None,
        "description": "",
        "description_ja": "",
        "description_en": "",
        "message": "",
    }

    if action == "unknown" or not action:
        print("🎵 [moOde] 音楽操作はありません。", flush=True)
        result["message"] = "音楽操作なし"
        return result

    client = get_mpd_client()
    if client is None:
        print(f"❌ [moOde] {config.MOODE_IP}:{config.MOODE_PORT} に接続できませんでした。", flush=True)
        result["message"] = f"moOde ({config.MOODE_IP}) に接続できませんでした。"
        return result

    global last_announced_file, last_announced_songid
    try:
        if action == "play_search":
            # 先に音楽を停止してキューを初期化（自動再生の暴発を防止）
            try:
                client.stop()
            except Exception:
                pass
            client.clear()

            print(f"🔍 [music_meta.db] 楽曲検索中: query='{query}'", flush=True)
            _broadcast("db", f"🔍 楽曲データベースを検索中 (SQLite): 「{query or 'おすすめ'}」")
            db_tracks = search_tracks_from_db(query, limit=15)
            print(f"📊 [music_meta.db] 該当曲数: {len(db_tracks)} 件", flush=True)

            if db_tracks:
                _broadcast("moode", f"🎵 moOde 再生キューを更新中 ({len(db_tracks)}曲をセット)...")
                added_tracks = add_db_tracks_to_mpd(client, db_tracks)
                added_count = len(added_tracks)

                # MPD の ReplayGain モードを確実に有効化
                ensure_replaygain_mode(client)

                # MPD追加成功曲があればそれをベースに、なければDB検索1件目を使用
                first_track = added_tracks[0] if added_tracks else db_tracks[0]
                first_title = first_track.get("title", "未設定")
                first_artist = first_track.get("artist", "アーティスト未設定")
                first_title_en = first_track.get("title_en", "")
                first_artist_en = first_track.get("artist_en", "")
                first_file = first_track.get("relative_path", "")
                description_ja = first_track.get("description_ja", "")
                description_en = first_track.get("description_en", "")
                description = (description_en if config.ANNOUNCE_LANGUAGE == "en" and description_en else description_ja) or description_en or description_ja

                # MPD のプレイリスト先頭情報を取得して同期（監視ループでの誤検知・自己曲紹介を防止）
                playlist_items = client.playlistinfo()
                if playlist_items:
                    first_mpd = playlist_items[0]
                    last_announced_file = first_mpd.get("file", first_file)
                    last_announced_songid = first_mpd.get("id", "")
                else:
                    last_announced_file = first_file
                    last_announced_songid = ""

                result["tracks_added"] = [t.get("title", "") for t in added_tracks]
                result["selected_tracks_meta"] = added_tracks if added_tracks else db_tracks
                result["track_info"] = {
                    "title": first_title,
                    "artist": first_artist,
                    "title_en": first_title_en,
                    "artist_en": first_artist_en,
                    "file": last_announced_file,
                    "description_ja": description_ja,
                    "description_en": description_en,
                }
                result["description"] = description
                result["description_ja"] = description_ja
                result["description_en"] = description_en
                result["success"] = True
                result["needs_playback"] = True
                result["message"] = f"「{query or 'おすすめ'}」に該当する楽曲 ({added_count or len(db_tracks)}曲) をセットしました。"

                print(f"🎵 [moOde] '{query}' の楽曲をセットしました ({added_count}曲 キュー追加, 先頭={last_announced_file})", flush=True)
                if description_ja or description_en:
                    print(f"📖 [Description 取得成功] (日): {description_ja} | (英): {description_en}", flush=True)
                else:
                    print(f"ℹ️ [Description] DB内に解説文が見つかりませんでした (title='{first_title}', artist='{first_artist}')", flush=True)
            else:
                result["message"] = f"「{query}」に該当する曲がデータベースに見つかりませんでした。"
                print(f"⚠️ [music_meta.db] '{query}' に該当する曲が見つかりません", flush=True)

        elif action == "play":
            status = client.status()
            pl_len = int(status.get("playlistlength", 0))
            if pl_len == 0:
                # キューが空の場合は自動で選曲して再生
                print("🎵 [moOde] キューが空のため、ランダム選曲を実行します...", flush=True)
                client.close()
                client.disconnect()
                return control_moode({"action": "play_search", "query": ""})

            _broadcast("moode", "▶️ moOde 音楽再生を再開中...", auto_idle_sec=3.0)
            safe_start_playback(client)
            result["success"] = True
            result["message"] = "音楽の再生を再開しました。"
        elif action == "pause":
            _broadcast("moode", "⏸️ 音楽を一時停止中...", auto_idle_sec=3.0)
            client.pause(1)
            result["success"] = True
            result["message"] = "音楽を一時停止しました。"
        elif action == "stop":
            _broadcast("moode", "⏹️ 音楽再生を停止中...", auto_idle_sec=3.0)
            client.stop()
            result["success"] = True
            result["message"] = "音楽を停止しました。"
        elif action in ("next", "previous"):
            act_text = "次の曲にスキップ" if action == "next" else "前の曲に戻る"
            _broadcast("moode", f"⏭️ moOde 操作: {act_text}中...")
            if action == "next":
                client.next()
                result["message"] = "次の曲にスキップしました。"
            else:
                client.previous()
                result["message"] = "前の曲に戻りました。"

            time.sleep(0.3)
            new_song = client.currentsong()
            new_file = new_song.get("file", "")
            last_announced_file = new_file
            last_announced_songid = new_song.get("id", "")

            # 解説文読み上げのために一旦一時停止
            client.pause(1)
            result["success"] = True
            result["needs_playback"] = True

            new_title = new_song.get("title") or (new_file.split("/")[-1] if new_file else "次の曲")
            new_artist = new_song.get("artist") or "アーティスト未設定"
            db_meta = find_track_metadata(file_path=new_file, title=new_title, artist=new_artist)
            description_ja = db_meta.get("description_ja", "") if db_meta else ""
            description_en = db_meta.get("description_en", "") if db_meta else ""
            title_en = db_meta.get("title_en", "") if db_meta else ""
            artist_en = db_meta.get("artist_en", "") if db_meta else ""
            description = (description_en if config.ANNOUNCE_LANGUAGE == "en" and description_en else description_ja) or description_en or description_ja

            result["track_info"] = {
                "title": new_title,
                "artist": new_artist,
                "title_en": title_en,
                "artist_en": artist_en,
                "file": new_file,
                "description_ja": description_ja,
                "description_en": description_en,
            }
            result["description"] = description
            result["description_ja"] = description_ja
            result["description_en"] = description_en

        elif action == "volume":
            vol = command.get("value", 50)
            client.setvol(int(vol))
            result["success"] = True
            result["message"] = f"音量を {vol}% に設定しました。"

        elif action in ("rate_good", "rate_bad", "good", "bad", "like", "dislike"):
            is_good = action in ("rate_good", "good", "like")
            cur_song = client.currentsong()
            cur_file = cur_song.get("file", "")
            cur_title = cur_song.get("title") or (cur_file.split("/")[-1] if cur_file else "再生中の曲")
            cur_artist = cur_song.get("artist") or ""

            act_str = "good" if is_good else "bad"
            _broadcast("db", f"⭐ 楽曲を評価中 ({'Good 👍' if is_good else 'Bad 👎'}): 『{cur_title}』")
            rate_res = update_track_rating(
                action=act_str,
                file_path=cur_file,
            )
            result["success"] = rate_res.get("success", False)
            result["rating_result"] = rate_res
            result["message"] = rate_res.get("message", "評価を更新しました。")
            result["track_info"] = {
                "title": cur_title,
                "artist": cur_artist,
                "file": cur_file,
                "rating": rate_res.get("rating"),
            }

        client.close()
        client.disconnect()
    except Exception as e:
        print(f"❌ moOde 操作エラー: {e}")
        traceback.print_exc()
        result["message"] = f"moOde 操作エラー: {e}"

    return result
