# -*- coding: utf-8 -*-
"""
fill_missing_genres.py
NAS/ローカル音楽ライブラリを検索し、ジャンルタグが設定されていない楽曲に対して
ローカルLLM (Lemonade Server) でジャンルを自動推論・設定するスクリプト

機能:
1. NAS (デフォルト: \\\\homenas\\music) 配下の音楽ファイルを再帰スキャン
2. ジャンルタグが未設定（未登録・空文字・Unknown）の楽曲ファイルを検出
3. タグ情報およびディレクトリ階層・ファイル名からアーティスト/アルバム/曲名を自動補完
4. ローカルLLM (Lemonade Server: http://localhost:13305/v1) に問い合わせてジャンルを自動推論
5. 音楽ファイル本体（WMA, FLAC, MP3, M4A, DSF等）のタグに推論したジャンルを直接書き込み
6. オプション (--update-db) により SQLite データベース (music_meta.db) の genre も同期更新
7. 安全機能: --dry-run オプションによる書き込み前シミュレーション
"""

import os
import sys
import argparse
import json
import re
import stat
import sqlite3
import time
import urllib.request
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
    from mutagen.asf import ASF, ASFUnicodeAttribute
    from mutagen.dsf import DSF
    from mutagen.id3 import ID3, TCON, ID3NoHeaderError
except ImportError:
    print("[Error] mutagen がインストールされていません。'pip install mutagen' を実行してください。")
    sys.exit(1)

# デフォルト設定
DEFAULT_MUSIC_DIR = r"\\homenas\music"
FALLBACK_LINUX_DIRS = [
    "/mnt/music",
    "/mnt/music/music",
    "/mnt/nas/music",
]
DEFAULT_BASE_URL = "http://localhost:13305/v1"
DEFAULT_DB_PATH = "music_meta.db"

# 対象とする音楽ファイル拡張子
SUPPORTED_EXTENSIONS = (
    '.flac', '.wma', '.mp3', '.m4a', '.aac',
    '.ogg', '.opus', '.wav', '.alac', '.aiff', '.aif',
    '.dsf', '.dff', '.asf', '.ape', '.wv'
)

# 一般的なライブラリジャンル候補例（プロンプト用）
GENRE_GUIDE = [
    "J ポップ", "ポップ", "ロック", "JAZZ", "クラシック", "サウンドトラック (映画)",
    "アニメ映画", "TV音楽", "R&B・ソウル", "ヒップホップ", "フォーク・カントリー",
    "ブルース", "エレクトロニック", "童謡 (日本)", "ニュー エイジ", "その他"
]


def resolve_default_music_dir() -> str:
    """環境に応じたデフォルトの音楽フォルダパスを判定"""
    if os.path.exists(DEFAULT_MUSIC_DIR):
        return DEFAULT_MUSIC_DIR
    for p in FALLBACK_LINUX_DIRS:
        if os.path.exists(p):
            return p
    return DEFAULT_MUSIC_DIR


def get_lemonade_active_model(base_url: str = DEFAULT_BASE_URL) -> str:
    """Lemonade Server から利用可能なアクティブモデルを自動取得"""
    try:
        req = urllib.request.Request(f"{base_url}/models")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            models = data.get("data", [])
            if models:
                # 優先したいモデルがあれば優先
                for m in models:
                    m_id = m.get("id", "")
                    if "Qwen" in m_id or "Llama" in m_id:
                        return m_id
                return models[0].get("id", "default")
    except Exception as e:
        print(f"[Warning] Lemonade Server ({base_url}) からのモデル取得に失敗しました: {e}")
    return "default"


def get_current_genres(file_path: str) -> List[str]:
    """音源ファイルから現在のジャンルタグを取得"""
    genres = []
    
    # 1. EasyID3 / EasyTag 経由での取得
    try:
        easy_audio = MutagenFile(file_path, easy=True)
        if easy_audio and "genre" in easy_audio:
            for g in easy_audio["genre"]:
                g_str = str(g).strip()
                if g_str and g_str.lower() not in ("(ジャンル未設定)", "unknown", "none", ""):
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
                            if t_str and t_str.lower() not in ("(ジャンル未設定)", "unknown", "none", ""):
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
                                s = str(getattr(item, "value", item)).strip()
                                if s and s.lower() not in ("(ジャンル未設定)", "unknown", "none", ""):
                                    genres.append(s)
                        else:
                            s = str(getattr(val, "value", val)).strip()
                            if s and s.lower() not in ("(ジャンル未設定)", "unknown", "none", ""):
                                genres.append(s)
                        if genres:
                            break
    except Exception:
        pass

    return genres


def extract_track_metadata(file_path: str, base_dir: str) -> Dict[str, str]:
    """音源ファイルのタグおよびファイルパスからアーティスト、アルバム、タイトルを抽出・補完"""
    title = ""
    artist = ""
    album = ""

    # 1. タグからの抽出
    try:
        easy_audio = MutagenFile(file_path, easy=True)
        if easy_audio:
            if "title" in easy_audio and easy_audio["title"]:
                title = str(easy_audio["title"][0]).strip()
            if "artist" in easy_audio and easy_audio["artist"]:
                artist = str(easy_audio["artist"][0]).strip()
            if "album" in easy_audio and easy_audio["album"]:
                album = str(easy_audio["album"][0]).strip()
    except Exception:
        pass

    # 2. Raw タグからのフォールバック (WMA / ASF / MP4等)
    if not (title and artist and album):
        try:
            raw_audio = MutagenFile(file_path)
            if raw_audio and raw_audio.tags:
                tags = raw_audio.tags
                if not title:
                    for k in ["Title", "TIT2", "\xa9nam"]:
                        v = tags.get(k)
                        if v:
                            val_str = str(v[0].value if hasattr(v[0], 'value') else v[0]).strip()
                            if val_str:
                                title = val_str
                                break
                if not artist:
                    for k in ["Author", "WM/AlbumArtist", "TPE1", "TPE2", "\xa9ART", "aART"]:
                        v = tags.get(k)
                        if v:
                            val_str = str(v[0].value if hasattr(v[0], 'value') else v[0]).strip()
                            if val_str:
                                artist = val_str
                                break
                if not album:
                    for k in ["WM/AlbumTitle", "TALB", "\xa9alb"]:
                        v = tags.get(k)
                        if v:
                            val_str = str(v[0].value if hasattr(v[0], 'value') else v[0]).strip()
                            if val_str:
                                album = val_str
                                break
        except Exception:
            pass

    # 3. パス・ファイル名からの補完
    filename = os.path.splitext(os.path.basename(file_path))[0]
    # トラック番号（例: "01-", "01 ", "01." 等）を除去してタイトル補完
    clean_filename = re.sub(r'^\d+[\s\-_.]+', '', filename).strip()

    if not title or title.lower() in ("unknown", "track", ""):
        title = clean_filename if clean_filename else filename

    # 絶対パスのディレクトリ構造から末尾2階層をアルバム・アーティストとして推定
    all_parts = [p for p in Path(file_path).parts if p and p not in ('\\\\', '/', ':')]
    if len(all_parts) >= 3:
        p_album = all_parts[-2]
        p_artist = all_parts[-3]
        if not artist or artist.lower() in ("unknown", ""):
            if p_artist.lower() not in ("music", "homenas", "nas"):
                artist = p_artist
            else:
                artist = p_album
        if not album or album.lower() in ("unknown", ""):
            album = p_album
    elif len(all_parts) >= 2:
        if not album or album.lower() in ("unknown", ""):
            album = all_parts[-2]

    if not artist:
        artist = "Unknown"
    if not album:
        album = "Unknown Album"

    rel_path = os.path.relpath(file_path, base_dir)
    return {
        "title": title,
        "artist": artist,
        "album": album,
        "file_name": os.path.basename(file_path),
        "rel_path": rel_path
    }


def query_llm_genre(meta: Dict[str, str], active_model: str, base_url: str = DEFAULT_BASE_URL) -> Optional[str]:
    """LLMに問い合わせてジャンルを推論"""
    system_role = (
        "あなたは日本の音楽ライブラリに精通したプロの音楽分類エディターです。"
        "提示された楽曲情報から、最も適した音楽ジャンル（1〜2個）を判定してください。"
        "余計な挨拶、注釈、前置き、理由説明は絶対に含めず、純粋なジャンル名のみをカンマ区切りで出力してください。"
    )

    prompt = f"""以下の楽曲のジャンルを1〜2個判定してください。

【楽曲情報】
・曲名: {meta['title']}
・アーティスト: {meta['artist']}
・アルバム: {meta['album']}
・ファイルパス: {meta['rel_path']}

【ジャンルの参考候補】
{", ".join(GENRE_GUIDE)}

【出力ルール】
・楽曲に最もふさわしいジャンル名をカンマ区切りで簡潔に出力してください。
・出力例: サウンドトラック (映画), アニメ映画
・出力例: ロック
・出力例: J ポップ
"""

    payload = {
        "model": active_model,
        "messages": [
            {"role": "system", "content": system_role},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.1,
        "max_tokens": 40,
        "stop": ["\nUser:", "\nAssistant:", "\nHuman:", "\n\n", "User:", "Note:", "(Note:"]
    }

    try:
        req = urllib.request.Request(
            f"{base_url}/chat/completions",
            headers={"Content-Type": "application/json"},
            data=json.dumps(payload).encode('utf-8')
        )
        with urllib.request.urlopen(req, timeout=45) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            raw_ans = data["choices"][0]["message"]["content"].strip()
            # 出力のクリーンアップ（引用符、パイプ記号、プレフィックス除去）
            clean_ans = re.sub(r'^(?:ジャンル[:：]|Genre[:：]|回答[:：])\s*', '', raw_ans, flags=re.IGNORECASE)
            clean_ans = clean_ans.strip().strip('"').strip("'").strip("`")
            # 1行目のみを取得
            first_line = clean_ans.splitlines()[0].strip() if clean_ans else ""
            # 末尾の「です」「でした」「である」や句読点を除去
            first_line = re.sub(r'[\s。．.]+$', '', first_line)
            first_line = re.sub(r'(?:です|でした|である|になります)$', '', first_line).strip()
            first_line = re.sub(r'[\s。．.]+$', '', first_line)
            return first_line if first_line else None
    except Exception as e:
        print(f"    [LLM Error] {e}")
        return None


def ensure_writable(file_path: str):
    """Windowsの読み取り専用属性が付与されている場合は解除"""
    try:
        mode = os.stat(file_path).st_mode
        if not (mode & stat.S_IWRITE):
            os.chmod(file_path, mode | stat.S_IWRITE)
    except Exception:
        pass


def write_genre_tag(file_path: str, new_genres: List[str]) -> bool:
    """音源ファイルにジャンルタグを書き込む"""
    ensure_writable(file_path)
    ext = os.path.splitext(file_path)[1].lower()
    genre_str = " / ".join(new_genres)

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
        if ext in ('.wma', '.asf'):
            audio = ASF(file_path)
            audio['WM/Genre'] = [ASFUnicodeAttribute(g) for g in new_genres]
            audio.save()
            return True

        elif ext == '.flac':
            audio = FLAC(file_path)
            audio['genre'] = new_genres
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

        elif ext == '.dsf':
            audio = DSF(file_path)
            if audio.tags is None:
                audio.add_tags()
            audio.tags.delall('TCON')
            audio.tags.add(TCON(encoding=3, text=new_genres))
            audio.save()
            return True

        else:
            raw_audio = MutagenFile(file_path)
            if raw_audio is not None and hasattr(raw_audio, 'tags') and raw_audio.tags is not None:
                raw_audio.tags['genre'] = new_genres
                raw_audio.save()
                return True

    except Exception as e:
        print(f"    [Write Error] {os.path.basename(file_path)}: {e}")
        return False

    return False


def update_db_genre(conn: sqlite3.Connection, file_path: str, genre_str: str) -> int:
    """music_meta.db の tracks テーブルの genre を更新"""
    cur = conn.cursor()
    normalized_path = file_path.replace('/', '\\')
    file_name = os.path.basename(file_path)

    # 1. 完全一致 または パス区切り正規化一致
    cur.execute("UPDATE tracks SET genre = ? WHERE file_path = ? OR REPLACE(file_path, '/', '\\') = ?",
                (genre_str, file_path, normalized_path))
    if cur.rowcount > 0:
        return cur.rowcount

    # 2. ファイル名部分一致
    cur.execute("UPDATE tracks SET genre = ? WHERE file_path LIKE ?", (genre_str, f"%{file_name}"))
    return cur.rowcount


def scan_for_untagged_files(music_dir: str) -> List[str]:
    """指定ディレクトリからジャンル未設定の音楽ファイルを探索"""
    untagged_files = []
    print(f"[Search] スキャン中: {music_dir} ...")
    start_t = time.time()
    total_scanned = 0

    for root, _, files in os.walk(music_dir):
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            if ext in SUPPORTED_EXTENSIONS:
                total_scanned += 1
                full_path = os.path.join(root, f)
                genres = get_current_genres(full_path)
                if not genres:
                    untagged_files.append(full_path)

        if total_scanned > 0 and total_scanned % 1000 == 0:
            print(f"  ... {total_scanned} 曲スキャン完了 (未設定: {len(untagged_files)} 件)")

    elapsed = time.time() - start_t
    print(f"[Search] スキャン完了: 総曲数 {total_scanned} 曲中、ジャンル未設定 {len(untagged_files)} 件 ({elapsed:.1f}秒)")
    return untagged_files


def main():
    parser = argparse.ArgumentParser(
        description="NAS/ローカル音楽ライブラリのジャンル未設定曲に対してLLMでGenreを自動推論・設定するスクリプト",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用例:
  # 1. プレビュー確認 (変更を保存せず推論結果を表示)
  python fill_missing_genres.py --dry-run --limit 5

  # 2. 全ての未設定曲にLLMでジャンルを設定して音源ファイルに保存
  python fill_missing_genres.py

  # 3. 音源ファイルタグの更新と同時に music_meta.db も更新
  python fill_missing_genres.py --update-db

  # 4. 対象ディレクトリを明示指定
  python fill_missing_genres.py --music-dir "\\\\homenas\\music\\Disney"
        """
    )

    default_dir = resolve_default_music_dir()
    parser.add_argument(
        "--music-dir",
        default=default_dir,
        help=f"スキャン対象ディレクトリ (デフォルト: {default_dir})"
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"Lemonade Server URL (デフォルト: {DEFAULT_BASE_URL})"
    )
    parser.add_argument(
        "--model",
        default=None,
        help="使用するLLMモデル名 (未指定時は稼働中モデルを自動検出)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="ファイルへの書き込みを行わず、推論結果のプレビューのみ実行"
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
        "--limit",
        type=int,
        default=None,
        help="処理する最大ファイル数（テスト用）"
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="詳細ログを出力"
    )

    args = parser.parse_args()

    print("=" * 70)
    print(" ジャンル未設定曲 LLM 自動設定スクリプト (fill_missing_genres.py)")
    print("=" * 70)
    print(f" 対象ディレクトリ : {args.music_dir}")
    print(f" LLM サーバー     : {args.base_url}")
    print(f" Dry Run          : {'有効 (書き込みなし)' if args.dry_run else '無効 (実際に書き込み実行)'}")
    print(f" DB 同期          : {'有効 (' + args.db_path + ')' if args.update_db else '無効'}")
    if args.limit:
        print(f" 件数制限         : 最大 {args.limit} 件")
    print("=" * 70)

    if not os.path.exists(args.music_dir):
        print(f"[Error] 指定されたディレクトリが存在しません: {args.music_dir}")
        sys.exit(1)

    # アクティブモデルの解決
    active_model = args.model if args.model else get_lemonade_active_model(args.base_url)
    print(f"[System] 使用モデル: {active_model}\n")

    # 未設定ファイルの探索
    untagged_files = scan_for_untagged_files(args.music_dir)
    if not untagged_files:
        print("[Result] ジャンル未設定の楽曲ファイルは見つかりませんでした。すべて設定済みです。")
        sys.exit(0)

    if args.limit and args.limit > 0:
        untagged_files = untagged_files[:args.limit]
        print(f"[Info] --limit 指定により先頭 {len(untagged_files)} 件を処理します。\n")

    # DB接続準備
    db_conn = None
    if args.update_db and not args.dry_run:
        if os.path.exists(args.db_path):
            try:
                db_conn = sqlite3.connect(args.db_path)
                print(f"[Database] {args.db_path} に接続しました。\n")
            except Exception as e:
                print(f"[Warning] データベース接続に失敗しました: {e}（DB更新はスキップします）\n")
        else:
            print(f"[Warning] データベースファイルが見つかりません: {args.db_path}（DB更新はスキップします）\n")

    # 処理ループ
    count_success = 0
    count_failed = 0
    count_db_updated = 0
    start_time = time.time()

    for idx, file_path in enumerate(untagged_files, 1):
        meta = extract_track_metadata(file_path, args.music_dir)
        print(f"[{idx}/{len(untagged_files)}] 推論中: {meta['rel_path']}")
        print(f"    情報: アーティスト='{meta['artist']}', アルバム='{meta['album']}', 曲名='{meta['title']}'")

        # LLM推論
        predicted_genre = query_llm_genre(meta, active_model, args.base_url)
        if not predicted_genre:
            print(f"    [Skip] ジャンルを推論できませんでした。スキップします。")
            count_failed += 1
            continue

        print(f"    ➔ 推論ジャンル: '{predicted_genre}'")

        # カンマまたはスラッシュで分割してリスト化
        genre_list = [g.strip() for g in re.split(r'[,/、／]\s*', predicted_genre) if g.strip()]
        if not genre_list:
            genre_list = [predicted_genre]

        if args.dry_run:
            count_success += 1
            print("    [DRY-RUN] タグ書き込みはスキップされました。")
        else:
            # タグ書き込み実行
            success = write_genre_tag(file_path, genre_list)
            if success:
                count_success += 1
                print(f"    [Write OK] タグを保存しました: {genre_list}")
                # DB更新
                if db_conn:
                    try:
                        db_genre_val = ", ".join(genre_list)
                        rows = update_db_genre(db_conn, file_path, db_genre_val)
                        count_db_updated += rows
                    except Exception as e:
                        print(f"    [DB Error] {e}")
            else:
                count_failed += 1
                print(f"    [Write Failed] タグの保存に失敗しました。")

        print("-" * 60)

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
    print(f" 対象未設定楽曲数 : {len(untagged_files)} 件")
    print(f" 推論成功 / 設定  : {count_success} 件 {'(※Dry-runのため変更なし)' if args.dry_run else ''}")
    if count_failed > 0:
        print(f" 推論 / 書込失敗  : {count_failed} 件")
    if args.update_db and not args.dry_run:
        print(f" DB更新レコード数 : {count_db_updated} 件")
    print(f" 処理所要時間     : {elapsed:.2f} 秒")
    print("=" * 70)


if __name__ == "__main__":
    main()
