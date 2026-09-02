"""music_meta.db (SQLite) アクセス層

voice_bot.py から切り出した楽曲メタデータ検索・選曲モジュール。
- find_track_metadata: ファイル名 / タイトル / アーティストから楽曲情報・解説文を取得
- search_tracks_from_db: ユーザー要望に合致する楽曲をランダム抽出
- add_db_tracks_to_mpd: 検索結果を MPD キューに追加
"""
import os
import random
import re
import sqlite3
from typing import Any, Dict, List, Optional

from . import tagger

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "music_meta.db")

# ランダム選曲の重複防止用（直近に再生した楽曲IDを保持）
recent_played_track_ids: List[int] = []


def ensure_rating_column_exists(conn: sqlite3.Connection):
    """tracks テーブルに rating カラムが存在しない場合は自動で追加"""
    try:
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(tracks);")
        columns = [col[1] for col in cur.fetchall()]
        if "rating" not in columns:
            cur.execute("ALTER TABLE tracks ADD COLUMN rating INTEGER DEFAULT NULL;")
            conn.commit()
            print("✨ [DB] tracks テーブルに 'rating' カラムを追加しました。")
    except Exception as e:
        print(f"⚠️ [DB rating カラム確認エラー]: {e}")


def find_track_metadata(
    file_path: Optional[str] = None,
    title: Optional[str] = None,
    artist: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """music_meta.db からファイル名やタイトル・アーティスト名で楽曲情報・解説文・評価を取得"""
    if not os.path.exists(DB_PATH):
        return None
    try:
        conn = sqlite3.connect(DB_PATH)
        ensure_rating_column_exists(conn)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # 1. ファイル名で検索（完全一致、末尾一致、拡張子なし一致）
        if file_path:
            norm_path = file_path.replace("\\", "/")
            fname = norm_path.split("/")[-1]
            fname_stem = os.path.splitext(fname)[0]

            cur.execute(
                """
                SELECT id, title, artist, title_en, artist_en, album, relative_path, file_path, genre, mood, energy_level, is_hires, rating, description_ja, description_en
                FROM tracks
                WHERE file_path LIKE ? OR relative_path LIKE ? OR relative_path = ? OR file_path LIKE ? OR relative_path LIKE ?
                LIMIT 1;
            """,
                (f"%{fname}", f"%{fname}", norm_path, f"%{fname_stem}%", f"%{fname_stem}%"),
            )
            row = cur.fetchone()
            if row and (row["description_ja"] or row["description_en"] or row["rating"] is not None):
                conn.close()
                return dict(row)

        # 2. タイトルとアーティストで検索
        if title and artist and artist != "アーティスト未設定" and artist != "Unknown":
            cur.execute(
                """
                SELECT id, title, artist, title_en, artist_en, album, relative_path, file_path, genre, mood, energy_level, is_hires, rating, description_ja, description_en
                FROM tracks
                WHERE (title LIKE ? OR ? LIKE '%' || title || '%') AND (artist LIKE ? OR ? LIKE '%' || artist || '%')
                LIMIT 1;
            """,
                (f"%{title}%", title, f"%{artist}%", artist),
            )
            row = cur.fetchone()
            if row and (row["description_ja"] or row["description_en"] or row["rating"] is not None):
                conn.close()
                return dict(row)

        # 3. タイトルのみで検索
        if title and title != "未選択" and title != "Unknown":
            cur.execute(
                """
                SELECT id, title, artist, title_en, artist_en, album, relative_path, file_path, genre, mood, energy_level, is_hires, rating, description_ja, description_en
                FROM tracks
                WHERE title LIKE ? OR ? LIKE '%' || title || '%'
                ORDER BY CASE WHEN title = ? THEN 0 ELSE 1 END
                LIMIT 1;
            """,
                (f"%{title}%", title, title),
            )
            row = cur.fetchone()
            if row:
                conn.close()
                return dict(row)

        conn.close()
    except Exception as e:
        print(f"⚠️ DB詳細取得エラー: {e}")
    return None


def get_track_rating(
    file_path: Optional[str] = None,
    track_id: Optional[int] = None,
) -> Optional[int]:
    """指定された楽曲の現在の評価（1〜5、未評価時は None）を取得"""
    if not os.path.exists(DB_PATH):
        return None
    try:
        conn = sqlite3.connect(DB_PATH)
        ensure_rating_column_exists(conn)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        row = None
        if track_id is not None:
            cur.execute("SELECT rating FROM tracks WHERE id = ? LIMIT 1;", (track_id,))
            row = cur.fetchone()

        if not row and file_path:
            norm_path = file_path.replace("\\", "/")
            fname = norm_path.split("/")[-1]
            fname_stem = os.path.splitext(fname)[0]
            cur.execute(
                """
                SELECT rating FROM tracks
                WHERE file_path = ? OR relative_path = ? OR relative_path LIKE ? OR file_path LIKE ? OR relative_path LIKE ?
                LIMIT 1;
            """,
                (file_path, norm_path, f"%{fname}", f"%{fname}", f"%{fname_stem}%"),
            )
            row = cur.fetchone()

        conn.close()
        if row and row["rating"] is not None:
            return int(row["rating"])
        return None
    except Exception as e:
        print(f"⚠️ [get_track_rating エラー]: {e}")
        return None


def update_track_rating(
    action: str = "good",
    file_path: Optional[str] = None,
    track_id: Optional[int] = None,
    direct_rating: Optional[int] = None,
) -> Dict[str, Any]:
    """
    楽曲の評価を更新する。
    【ルール】
    - 現在「無印 (None/0)」の場合:
      - good ➔ ★3
      - bad  ➔ ★2
    - すでに評価されている場合:
      - good ➔ ★+1 (最大★5)
      - bad  ➔ ★-1 (最小★1)
    - direct_rating 指定時: 1〜5 の数値を直接設定
    """
    if not os.path.exists(DB_PATH):
        return {"success": False, "message": "データベースが見つかりません。"}

    try:
        conn = sqlite3.connect(DB_PATH)
        ensure_rating_column_exists(conn)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # 対象レコードを検索
        row = None
        if track_id is not None:
            cur.execute(
                "SELECT id, title, artist, file_path, relative_path, rating FROM tracks WHERE id = ? LIMIT 1;",
                (track_id,),
            )
            row = cur.fetchone()

        if not row and file_path:
            norm_path = file_path.replace("\\", "/")
            # プレフィックス除去 (例: "NAS/Artist/..." -> "Artist/...")
            stripped_rel = norm_path
            for pfx in ("NAS/", "USB/", "SDCARD/", "nas/", "usb/", "sdcard/"):
                if stripped_rel.startswith(pfx):
                    stripped_rel = stripped_rel[len(pfx):].lstrip("/")
                    break

            fname = norm_path.split("/")[-1]
            fname_stem = os.path.splitext(fname)[0]
            cur.execute(
                """
                SELECT id, title, artist, file_path, relative_path, rating FROM tracks
                WHERE file_path = ? OR relative_path = ? OR relative_path = ?
                   OR relative_path LIKE ? OR file_path LIKE ? OR relative_path LIKE ?
                ORDER BY CASE
                    WHEN relative_path = ? OR relative_path = ? THEN 0
                    WHEN file_path = ? THEN 1
                    ELSE 2 END
                LIMIT 1;
            """,
                (file_path, norm_path, stripped_rel, f"%{fname}", f"%{fname}", f"%{fname_stem}%", norm_path, stripped_rel, file_path),
            )
            row = cur.fetchone()

        if not row:
            # DB レコードが見つからない場合でも、もし実ファイルが存在すればファイルタグ書き込みを試行
            real_file_path = tagger.resolve_audio_file_path(file_path, file_path)
            if real_file_path and os.path.isfile(real_file_path):
                cur_rate = tagger.read_rating_from_file(real_file_path)
                if direct_rating is not None:
                    new_rating = max(1, min(5, int(direct_rating)))
                elif cur_rate is None or cur_rate == 0:
                    new_rating = 3 if action.lower() in ("good", "like", "up", "plus") else 2
                else:
                    new_rating = min(5, cur_rate + 1) if action.lower() in ("good", "like", "up", "plus") else max(1, cur_rate - 1)

                tag_written = tagger.write_rating_to_file(real_file_path, new_rating)
                conn.close()
                return {
                    "success": True,
                    "title": os.path.basename(real_file_path),
                    "file": real_file_path,
                    "real_file_path": real_file_path,
                    "tag_written": tag_written,
                    "old_rating": cur_rate,
                    "rating": new_rating,
                    "action": action,
                    "message": f"音源ファイル『{os.path.basename(real_file_path)}』のタグに ★{new_rating} を書き込みました。",
                }

            conn.close()
            return {"success": False, "message": "評価対象の楽曲がデータベースおよび音源フォルダに見つかりませんでした。"}

        current_rating = row["rating"]
        old_rating = current_rating

        # 新しい評価の計算
        if direct_rating is not None:
            new_rating = max(1, min(5, int(direct_rating)))
        elif current_rating is None or current_rating == 0:
            # 無印の場合
            if action.lower() in ("good", "like", "up", "plus"):
                new_rating = 3
            else:
                new_rating = 2
        else:
            # 既に評価済みの場合
            if action.lower() in ("good", "like", "up", "plus"):
                new_rating = min(5, int(current_rating) + 1)
            else:
                new_rating = max(1, int(current_rating) - 1)

        # 1. 音楽ファイル自体へのメタデータタグ書き込み (FLAC/MP3/M4A等)
        raw_file_path = row["file_path"]
        raw_rel_path = row["relative_path"]
        # 多重候補から探索 (music_meta.db の UNC パス \\homenas\music\... および 相対パス)
        real_file_path = (
            tagger.resolve_audio_file_path(raw_file_path, raw_rel_path)
            or (tagger.resolve_audio_file_path(file_path, None) if file_path else None)
        )
        tag_written = False
        tag_msg = ""
        tag_error = None

        if real_file_path:
            tag_written, tag_msg = tagger.write_rating_to_file(real_file_path, new_rating)
            if not tag_written:
                tag_error = tag_msg
        else:
            tag_msg = f"音源ファイルが見つかりません (DBパス: {raw_file_path}, 相対パス: {raw_rel_path}, 探索先: /mnt/music)"
            tag_error = tag_msg
            print(f"⚠️ [Rating Update] NAS音源ファイルが見つからないためタグ書き込みをスキップしました。", flush=True)
            print(f"   DB登録パス: {raw_file_path}", flush=True)
            print(f"   相対パス: {raw_rel_path}", flush=True)
            print(f"   💡 Jetson 端末上で NAS が /mnt/music にマウントされているか確認してください。", flush=True)

        # 2. DB 更新
        target_id = row["id"]
        cur.execute("UPDATE tracks SET rating = ? WHERE id = ?;", (new_rating, target_id))
        conn.commit()
        conn.close()

        track_title = row["title"] or "楽曲"
        track_artist = row["artist"] or ""
        artist_str = f"（{track_artist}）" if track_artist and track_artist != "Unknown" else ""

        print(
            f"⭐ [Rating Update] 『{track_title}』{artist_str} の評価を更新: {old_rating} ➔ ★{new_rating} (action={action}, file_tagged={tag_written}, path={real_file_path})",
            flush=True,
        )
        if not tag_written:
            print(f"❌ [Rating Update] タグ書き込み失敗理由: {tag_msg}", flush=True)

        if tag_written:
            user_msg = f"『{track_title}』を ★{new_rating} に評価しました。（NAS音源ファイルタグにも保存完了）"
        else:
            user_msg = f"『{track_title}』を ★{new_rating} に評価しました。（⚠️ 音源ファイルへのタグ書き込み失敗: {tag_msg}）"

        return {
            "success": True,
            "track_id": target_id,
            "title": track_title,
            "artist": track_artist,
            "file": row["relative_path"] or row["file_path"],
            "real_file_path": real_file_path,
            "tag_written": tag_written,
            "tag_msg": tag_msg,
            "tag_error": tag_error,
            "old_rating": old_rating,
            "rating": new_rating,
            "action": action,
            "message": user_msg,
        }

    except Exception as e:
        print(f"⚠️ [update_track_rating エラー]: {e}")
        return {"success": False, "message": f"評価更新中にエラーが発生しました: {e}"}



def search_tracks_from_db(query: str, limit: int = 15) -> List[Dict[str, Any]]:
    """music_meta.db からユーザー要望（ジャンル、ムード、エネルギー、ハイレゾ、アーティスト、曲名等）に合致する楽曲を完全ランダム抽出"""
    global recent_played_track_ids
    if not os.path.exists(DB_PATH):
        print(f"⚠️ [DB] {DB_PATH} が存在しません。")
        return []
    try:
        conn = sqlite3.connect(DB_PATH)
        ensure_rating_column_exists(conn)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # 英語・日本語のプレフィックス・ストップワードをクリーンアップ
        clean_q = query.strip()
        clean_q = re.sub(r"^(?:play\s+(?:some\s+|me\s+)?|put\s+on\s+|listen\s+to\s+)", "", clean_q, flags=re.IGNORECASE)
        clean_q = re.sub(r"\s+(?:please|music|song|songs|track|tracks)$", "", clean_q, flags=re.IGNORECASE)
        for sw in ["をかけて", "を流して", "を再生して", "かけて", "流して", "再生して", "聴きたい", "聴かせて", "の曲", "曲", "音楽"]:
            clean_q = clean_q.replace(sw, "")
        clean_q = clean_q.strip()

        conditions = []
        params = []

        # 直近に再生した楽曲IDは除外候補（同じ曲の連続再生を防止）
        recent_ids = recent_played_track_ids[-30:] if recent_played_track_ids else []

        # 1. ジャンル判定マッピング（日英両対応・派生語対応）
        genre_keywords = {
            "ジャズ": "ジャズ", "jazz": "ジャズ", "fusion": "ジャズ", "bossa": "ジャズ",
            "ロック": "ロック", "rock": "ロック", "hard rock": "ロック", "punk": "ロック",
            "ポップ": "ポップ", "ポップス": "ポップ", "pop": "ポップ", "pops": "ポップ", "j-pop": "ポップ", "jpop": "ポップ",
            "クラシック": "クラシック", "classic": "クラシック", "classical": "クラシック", "orchestra": "クラシック",
            "ブルース": "ブルース", "blues": "ブルース",
            "ソウル": "R&B・ソウル", "r&b": "R&B・ソウル", "rnb": "R&B・ソウル", "soul": "R&B・ソウル", "funk": "R&B・ソウル",
            "エレクトロニック": "エレクトロニック", "テクノ": "エレクトロニック", "edm": "エレクトロニック", "electronic": "エレクトロニック", "techno": "エレクトロニック", "ambient": "エレクトロニック",
            "フォーク": "フォーク・カントリー", "カントリー": "フォーク・カントリー", "folk": "フォーク・カントリー", "country": "フォーク・カントリー",
            "ヒップホップ": "ヒップホップ", "hiphop": "ヒップホップ", "hip-hop": "ヒップホップ", "ラップ": "ヒップホップ", "rap": "ヒップホップ",
            "サントラ": "サウンドトラック・インスト", "サウンドトラック": "サウンドトラック・インスト", "インスト": "サウンドトラック・インスト", "soundtrack": "サウンドトラック・インスト", "ost": "サウンドトラック・インスト", "instrumental": "サウンドトラック・インスト",
        }

        matched_genres = [db_genre for kw, db_genre in genre_keywords.items() if re.search(rf"\b{re.escape(kw)}\b", clean_q.lower()) or kw in clean_q.lower()]
        # 重複除去
        matched_genres = list(dict.fromkeys(matched_genres))
        if matched_genres:
            genre_clause = " OR ".join(["genre LIKE ?" for _ in matched_genres])
            conditions.append(f"({genre_clause})")
            params.extend([f"%{g}%" for g in matched_genres])

        # 2. ハイレゾ判定
        if any(k in clean_q.lower() for k in ["ハイレゾ", "hires", "hi-res", "高音質"]):
            conditions.append("is_hires = 1")

        # 3. エネルギー / 気分判定 (日英対応)
        if any(k in clean_q.lower() for k in ["静か", "落ち着", "リラックス", "穏やか", "眠", "バラード", "癒", "calm", "relax", "quiet", "peaceful", "sleep", "slow"]):
            conditions.append("(energy_level <= 2 OR mood LIKE '%Calm%' OR mood LIKE '%Relax%')")
        elif any(k in clean_q.lower() for k in ["元気", "激し", "アップテンポ", "ノリ", "ドライブ", "テンション", "energetic", "upbeat", "fast", "party", "drive"]):
            conditions.append("(energy_level >= 4 OR mood LIKE '%Energetic%' OR mood LIKE '%Upbeat%')")

        # 4. 邦楽 / 洋楽判定
        if any(k in clean_q.lower() for k in ["邦楽", "j-pop", "jpop", "日本の曲", "日本語", "japanese"]):
            conditions.append("music_category = '邦楽'")
        elif any(k in clean_q.lower() for k in ["洋楽", "海外", "western", "english"]):
            conditions.append("music_category = '洋楽'")

        # 5. 一般キーワード（アーティスト名、曲名、アルバム名、解説文）
        keyword_q = clean_q
        if keyword_q and not matched_genres:
            words = keyword_q.split()
            kw_conditions = []
            for w in words:
                kw_conditions.append("(title LIKE ? OR artist LIKE ? OR album LIKE ? OR description_ja LIKE ? OR description_en LIKE ?)")
                params.extend([f"%{w}%", f"%{w}%", f"%{w}%", f"%{w}%", f"%{w}%"])
            if kw_conditions:
                conditions.append(" AND ".join(kw_conditions))

        # 直近再生した曲を除外する条件を追加（ヒット数が十分に取れる場合）
        base_where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        # まず直近再生除外 ＋ description ありの候補をランダムに50件取得
        sql = f"""
            SELECT id, title, artist, title_en, artist_en, album, relative_path, file_path, genre, mood, energy_level, is_hires, rating, description_ja, description_en
            FROM tracks
            {base_where}
            ORDER BY (CASE WHEN (description_ja IS NOT NULL AND description_ja != '') OR (description_en IS NOT NULL AND description_en != '') THEN 0 ELSE 1 END), RANDOM()
            LIMIT 50;
        """
        cur.execute(sql, params)
        rows = [dict(r) for r in cur.fetchall()]

        # 直近再生曲を除外したリストを作成
        filtered_rows = [r for r in rows if r["id"] not in recent_ids]
        candidate_rows = filtered_rows if len(filtered_rows) >= limit else rows

        # ヒットしなかった場合、キーワードの部分一致でフォールバック
        if not candidate_rows and keyword_q:
            cur.execute(f"""
                SELECT id, title, artist, title_en, artist_en, album, relative_path, file_path, genre, mood, energy_level, is_hires, rating, description_ja, description_en
                FROM tracks
                WHERE title LIKE ? OR artist LIKE ? OR album LIKE ? OR description_ja LIKE ? OR description_en LIKE ?
                ORDER BY (CASE WHEN (description_ja IS NOT NULL AND description_ja != '') OR (description_en IS NOT NULL AND description_en != '') THEN 0 ELSE 1 END), RANDOM()
                LIMIT 50;
            """, (f"%{keyword_q}%", f"%{keyword_q}%", f"%{keyword_q}%", f"%{keyword_q}%", f"%{keyword_q}%"))
            candidate_rows = [dict(r) for r in cur.fetchall()]

        # それでもヒットしない場合、解説文付きの曲からランダムに取得
        if not candidate_rows:
            cur.execute("""
                SELECT id, title, artist, title_en, artist_en, album, relative_path, file_path, genre, mood, energy_level, is_hires, rating, description_ja, description_en
                FROM tracks
                WHERE (description_ja IS NOT NULL AND description_ja != '') OR (description_en IS NOT NULL AND description_en != '')
                ORDER BY RANDOM()
                LIMIT 50;
            """)
            candidate_rows = [dict(r) for r in cur.fetchall()]

        # それでも無ければテーブル全体からランダム取得
        if not candidate_rows:
            cur.execute("SELECT id, title, artist, title_en, artist_en, album, relative_path, file_path, genre, mood, energy_level, is_hires, rating, description_ja, description_en FROM tracks ORDER BY RANDOM() LIMIT 50;")
            candidate_rows = [dict(r) for r in cur.fetchall()]

        # Python 側でも再度シャッフルして完全なランダム性を確保
        random.shuffle(candidate_rows)
        selected_tracks = candidate_rows[:limit]

        # 選択した曲の ID を直近再生リストに追加（最大50件保持）
        for t in selected_tracks:
            t_id = t.get("id")
            if t_id and t_id not in recent_played_track_ids:
                recent_played_track_ids.append(t_id)
        if len(recent_played_track_ids) > 50:
            recent_played_track_ids = recent_played_track_ids[-50:]

        conn.close()
        return selected_tracks
    except Exception as e:
        print(f"⚠️ [DB検索エラー]: {e}")
        return []


def add_db_tracks_to_mpd(client: Any, db_tracks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """DB検索結果の楽曲を MPD のキューに追加し、追加に成功した楽曲リストを返す (多重フォールバック対応)"""
    added_tracks = []
    for track in db_tracks:
        rel_path = track.get("relative_path") or ""
        fname = rel_path.replace("\\", "/").split("/")[-1] if rel_path else ""
        norm_rel = rel_path.replace("\\", "/") if rel_path else ""
        title = track.get("title") or fname
        artist = track.get("artist") or ""

        added = False

        # 1. ファイル名（basename）で MPD 検索
        if fname:
            for search_type in ["file", "filename", "any"]:
                try:
                    search_res = client.search(search_type, fname)
                    if search_res:
                        client.add(search_res[0]["file"])
                        added_tracks.append(track)
                        added = True
                        break
                except Exception:
                    pass

        # 2. 相対パスでの直接追加および検索
        if not added and norm_rel:
            for p in [norm_rel, f"NAS/{norm_rel}", f"NAS/music/{norm_rel}", f"NAS/homenas_music/{norm_rel}"]:
                try:
                    client.add(p)
                    added_tracks.append(track)
                    added = True
                    break
                except Exception:
                    pass
            if not added:
                try:
                    search_res = client.search("file", norm_rel)
                    if search_res:
                        client.add(search_res[0]["file"])
                        added_tracks.append(track)
                        added = True
                except Exception:
                    pass

        # 3. タイトル＆アーティストで MPD 検索
        if not added and title:
            try:
                if artist and artist not in ("アーティスト未設定", "Unknown", "unknown", "None"):
                    search_res = client.search("title", title, "artist", artist)
                else:
                    search_res = client.search("title", title)
                if search_res:
                    client.add(search_res[0]["file"])
                    added_tracks.append(track)
                    added = True
            except Exception:
                pass

        # 4. タイトル単体での検索
        if not added and title and title != "未選択":
            try:
                search_res = client.search("title", title)
                if search_res:
                    client.add(search_res[0]["file"])
                    added_tracks.append(track)
                    added = True
            except Exception:
                pass

    # 5. 【フェイルセーフ】もし MPD への個別追加がすべて失敗した場合、MPD ライブラリ全体から直接検索して追加
    if not added_tracks and db_tracks:
        print("⚠️ [MPD Add] 個別パス追加失敗のため、MPDライブラリ直接検索フォールバックを実行します...", flush=True)
        # タイトル群で検索
        for track in db_tracks[:5]:
            t_title = track.get("title", "")
            if t_title:
                try:
                    res = client.search("title", t_title)
                    if not res:
                        res = client.search("any", t_title)
                    if res:
                        client.add(res[0]["file"])
                        added_tracks.append(track)
                except Exception:
                    pass

    return added_tracks
