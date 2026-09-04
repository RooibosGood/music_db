# -*- coding: utf-8 -*-
"""
set_jazz_genre.py
NASの music/JAZZ フォルダ配下の全楽曲ファイルのジャンルタグに "JAZZ" を設定・書き込むスクリプト

機能:
1. NAS (デフォルト: \\\\homenas\\music\\JAZZ) 配下の音楽ファイルを再帰的にスキャン
2. 各種フォーマット（FLAC, DSF, DFF, MP3, M4A, AAC, OGG, OPUS, WAV, AIFF, WMA等）に対応
3. ジャンルタグを "JAZZ" に設定・更新 (mutagen 使用)
4. モード選択: overwrite (上書き), append (追記), if_empty (未設定時のみ)
5. 安全機能: --dry-run オプションによる書き込み前シミュレーション
6. データベース同期: --update-db オプションによる music_meta.db の genre 同期更新
"""

import os
import sys
import argparse
import stat
import sqlite3
import time
from pathlib import Path
from typing import List, Optional, Tuple, Dict, Any

# Windows コンソールの UTF-8 出力対応
if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

try:
    import mutagen
    from mutagen import File as MutagenFile
    from mutagen.flac import FLAC
    from mutagen.mp3 import MP3
    from mutagen.mp4 import MP4
    from mutagen.oggvorbis import OggVorbis
    from mutagen.oggopus import OggOpus
    from mutagen.asf import ASF
    from mutagen.dsf import DSF
    from mutagen.id3 import ID3, TCON, ID3NoHeaderError
except ImportError:
    print("[Error] mutagen がインストールされていません。'pip install mutagen' を実行してください。")
    sys.exit(1)

# デフォルト設定
DEFAULT_JAZZ_DIR = r"\\homenas\music\JAZZ"
# Linux / Jetson 環境でのフォールバック候補
FALLBACK_LINUX_DIRS = [
    "/mnt/music/JAZZ",
    "/mnt/music/music/JAZZ",
    "/mnt/nas/music/JAZZ",
]
DEFAULT_GENRE = "JAZZ"
DEFAULT_DB_PATH = "music_meta.db"

# 対象とする音楽ファイル拡張子
SUPPORTED_EXTENSIONS = (
    '.flac', '.dsf', '.dff', '.mp3', '.m4a', '.aac',
    '.ogg', '.opus', '.wav', '.alac', '.aiff', '.aif',
    '.wma', '.asf', '.ape', '.wv'
)


def resolve_default_target_dir() -> str:
    """環境に応じて最適なデフォルトのJAZZフォルダパスを判定"""
    if os.path.exists(DEFAULT_JAZZ_DIR):
        return DEFAULT_JAZZ_DIR
    for linux_path in FALLBACK_LINUX_DIRS:
        if os.path.exists(linux_path):
            return linux_path
    return DEFAULT_JAZZ_DIR


def get_current_genre(file_path: str) -> List[str]:
    """音源ファイルから現在のジャンルタグを取得"""
    genres = []
    
    # 1. EasyID3 / EasyTag 経由での取得
    try:
        easy_audio = MutagenFile(file_path, easy=True)
        if easy_audio and "genre" in easy_audio:
            for g in easy_audio["genre"]:
                g_str = str(g).strip()
                if g_str:
                    genres.append(g_str)
            if genres:
                return genres
    except Exception:
        pass

    # 2. Raw タグ経由でのフォールバック取得
    try:
        raw_audio = MutagenFile(file_path)
        if raw_audio and raw_audio.tags:
            tags = raw_audio.tags
            # ID3 (MP3, DSF, WAV等)
            if hasattr(tags, "getall"):
                tcon_frames = tags.getall("TCON")
                for frame in tcon_frames:
                    if hasattr(frame, "text"):
                        for t in frame.text:
                            t_str = str(t).strip()
                            if t_str:
                                genres.append(t_str)
                    elif str(frame).strip():
                        genres.append(str(frame).strip())
            # Vorbis / MP4 / ASF
            elif isinstance(tags, dict) or hasattr(tags, "get"):
                for key in ["GENRE", "genre", "\xa9gen", "WM/Genre", "gnre"]:
                    val = tags.get(key)
                    if val:
                        if isinstance(val, list):
                            for item in val:
                                if hasattr(item, "value"):
                                    genres.append(str(item.value).strip())
                                elif str(item).strip():
                                    genres.append(str(item).strip())
                        else:
                            if hasattr(val, "value"):
                                genres.append(str(val.value).strip())
                            elif str(val).strip():
                                genres.append(str(val).strip())
                        break
    except Exception:
        pass

    return genres


def ensure_writable(file_path: str):
    """Windowsの読み取り専用属性が付与されている場合は解除"""
    try:
        mode = os.stat(file_path).st_mode
        if not (mode & stat.S_IWRITE):
            os.chmod(file_path, mode | stat.S_IWRITE)
    except Exception:
        pass


def write_genre_tag(file_path: str, new_genres: List[str]) -> bool:
    """音源ファイルにジャンルタグを直接書き込む"""
    ensure_writable(file_path)
    ext = os.path.splitext(file_path)[1].lower()

    # 1. EasyTag を試行
    try:
        easy_audio = MutagenFile(file_path, easy=True)
        if easy_audio is not None:
            easy_audio["genre"] = new_genres
            easy_audio.save()
            return True
    except Exception:
        pass

    # 2. フォーマット固有の書き込みフォールバック
    try:
        if ext == '.flac':
            audio = FLAC(file_path)
            audio['genre'] = new_genres
            audio.save()
            return True

        elif ext == '.dsf':
            audio = DSF(file_path)
            if audio.tags is None:
                audio.add_tags()
            audio.tags.delall('TCON')
            audio.tags.add(TCON(encoding=3, text=new_genres))
            audio.save()
            return True

        elif ext in ('.mp3', '.wav', '.aiff', '.aif'):
            try:
                id3 = ID3(file_path)
            except ID3NoHeaderError:
                id3 = ID3()
            id3.delall('TCON')
            id3.add(TCON(encoding=3, text=new_genres))
            id3.save(file_path)
            return True

        elif ext in ('.m4a', '.mp4', '.aac', '.alac'):
            audio = MP4(file_path)
            audio['\xa9gen'] = new_genres
            audio.save()
            return True

        elif ext in ('.ogg', '.opus'):
            try:
                audio = OggVorbis(file_path)
            except Exception:
                audio = OggOpus(file_path)
            audio['genre'] = new_genres
            audio.save()
            return True

        elif ext in ('.wma', '.asf'):
            audio = ASF(file_path)
            audio['WM/Genre'] = new_genres[0] if new_genres else ""
            audio.save()
            return True

        else:
            # 汎用 File
            raw_audio = MutagenFile(file_path)
            if raw_audio is not None and hasattr(raw_audio, 'tags') and raw_audio.tags is not None:
                raw_audio.tags['genre'] = new_genres
                raw_audio.save()
                return True

    except Exception as e:
        print(f"    [Write Error] {os.path.basename(file_path)}: {e}")
        return False

    return False


def update_db_genre(conn: sqlite3.Connection, file_path: str, target_genre: str, mode: str = "append") -> int:
    """music_meta.db の tracks テーブルの genre を更新（モードに応じた追加・上書き）"""
    cur = conn.cursor()
    normalized_path = file_path.replace('/', '\\')
    file_name = os.path.basename(file_path)

    # 対象レコードの既存 genre を取得
    cur.execute("SELECT id, genre FROM tracks WHERE file_path = ? OR REPLACE(file_path, '/', '\\') = ?", (file_path, normalized_path))
    rows = cur.fetchall()
    if not rows:
        cur.execute("SELECT id, genre FROM tracks WHERE file_path LIKE ? AND file_path LIKE '%JAZZ%'", (f"%{file_name}",))
        rows = cur.fetchall()

    if not rows:
        return 0

    updated_count = 0
    for track_id, current_genre in rows:
        current_genre_str = current_genre.strip() if current_genre else ""

        if mode == "overwrite":
            new_val = target_genre
        elif mode == "if_empty":
            if not current_genre_str:
                new_val = target_genre
            else:
                continue
        else:  # append (デフォルト)
            parts = [p.strip() for p in current_genre_str.split(',') if p.strip()]
            if any(p.upper() == target_genre.upper() for p in parts):
                continue
            parts.append(target_genre)
            new_val = ", ".join(parts)

        if new_val != current_genre_str:
            cur.execute("UPDATE tracks SET genre = ? WHERE id = ?", (new_val, track_id))
            updated_count += cur.rowcount

    return updated_count


def scan_and_collect_files(target_dir: str) -> List[str]:
    """対象ディレクトリ配下の音楽ファイルを再帰的に探索"""
    music_files = []
    print(f"[Search] スキャン中: {target_dir} ...")
    
    for root, _, files in os.walk(target_dir):
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            if ext in SUPPORTED_EXTENSIONS:
                music_files.append(os.path.join(root, f))
                
    return music_files


def main():
    parser = argparse.ArgumentParser(
        description="NAS music/JAZZ フォルダ配下の楽曲ファイルのジャンルタグに 'JAZZ' を追加・設定するスクリプト",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用例:
  # 1. プレビュー確認 (変更を保存しない)
  python set_jazz_genre.py --dry-run --limit 10

  # 2. 現在のGenreを維持しつつ "JAZZ" を追加 (デフォルト動作)
  python set_jazz_genre.py

  # 3. 既存のGenreを "JAZZ" 単独で上書きしたい場合
  python set_jazz_genre.py --mode overwrite

  # 4. ジャンル未設定の曲のみ "JAZZ" を設定したい場合
  python set_jazz_genre.py --mode if_empty

  # 5. ファイルタグ更新と同時に music_meta.db も更新 (DB既存ジャンルにも追記)
  python set_jazz_genre.py --update-db

  # 6. 対象フォルダを直接指定
  python set_jazz_genre.py --target-dir "D:\\music\\JAZZ"
        """
    )

    default_dir = resolve_default_target_dir()
    parser.add_argument(
        "--target-dir",
        default=default_dir,
        help=f"対象のJAZZフォルダパス (デフォルト: {default_dir})"
    )
    parser.add_argument(
        "--genre",
        default=DEFAULT_GENRE,
        help=f"設定・追加するジャンル名 (デフォルト: '{DEFAULT_GENRE}')"
    )
    parser.add_argument(
        "--mode",
        choices=["append", "overwrite", "if_empty"],
        default="append",
        help="ジャンル設定モード: append (現在のGenreに追加・デフォルト), overwrite (上書き), if_empty (未設定時のみ) (デフォルト: append)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="実際には書き込まずに対象ファイルと更新前後のタグをプレビュー表示"
    )
    parser.add_argument(
        "--update-db",
        action="store_true",
        help="music_meta.db の tracks テーブルの genre も同期更新"
    )
    parser.add_argument(
        "--db-path",
        default=DEFAULT_DB_PATH,
        help=f"SQLite データベースパス (デフォルト: {DEFAULT_DB_PATH})"
    )
    parser.add_argument(
        "--db-genre",
        default=None,
        help="DB更新時のジャンル名（未指定時は --genre と同一、例: 'ジャズ' を指定可能）"
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="処理する最大ファイル数（テスト用）"
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="全ファイルの変更前後の詳細をログ出力"
    )

    args = parser.parse_args()

    print("=" * 70)
    print(" NAS music/JAZZ 楽曲ジャンル設定スクリプト (set_jazz_genre.py)")
    print("=" * 70)
    print(f" 対象フォルダ   : {args.target_dir}")
    print(f" 設定ジャンル   : {args.genre}")
    print(f" 動作モード     : {args.mode}")
    print(f" Dry Run        : {'有効 (書き込みなし)' if args.dry_run else '無効 (実際に書き込み実行)'}")
    print(f" DB 同期        : {'有効 (' + args.db_path + ')' if args.update_db else '無効'}")
    if args.limit:
        print(f" 件数制限       : 最大 {args.limit} 件")
    print("=" * 70)

    if not os.path.exists(args.target_dir):
        print(f"[Error] 指定されたフォルダが存在しません: {args.target_dir}")
        sys.exit(1)

    # ファイル探索
    start_time = time.time()
    music_files = scan_and_collect_files(args.target_dir)
    total_found = len(music_files)
    print(f"[Info] 該当音楽ファイルを {total_found} 件検出しました。\n")

    if total_found == 0:
        print("[Info] 処理対象の音楽ファイルがありませんでした。終了します。")
        sys.exit(0)

    if args.limit and args.limit > 0:
        music_files = music_files[:args.limit]
        print(f"[Info] --limit 指定により先頭 {len(music_files)} 件を処理します。\n")

    # DB接続準備
    db_conn = None
    if args.update_db and not args.dry_run:
        if os.path.exists(args.db_path):
            try:
                db_conn = sqlite3.connect(args.db_path)
                print(f"[Database] {args.db_path} に接続しました。")
            except Exception as e:
                print(f"[Warning] データベース接続に失敗しました: {e}（DB更新はスキップします）")
        else:
            print(f"[Warning] データベースファイルが見つかりません: {args.db_path}（DB更新はスキップします）")

    target_db_genre = args.db_genre if args.db_genre else args.genre

    # 処理ループ
    count_updated = 0
    count_skipped = 0
    count_failed = 0
    count_db_updated = 0

    for i, file_path in enumerate(music_files, 1):
        rel_path = os.path.relpath(file_path, args.target_dir)
        current_genres = get_current_genre(file_path)
        current_genre_str = " / ".join(current_genres) if current_genres else "(未設定)"

        # モードに応じた新しいジャンルリストの決定
        need_update = False
        new_genres = []

        if args.mode == "overwrite":
            # 既に "JAZZ" のみ設定されている場合はスキップ
            if len(current_genres) == 1 and current_genres[0].strip().upper() == args.genre.strip().upper():
                need_update = False
            else:
                need_update = True
                new_genres = [args.genre]

        elif args.mode == "append":
            # 既に genre が含まれているかチェック (大文字小文字無視)
            has_genre = any(g.strip().upper() == args.genre.strip().upper() for g in current_genres)
            if has_genre:
                need_update = False
            else:
                need_update = True
                new_genres = list(current_genres) + [args.genre]

        elif args.mode == "if_empty":
            if not current_genres:
                need_update = True
                new_genres = [args.genre]
            else:
                need_update = False

        new_genre_str = " / ".join(new_genres) if new_genres else current_genre_str

        # ログ表示と実行
        if need_update:
            if args.verbose or args.dry_run or (i <= 5 or i % 100 == 0):
                prefix = "[DRY-RUN]" if args.dry_run else "[UPDATE]"
                print(f" {prefix} [{i}/{len(music_files)}] {rel_path}")
                print(f"    ジャンル: '{current_genre_str}' -> '{new_genre_str}'")

            if args.dry_run:
                count_updated += 1
            else:
                # 実際のファイル書き込み
                success = write_genre_tag(file_path, new_genres)
                if success:
                    count_updated += 1
                    # DB更新
                    if db_conn:
                        try:
                            rows = update_db_genre(db_conn, file_path, target_db_genre, mode=args.mode)
                            count_db_updated += rows
                        except Exception as e:
                            print(f"    [DB Error] {e}")
                else:
                    count_failed += 1
        else:
            count_skipped += 1
            if args.verbose:
                print(f" [SKIP] [{i}/{len(music_files)}] {rel_path} (既に '{current_genre_str}')")

        # 進捗サマリー（100件ごと）
        if i % 200 == 0 and not args.verbose:
            print(f"  ... 進捗: {i}/{len(music_files)} 件処理完了 (更新: {count_updated}, スキップ: {count_skipped})")

    # DBコミット
    if db_conn:
        try:
            db_conn.commit()
            db_conn.close()
            print(f"\n[Database] データベースの更新をコミットしました（{count_db_updated} レコード更新）。")
        except Exception as e:
            print(f"\n[Database Error] コミット失敗: {e}")

    elapsed = time.time() - start_time
    print("\n" + "=" * 70)
    print(" 処理結果サマリー")
    print("=" * 70)
    print(f" 総スキャン楽曲数 : {len(music_files)} 件")
    print(f" 更新対象 / 成功  : {count_updated} 件 {'(※Dry-runのため変更なし)' if args.dry_run else ''}")
    print(f" 変更不要スキップ : {count_skipped} 件")
    if count_failed > 0:
        print(f" 書き込み失敗     : {count_failed} 件")
    if args.update_db and not args.dry_run:
        print(f" DB更新レコード数 : {count_db_updated} 件")
    print(f" 処理所要時間     : {elapsed:.2f} 秒")
    print("=" * 70)


if __name__ == "__main__":
    main()
