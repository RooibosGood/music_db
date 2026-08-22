import os
import sys
import sqlite3
import argparse
from pathlib import Path
from mutagen import File

DB_PATH = "music_meta.db"
DEFAULT_THRESHOLD_MINUTES = 20

def format_duration(seconds: int) -> str:
    """秒数を 'MM:SS' または 'HH:MM:SS' 形式にフォーマット"""
    if seconds is None:
        return "不明"
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    if hours > 0:
        return f"{hours}時間{minutes:02d}分{secs:02d}秒 ({seconds // 60:.1f}分)"
    return f"{minutes}分{secs:02d}秒 ({seconds / 60:.1f}分)"

def cleanup_long_tracks(db_path: str = DB_PATH, threshold_minutes: int = DEFAULT_THRESHOLD_MINUTES, dry_run: bool = False, auto_confirm: bool = False):
    if not os.path.exists(db_path):
        print(f"[Error] データベースファイルが見つかりません: {db_path}")
        sys.exit(1)

    threshold_seconds = threshold_minutes * 60
    print(f"=== 長尺音源クリーンアップ ===")
    print(f"  データベース: {db_path}")
    print(f"  判定しきい値: {threshold_minutes} 分 ({threshold_seconds} 秒) 以上\n")

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    # tracksテーブルの存在確認
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='tracks';")
    if not cur.fetchone():
        print("[Error] テーブル 'tracks' が存在しません。")
        conn.close()
        sys.exit(1)

    # duration_seconds カラムの存在確認
    cur.execute("PRAGMA table_info(tracks);")
    columns = [col[1] for col in cur.fetchall()]
    has_duration_col = "duration_seconds" in columns

    # 全レコードを取得してチェック
    if has_duration_col:
        cur.execute("SELECT id, title, artist, album, file_path, duration_seconds FROM tracks;")
    else:
        cur.execute("SELECT id, title, artist, album, file_path, NULL FROM tracks;")
    
    rows = cur.fetchall()
    print(f"[Info] データベース内の全 {len(rows)} 件を検査中...")

    long_tracks = []
    updated_durations = 0

    for row_id, title, artist, album, file_path, dur_sec in rows:
        actual_dur = dur_sec

        # duration_seconds が未設定の場合、ファイルから直接長さを取得
        if actual_dur is None:
            if file_path and os.path.exists(file_path):
                try:
                    audio = File(file_path, easy=True)
                    if audio and audio.info:
                        actual_dur = int(getattr(audio.info, "length", 0))
                        if has_duration_col:
                            cur.execute("UPDATE tracks SET duration_seconds = ? WHERE id = ?", (actual_dur, row_id))
                            updated_durations += 1
                except Exception:
                    pass

        if actual_dur and actual_dur >= threshold_seconds:
            long_tracks.append({
                "id": row_id,
                "title": title or "（タイトルなし）",
                "artist": artist or "Unknown",
                "album": album or "",
                "file_path": file_path,
                "duration_seconds": actual_dur
            })

    if updated_durations > 0 and not dry_run:
        conn.commit()
        print(f"[Info] {updated_durations} 件の未設定レコードに再生時間情報を補完しました。")

    if not long_tracks:
        print(f"\n[Result] {threshold_minutes}分以上の長尺音源は見つかりませんでした。データベースはクリーンです。")
        conn.close()
        return

    print(f"\n[Result] {threshold_minutes}分以上の長尺音源が {len(long_tracks)} 件見つかりました:\n")
    print(f"{'ID':<6} | {'再生時間':<20} | {'アーティスト / タイトル':<40} | {'ファイル名'}")
    print("-" * 100)
    for track in long_tracks:
        dur_str = format_duration(track["duration_seconds"])
        info_str = f"{track['artist']} - {track['title']}"
        file_name = Path(track["file_path"]).name if track["file_path"] else "不明"
        print(f"{track['id']:<6} | {dur_str:<20} | {info_str[:38]:<40} | {file_name}")

    if dry_run:
        print(f"\n[Dry Run] --dry-run が指定されているため、削除は行いませんでした。（対象: {len(long_tracks)} 件）")
        conn.close()
        return

    # 削除確認
    if not auto_confirm:
        print(f"\n上記の {len(long_tracks)} 件のレコードをデータベースから削除しますか？")
        answer = input("削除を実行する場合は 'y' を入力してください (y/N): ").strip().lower()
        if answer != 'y':
            print("[Canceled] 削除をキャンセルしました。")
            conn.close()
            return

    # 削除実行
    ids_to_delete = [t["id"] for t in long_tracks]
    cur.executemany("DELETE FROM tracks WHERE id = ?", [(tid,) for tid in ids_to_delete])
    conn.commit()
    print(f"\n[Success] {len(ids_to_delete)} 件のレコードを正常に削除しました。")

    # VACUUM でDBファイルを最適化
    print("[Info] データベースを最適化中 (VACUUM)...")
    cur.execute("VACUUM;")
    conn.close()
    print("[Success] クリーンアップが完了しました。")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SQLiteデータベースから指定分数以上の長尺音源（DVD音声など）を削除するスクリプト")
    parser.add_argument("--db", default=DB_PATH, help=f"SQLiteデータベースのパス (デフォルト: {DB_PATH})")
    parser.add_argument("--minutes", type=int, default=DEFAULT_THRESHOLD_MINUTES, help=f"除外判定する分数 (デフォルト: {DEFAULT_THRESHOLD_MINUTES}分)")
    parser.add_argument("--dry-run", action="store_true", help="削除を実行せず、対象レコードの一覧のみを表示する")
    parser.add_argument("-y", "--yes", action="store_true", help="確認プロンプトをスキップして即座に削除を実行する")
    args = parser.parse_args()

    try:
        cleanup_long_tracks(
            db_path=args.db,
            threshold_minutes=args.minutes,
            dry_run=args.dry_run,
            auto_confirm=args.yes
        )
    except KeyboardInterrupt:
        print("\n[System] ユーザーによって処理が中断されました。")
        sys.exit(0)
