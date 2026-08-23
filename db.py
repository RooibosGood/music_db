"""music_meta.db (SQLite) アクセス層

voice_bot.py から切り出した楽曲メタデータ検索・選曲モジュール。
- find_track_metadata: ファイル名 / タイトル / アーティストから楽曲情報・解説文を取得
- search_tracks_from_db: ユーザー要望に合致する楽曲をランダム抽出
- add_db_tracks_to_mpd: 検索結果を MPD キューに追加
"""
import os
import random
import sqlite3
from typing import Any, Dict, List, Optional

DB_PATH = "music_meta.db"

# ランダム選曲の重複防止用（直近に再生した楽曲IDを保持）
recent_played_track_ids: List[int] = []


def find_track_metadata(
    file_path: Optional[str] = None,
    title: Optional[str] = None,
    artist: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """music_meta.db からファイル名やタイトル・アーティスト名で楽曲情報・解説文を取得"""
    if not os.path.exists(DB_PATH):
        return None
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # 1. ファイル名で検索（完全一致、末尾一致、拡張子なし一致）
        if file_path:
            norm_path = file_path.replace("\\", "/")
            fname = norm_path.split("/")[-1]
            fname_stem = os.path.splitext(fname)[0]

            cur.execute(
                """
                SELECT id, title, artist, album, relative_path, file_path, genre, mood, energy_level, is_hires, description_ja, description_en
                FROM tracks
                WHERE file_path LIKE ? OR relative_path LIKE ? OR relative_path = ? OR file_path LIKE ? OR relative_path LIKE ?
                LIMIT 1;
            """,
                (f"%{fname}", f"%{fname}", norm_path, f"%{fname_stem}%", f"%{fname_stem}%"),
            )
            row = cur.fetchone()
            if row and (row["description_ja"] or row["description_en"]):
                conn.close()
                return dict(row)

        # 2. タイトルとアーティストで検索
        if title and artist and artist != "アーティスト未設定" and artist != "Unknown":
            cur.execute(
                """
                SELECT id, title, artist, album, relative_path, file_path, genre, mood, energy_level, is_hires, description_ja, description_en
                FROM tracks
                WHERE (title LIKE ? OR ? LIKE '%' || title || '%') AND (artist LIKE ? OR ? LIKE '%' || artist || '%')
                LIMIT 1;
            """,
                (f"%{title}%", title, f"%{artist}%", artist),
            )
            row = cur.fetchone()
            if row and (row["description_ja"] or row["description_en"]):
                conn.close()
                return dict(row)

        # 3. タイトルのみで検索
        if title and title != "未選択" and title != "Unknown":
            cur.execute(
                """
                SELECT id, title, artist, album, relative_path, file_path, genre, mood, energy_level, is_hires, description_ja, description_en
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


def search_tracks_from_db(query: str, limit: int = 15) -> List[Dict[str, Any]]:
    """music_meta.db からユーザー要望（ジャンル、ムード、エネルギー、ハイレゾ、アーティスト、曲名等）に合致する楽曲を完全ランダム抽出"""
    global recent_played_track_ids
    if not os.path.exists(DB_PATH):
        print(f"⚠️ [DB] {DB_PATH} が存在しません。")
        return []
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        clean_q = query.strip()
        conditions = []
        params = []

        # 直近に再生した楽曲IDは除外候補（同じ曲の連続再生を防止）
        recent_ids = recent_played_track_ids[-30:] if recent_played_track_ids else []

        # 1. ジャンル判定マッピング
        genre_keywords = {
            "ジャズ": "ジャズ", "jazz": "ジャズ",
            "ロック": "ロック", "rock": "ロック",
            "ポップ": "ポップ", "ポップス": "ポップ", "pop": "ポップ",
            "クラシック": "クラシック", "classic": "クラシック", "classical": "クラシック",
            "ブルース": "ブルース", "blues": "ブルース",
            "ソウル": "R&B・ソウル", "r&b": "R&B・ソウル", "rnb": "R&B・ソウル",
            "エレクトロニック": "エレクトロニック", "テクノ": "エレクトロニック", "edm": "エレクトロニック",
            "フォーク": "フォーク・カントリー", "カントリー": "フォーク・カントリー",
            "ヒップホップ": "ヒップホップ", "hiphop": "ヒップホップ", "ラップ": "ヒップホップ",
            "サントラ": "サウンドトラック・インスト", "サウンドトラック": "サウンドトラック・インスト", "インスト": "サウンドトラック・インスト",
        }

        matched_genres = [db_genre for kw, db_genre in genre_keywords.items() if kw in clean_q.lower()]
        if matched_genres:
            genre_clause = " OR ".join(["genre LIKE ?" for _ in matched_genres])
            conditions.append(f"({genre_clause})")
            params.extend([f"%{g}%" for g in matched_genres])

        # 2. ハイレゾ判定
        if any(k in clean_q.lower() for k in ["ハイレゾ", "hires", "hi-res", "高音質"]):
            conditions.append("is_hires = 1")

        # 3. エネルギー / 気分判定
        if any(k in clean_q for k in ["静か", "落ち着", "リラックス", "穏やか", "眠", "バラード", "癒"]):
            conditions.append("(energy_level <= 2 OR mood LIKE '%Calm%' OR mood LIKE '%Relax%')")
        elif any(k in clean_q for k in ["元気", "激し", "アップテンポ", "ノリ", "ドライブ", "テンション"]):
            conditions.append("(energy_level >= 4 OR mood LIKE '%Energetic%' OR mood LIKE '%Upbeat%')")

        # 4. 邦楽 / 洋楽判定
        if any(k in clean_q for k in ["邦楽", "j-pop", "jpop", "日本の曲", "日本語"]):
            conditions.append("music_category = '邦楽'")
        elif any(k in clean_q for k in ["洋楽", "海外"]):
            conditions.append("music_category = '洋楽'")

        # 5. 一般キーワード（アーティスト名、曲名、アルバム名、解説文）
        stop_words = ["をかけて", "を流して", "を再生して", "かけて", "流して", "再生して", "聴きたい", "聴かせて", "曲", "音楽"]
        keyword_q = clean_q
        for sw in stop_words:
            keyword_q = keyword_q.replace(sw, "").strip()

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
            SELECT id, title, artist, album, relative_path, file_path, genre, mood, energy_level, is_hires, description_ja, description_en
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
                SELECT id, title, artist, album, relative_path, file_path, genre, mood, energy_level, is_hires, description_ja, description_en
                FROM tracks
                WHERE title LIKE ? OR artist LIKE ? OR album LIKE ? OR description_ja LIKE ? OR description_en LIKE ?
                ORDER BY (CASE WHEN (description_ja IS NOT NULL AND description_ja != '') OR (description_en IS NOT NULL AND description_en != '') THEN 0 ELSE 1 END), RANDOM()
                LIMIT 50;
            """, (f"%{keyword_q}%", f"%{keyword_q}%", f"%{keyword_q}%", f"%{keyword_q}%", f"%{keyword_q}%"))
            candidate_rows = [dict(r) for r in cur.fetchall()]

        # それでもヒットしない場合、ランダムに曲を取得
        if not candidate_rows:
            cur.execute("""
                SELECT id, title, artist, album, relative_path, file_path, genre, mood, energy_level, is_hires, description_ja, description_en
                FROM tracks
                WHERE (description_ja IS NOT NULL AND description_ja != '') OR (description_en IS NOT NULL AND description_en != '')
                ORDER BY RANDOM()
                LIMIT 50;
            """)
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
    """DB検索結果の楽曲を MPD のキューに追加し、追加に成功した楽曲リストを返す"""
    added_tracks = []
    for track in db_tracks:
        rel_path = track.get("relative_path") or ""
        fname = rel_path.replace("\\", "/").split("/")[-1] if rel_path else ""
        title = track.get("title") or fname
        artist = track.get("artist") or ""

        added = False
        # 1. ファイル名で MPD 検索
        if fname:
            try:
                search_res = client.search("file", fname)
                if not search_res:
                    search_res = client.search("any", fname)
                if search_res:
                    client.add(search_res[0]["file"])
                    added_tracks.append(track)
                    added = True
            except Exception:
                pass

        # 2. タイトル＆アーティストで MPD 検索
        if not added and title:
            try:
                if artist and artist != "アーティスト未設定" and artist != "Unknown":
                    search_res = client.search("title", title, "artist", artist)
                else:
                    search_res = client.search("title", title)
                if search_res:
                    client.add(search_res[0]["file"])
                    added_tracks.append(track)
                    added = True
            except Exception:
                pass

        # 3. 直接パスでの追加試行 (moOde の相対パス)
        if not added and rel_path:
            norm_rel = rel_path.replace("\\", "/")
            try:
                client.add(norm_rel)
                added_tracks.append(track)
                added = True
            except Exception:
                pass

    return added_tracks
