# -*- coding: utf-8 -*-
"""
cleanup_long_tracks.py
SQLiteデータベース (music_meta.db) のメタデータクリーンアップ・修復ツール

機能:
1. 長尺音源の削除 (指定分数以上の音源レコードを除外)
2. 年号の異常検出・修正 (11967, 11960s, 1 960s, 160s, 1 177 などの5桁・3桁・スペース混入年号を4桁西暦に正規化)
3. 英語解説文 (description_en) の日本語混入・不要テキスト修復 (日本語固有名詞をアルファベット/ローマ字表記化し自然な英文に再構築)
"""

import os
import sys
import sqlite3
import re
import argparse
import time
import json
import urllib.request
import traceback
from pathlib import Path
from mutagen import File

# WindowsコンソールのUTF-8出力対応
if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

DB_PATH = "music_meta.db"
DEFAULT_THRESHOLD_MINUTES = 20
LEMONADE_BASE_URL = "http://localhost:13305/v1"

# 日本語・中国語文字（漢字・ひらがな・カタカナ・CJK統合漢字）判定用正規表現
JP_CHAR_REGEX = re.compile(r'[\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFF\u3400-\u4DBF\uF900-\uFAFF]')
# LLM出力ゴミ（JSON、マークダウン、注意書き等）判定用正規表現
JUNK_TEXT_REGEX = re.compile(r'```|\bjson\b|注意|不，|请注意|: " "|: ""')


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


def get_lemonade_model(base_url: str = LEMONADE_BASE_URL) -> str:
    """Lemonade Server からアクティブなモデル名を取得"""
    try:
        req = urllib.request.Request(f"{base_url}/models", headers={"User-Agent": "cleanup-script"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            if "data" in data and len(data["data"]) > 0:
                return data["data"][0]["id"]
    except Exception:
        pass
    return "Qwen2.5-7B-Instruct-NPU"


def normalize_year_text(text: str) -> str:
    """テキスト内の異常な年号（5桁、3桁、スペース混入など）を4桁西暦に正規化"""
    if not text:
        return text

    # 1. 連続する "1 " + "19xx/20xx" -> "19xx/20xx" (e.g. 1 1 1994 -> 1994, 1 1 1 1970s -> 1970s)
    text = re.sub(r'(?<!\d)(?:1\s+)+(19\d{2}|20\d{2})(s|年代|年)?(?!\d)', r'\1\2', text)

    # 2. 1 1[5-9]x (e.g. 1 170年代 -> 1970年代, 1 177 -> 1977, 1 158 -> 1958)
    text = re.sub(r'(?<!\d)1\s+1([5-9]\d)(s|年代|年)?(?!\d)', r'19\1\2', text)
    text = re.sub(r'(?<!\d)1\s+1(\d{2})(?!\d)', r'19\1', text)

    # 3. 119xx / 120xx -> 19xx / 20xx (先頭の1が重複した5桁)
    text = re.sub(r'(?<!\d)1(19\d{2})(s|年代|年)?(?!\d)', r'\1\2', text)
    text = re.sub(r'(?<!\d)1(20\d{2})(s|年代|年)?(?!\d)', r'\1\2', text)
    text = re.sub(r'(?<!\d)1(19\d{2})-(19\d{2}|20\d{2})(?!\d)', r'\1-\2', text)

    # 4. 1 960s -> 1960s, 1 970s -> 1970s, 1 980s -> 1980s
    text = re.sub(r'(?<!\d)1\s+([5-9]\d{2})(s|年代|年)?(?!\d)', r'1\1\2', text)

    # 5. 1 70s -> 1970s, 1 60s -> 1960s, 1 80s -> 1980s, 1 90s -> 1990s
    text = re.sub(r'(?<!\d)1\s+([5-9]\d)s(?!\d)', r'19\1s', text)
    text = re.sub(r'(?<!\d)1\s+([5-9]\d)年代', r'19\1年代', text)
    text = re.sub(r'(?<!\d)1\s+([5-9]\d)年', r'19\1年', text)
    text = re.sub(r'(?<!\d)1\s+(\d{2})年', r'19\1年', text)

    # 6. 160s -> 1960s, 170s -> 1970s, 180s -> 1980s, 190s -> 1990s (3桁の年代)
    text = re.sub(r'(?<!\d)1([5-9]\d)s(?!\w)', r'19\1s', text)
    text = re.sub(r'(?<!\d)1([5-9]\d)年代', r'19\1年代', text)

    # 7. 1 10年代 -> 1950年代 (Moanin' 等)
    text = re.sub(r'(?<!\d)1\s+10年代', r'1950年代', text)

    # 8. 1600s (60年代ロックなどの文脈での誤表記)
    text = re.sub(r'\b1600s\b', '1960s', text)

    return text


def cleanup_long_tracks(db_path: str = DB_PATH, threshold_minutes: int = DEFAULT_THRESHOLD_MINUTES, dry_run: bool = False, auto_confirm: bool = False):
    """長尺音源の検出および削除"""
    if not os.path.exists(db_path):
        print(f"[Error] データベースファイルが見つかりません: {db_path}")
        return False

    threshold_seconds = threshold_minutes * 60
    print(f"\n==================================================")
    print(f"  長尺音源クリーンアップ")
    print(f"  データベース: {db_path}")
    print(f"  判定しきい値: {threshold_minutes} 分 ({threshold_seconds} 秒) 以上")
    print(f"==================================================\n")

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='tracks';")
    if not cur.fetchone():
        print("[Error] テーブル 'tracks' が存在しません。")
        conn.close()
        return False

    cur.execute("PRAGMA table_info(tracks);")
    columns = [col[1] for col in cur.fetchall()]
    has_duration_col = "duration_seconds" in columns

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
        if actual_dur is None and file_path and os.path.exists(file_path):
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
        print(f"[Result] {threshold_minutes}分以上の長尺音源は見つかりませんでした。")
        conn.close()
        return True

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
        return True

    if not auto_confirm:
        print(f"\n上記の {len(long_tracks)} 件のレコードをデータベースから削除しますか？")
        answer = input("削除を実行する場合は 'y' を入力してください (y/N): ").strip().lower()
        if answer != 'y':
            print("[Canceled] 削除をキャンセルしました。")
            conn.close()
            return True

    ids_to_delete = [t["id"] for t in long_tracks]
    cur.executemany("DELETE FROM tracks WHERE id = ?", [(tid,) for tid in ids_to_delete])
    conn.commit()
    print(f"\n[Success] {len(ids_to_delete)} 件のレコードを正常に削除しました。")

    print("[Info] データベースを最適化中 (VACUUM)...")
    cur.execute("VACUUM;")
    conn.close()
    print("[Success] 長尺音源クリーンアップが完了しました。\n")
    return True


def fix_years(db_path: str = DB_PATH, dry_run: bool = False, auto_confirm: bool = False):
    """年号の異常（5桁、3桁、スペース混入等）を検出・修正"""
    if not os.path.exists(db_path):
        print(f"[Error] データベースファイルが見つかりません: {db_path}")
        return False

    print(f"\n==================================================")
    print(f"  年号異常の検出・修正 (5桁/3桁/スペース混入の修復)")
    print(f"  データベース: {db_path}")
    print(f"==================================================\n")

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    cur.execute("SELECT id, title, artist, album, release_year, description_ja, description_en FROM tracks;")
    rows = cur.fetchall()
    print(f"[Info] 全 {len(rows)} 件のレコードを検査中...")

    diffs = []

    for row_id, title, artist, album, year, ja, en in rows:
        new_year = year
        # release_year が1000未満または2030超の場合の補正
        if year is not None:
            if year > 9999 and str(year).startswith("119"):
                new_year = int(str(year)[1:])
            elif year > 9999 and str(year).startswith("120"):
                new_year = int(str(year)[1:])

        new_ja = normalize_year_text(ja) if ja else ja
        new_en = normalize_year_text(en) if en else en

        if new_year != year or new_ja != ja or new_en != en:
            diffs.append({
                "id": row_id,
                "title": title or "（タイトルなし）",
                "artist": artist or "Unknown",
                "old_year": year,
                "new_year": new_year,
                "old_ja": ja,
                "new_ja": new_ja,
                "old_en": en,
                "new_en": new_en
            })

    if not diffs:
        print("[Result] 年号の異常は見つかりませんでした。すべての年号は正常です。")
        conn.close()
        return True

    print(f"\n[Result] 年号の修正対象が {len(diffs)} 件見つかりました:\n")
    for i, d in enumerate(diffs[:20], 1):
        print(f"--- [{i}/{len(diffs)}] ID {d['id']}: {d['artist']} - {d['title']} ---")
        if d["old_year"] != d["new_year"]:
            print(f"  [Year] {d['old_year']}  -->  {d['new_year']}")
        if d["old_ja"] != d["new_ja"]:
            print(f"  [JA Old] {d['old_ja']}")
            print(f"  [JA New] {d['new_ja']}")
        if d["old_en"] != d["new_en"]:
            print(f"  [EN Old] {d['old_en']}")
            print(f"  [EN New] {d['new_en']}")
        print()

    if len(diffs) > 20:
        print(f"... 他 {len(diffs) - 20} 件 (省略)")

    if dry_run:
        print(f"\n[Dry Run] --dry-run が指定されているため、DBの更新は行いませんでした。（対象: {len(diffs)} 件）")
        conn.close()
        return True

    if not auto_confirm:
        print(f"\n上記の {len(diffs)} 件の年号修正をデータベースに反映しますか？")
        answer = input("修正を実行する場合は 'y' を入力してください (y/N): ").strip().lower()
        if answer != 'y':
            print("[Canceled] 年号修正をキャンセルしました。")
            conn.close()
            return True

    updated_count = 0
    for d in diffs:
        cur.execute("""
            UPDATE tracks
            SET release_year = ?, description_ja = ?, description_en = ?
            WHERE id = ?
        """, (d["new_year"], d["new_ja"], d["new_en"], d["id"]))
        updated_count += 1

    conn.commit()
    conn.close()
    print(f"\n[Success] {updated_count} 件のレコードの年号を正常に修正・更新しました。\n")
    return True


def fix_en_descriptions(db_path: str = DB_PATH, limit: int = None, dry_run: bool = False, auto_confirm: bool = False, base_url: str = LEMONADE_BASE_URL):
    """英語解説文 (description_en) 内の日本語混入・不要テキストをLLMで修復"""
    if not os.path.exists(db_path):
        print(f"[Error] データベースファイルが見つかりません: {db_path}")
        return False

    print(f"\n==================================================")
    print(f"  英語解説文 (description_en) の日本語混入・不要テキスト修復")
    print(f"  データベース: {db_path}")
    print(f"  LLM サーバー: {base_url}")
    print(f"==================================================\n")

    active_model = get_lemonade_model(base_url)
    print(f"[System] 使用モデル: {active_model}")

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    cur.execute("SELECT id, title, artist, album, release_year, description_ja, description_en FROM tracks WHERE description_en IS NOT NULL;")
    rows = cur.fetchall()

    targets = []
    for row_id, title, artist, album, year, ja, en in rows:
        if en and (JP_CHAR_REGEX.search(en) or JUNK_TEXT_REGEX.search(en)):
            targets.append({
                "id": row_id,
                "title": title or "",
                "artist": artist or "Unknown",
                "album": album or "",
                "year": year,
                "ja": ja or "",
                "old_en": en
            })

    if not targets:
        print("[Result] 日本語混入やゴミテキストを含む英語解説文は見つかりませんでした。すべてクリーンです。")
        conn.close()
        return True

    print(f"[Result] 修正が必要な英語解説文が {len(targets)} 件見つかりました。")
    if limit:
        targets = targets[:limit]
        print(f"  ※ --limit により先頭 {len(targets)} 件を処理対象とします。")

    if dry_run:
        print(f"\n[Dry Run] --dry-run モードのため、先頭 5 件の推論サンプルを表示します:")
        sample_targets = targets[:5]
    else:
        if not auto_confirm:
            print(f"\n{len(targets)} 件の英語解説文を LLM で修復・更新しますか？")
            answer = input("実行する場合は 'y' を入力してください (y/N): ").strip().lower()
            if answer != 'y':
                print("[Canceled] 英語解説文の修復をキャンセルしました。")
                conn.close()
                return True
        sample_targets = targets

    success_count = 0
    start_time = time.time()

    for idx, t in enumerate(sample_targets, 1):
        print(f"\n[{idx}/{len(sample_targets)}] 修復中: ID {t['id']} [{t['artist']} - {t['title']}]")
        print(f"  [OLD EN] {t['old_en']}")

        prompt = f"""Please rewrite the English description of this song so it is 100% natural English and suitable for text-to-speech / DJ voice reading.
Requirements:
1. NO Japanese or Chinese characters (Kanji, Hiragana, Katakana, Hanzi). Convert Japanese names or terms to Alphabet/Romaji (e.g. 'Takaaki Miyanoue', 'Chiisana Tabi', 'Yuji Ohno').
2. Use standard 4-digit years (e.g. 1968, 1970s). Never use 5-digit years like 11968 or 3-digit years like 160s.
3. Do NOT include markdown blocks, JSON, notes, or conversational text.
4. Output strictly 1-2 sentences of clean English description text.

Track Info:
- Title: {t['title']}
- Artist: {t['artist']}
- Album: {t['album']}
- Release Year: {t['year']}
- Current EN Description: {t['old_en']}
- Japanese Context (for reference): {t['ja']}"""

        payload = {
            "model": active_model,
            "messages": [
                {
                    "role": "system",
                    "content": "You are an expert English music announcer. You output strictly 1-2 sentences of natural English text with Latin/Romaji alphabet only."
                },
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.1,
            "max_tokens": 200
        }

        try:
            req = urllib.request.Request(
                f"{base_url}/chat/completions",
                headers={"Content-Type": "application/json", "User-Agent": "cleanup-script"},
                data=json.dumps(payload).encode('utf-8')
            )
            with urllib.request.urlopen(req, timeout=40) as resp:
                res_data = json.loads(resp.read().decode('utf-8'))
                raw_new_en = res_data["choices"][0]["message"]["content"].strip()

                # 整形・クリーンアップ
                clean_en = raw_new_en.strip('"`\' \n\r\t')
                clean_en = re.sub(r'```[\s\S]*?```', '', clean_en).strip()
                clean_en = re.sub(r'^[\s:`"]+', '', clean_en).strip()
                clean_en = normalize_year_text(clean_en)

                print(f"  [NEW EN] {clean_en}")

                if not dry_run:
                    cur.execute("UPDATE tracks SET description_en = ? WHERE id = ?", (clean_en, t["id"]))
                    # 5件ごとにコミットして安全性を担保
                    if idx % 5 == 0:
                        conn.commit()
                success_count += 1

        except Exception as e:
            print(f"  [Error] ID {t['id']} の推論中にエラーが発生しました: {e}")

    if not dry_run:
        conn.commit()

    conn.close()
    elapsed = time.time() - start_time
    status_label = "プレビュー完了" if dry_run else "正常に更新完了"
    print(f"\n[Success] {success_count}/{len(sample_targets)} 件の英語解説文の修復が{status_label}しました ({elapsed:.1f}秒)。\n")
    return True


def run_all_cleanups(db_path: str = DB_PATH, threshold_minutes: int = DEFAULT_THRESHOLD_MINUTES, dry_run: bool = False, auto_confirm: bool = False):
    """すべてのクリーンアップ処理を順次実行"""
    print(f"\n==================================================")
    print(f"  全メタデータクリーンアップの一括実行")
    print(f"==================================================")

    # 1. 長尺音源クリーンアップ
    cleanup_long_tracks(db_path=db_path, threshold_minutes=threshold_minutes, dry_run=dry_run, auto_confirm=auto_confirm)

    # 2. 年号の異常修正
    fix_years(db_path=db_path, dry_run=dry_run, auto_confirm=auto_confirm)

    # 3. 英語解説文の日本語混入・不要テキスト修復
    fix_en_descriptions(db_path=db_path, dry_run=dry_run, auto_confirm=auto_confirm)

    print(f"\n[Complete] すべてのクリーンアップ処理が完了しました。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="SQLiteデータベース (music_meta.db) のメタデータクリーンアップ・修復ツール"
    )
    parser.add_argument("--db", default=DB_PATH, help=f"SQLiteデータベースのパス (デフォルト: {DB_PATH})")
    parser.add_argument("--minutes", type=int, default=DEFAULT_THRESHOLD_MINUTES, help=f"長尺音源の除外判定分数 (デフォルト: {DEFAULT_THRESHOLD_MINUTES}分)")
    
    # 実行モード選択
    mode_group = parser.add_argument_group("実行モード")
    mode_group.add_argument("--all", action="store_true", help="長尺音源削除・年号修正・英語解説文修復をすべて実行")
    mode_group.add_argument("--cleanup-duration", action="store_true", help="指定分数以上の長尺音源のみを削除")
    mode_group.add_argument("--fix-years", action="store_true", help="年号の異常（5桁/3桁/スペース混入等）のみを修正")
    mode_group.add_argument("--fix-en-descriptions", action="store_true", help="英語解説文の日本語混入・不要テキストのみを修復")
    
    # オプション
    parser.add_argument("--limit", type=int, default=None, help="英語解説文修復の最大処理件数（テスト用）")
    parser.add_argument("--dry-run", action="store_true", help="データベースを変更せず、対象と修正内容のプレビューのみを表示")
    parser.add_argument("-y", "--yes", action="store_true", help="確認プロンプトをスキップして即座に実行")

    args = parser.parse_args()

    try:
        if args.all:
            run_all_cleanups(
                db_path=args.db,
                threshold_minutes=args.minutes,
                dry_run=args.dry_run,
                auto_confirm=args.yes
            )
        elif args.fix_years:
            fix_years(
                db_path=args.db,
                dry_run=args.dry_run,
                auto_confirm=args.yes
            )
        elif args.fix_en_descriptions:
            fix_en_descriptions(
                db_path=args.db,
                limit=args.limit,
                dry_run=args.dry_run,
                auto_confirm=args.yes
            )
        elif args.cleanup_duration:
            cleanup_long_tracks(
                db_path=args.db,
                threshold_minutes=args.minutes,
                dry_run=args.dry_run,
                auto_confirm=args.yes
            )
        else:
            print("\n=== メタデータクリーンアップメニュー ===")
            print("1. 長尺音源クリーンアップ (--cleanup-duration)")
            print("2. 年号の異常修正 (--fix-years)")
            print("3. 英語解説文の修復 (--fix-en-descriptions)")
            print("4. すべて実行 (--all)")
            print("0. 終了")
            
            choice = input("\n実行する番号を入力してください (1-4, 0): ").strip()
            if choice == "1":
                cleanup_long_tracks(db_path=args.db, threshold_minutes=args.minutes, dry_run=args.dry_run, auto_confirm=args.yes)
            elif choice == "2":
                fix_years(db_path=args.db, dry_run=args.dry_run, auto_confirm=args.yes)
            elif choice == "3":
                fix_en_descriptions(db_path=args.db, limit=args.limit, dry_run=args.dry_run, auto_confirm=args.yes)
            elif choice == "4":
                run_all_cleanups(db_path=args.db, threshold_minutes=args.minutes, dry_run=args.dry_run, auto_confirm=args.yes)
            else:
                print("[Info] 処理を終了しました。")

    except KeyboardInterrupt:
        print("\n[System] ユーザーによって処理が中断されました。")
        sys.exit(0)
