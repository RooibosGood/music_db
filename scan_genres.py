# -*- coding: utf-8 -*-
"""
scan_genres.py
NASやローカルディレクトリに保存されている音楽ファイルのジャンルタグを洗い出し・集計するスクリプト

機能:
1. NAS/ローカルフォルダ内の音楽ファイルを再帰的に高速スキャン
2. 各種フォーマット（MP3, FLAC, WMA, M4A, OGG, WAV 等）からジャンルタグを高精度に抽出
3. ジャンル別の楽曲数・構成比・代表曲の集計・ランキング表示
4. 多彩なレポート出力（コンソール表示, Markdown, CSV, JSON, 未タグ曲リスト）
5. 既存データベース (music_meta.db) との比較・対比モード
"""

import os
import sys
import argparse
import json
import csv
import re
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from mutagen import File

# WindowsコンソールのUTF-8出力対応
if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

DEFAULT_MUSIC_DIR = r"\\homenas\music"
DB_PATH = "music_meta.db"

# 対応する音楽ファイル拡張子
SUPPORTED_EXTENSIONS = (
    '.mp3', '.flac', '.wma', '.m4a', '.aac',
    '.ogg', '.wav', '.alac', '.aiff', '.aif',
    '.dsf', '.dff', '.ape', '.wv'
)

# ID3v1 標準ジャンルコード定義（数値タグ展開用）
ID3_GENRES = [
    "Blues", "Classic Rock", "Country", "Dance", "Disco", "Funk", "Grunge", "Hip-Hop",
    "Jazz", "Metal", "New Age", "Oldies", "Other", "Pop", "R&B", "Rap", "Reggae", "Rock",
    "Techno", "Industrial", "Alternative", "Ska", "Death Metal", "Pranks", "Soundtrack",
    "Euro-Techno", "Ambient", "Trip-Hop", "Vocal", "Jazz+Funk", "Fusion", "Trance",
    "Classical", "Instrumental", "Acid", "House", "Game", "Sound Clip", "Gospel", "Noise",
    "Alternative Rock", "Bass", "Soul", "Punk", "Space", "Meditative", "Instrumental Pop",
    "Instrumental Rock", "Ethnic", "Gothic", "Darkwave", "Techno-Industrial", "Electronic",
    "Pop-Folk", "Eurodance", "Dream", "Southern Rock", "Comedy", "Cult", "Gangsta",
    "Top 40", "Christian Rap", "Pop/Funk", "Jungle", "Native US", "Cabaret", "New Wave",
    "Psychadelic", "Rave", "Showtunes", "Trailer", "Lo-Fi", "Tribal", "Acid Punk",
    "Acid Jazz", "Polka", "Retro", "Musical", "Rock & Roll", "Hard Rock", "Folk",
    "Folk-Rock", "National Folk", "Swing", "Fast Fusion", "Bebob", "Latin", "Revival",
    "Celtic", "Bluegrass", "Avantgarde", "Gothic Rock", "Progressive Rock",
    "Psychedelic Rock", "Symphonic Rock", "Slow Rock", "Big Band", "Chorus",
    "Easy Listening", "Acoustic", "Humour", "Speech", "Chanson", "Opera",
    "Chamber Music", "Sonata", "Symphony", "Booty Bass", "Primus", "Porn Groove",
    "Satire", "Slow Jam", "Club", "Tango", "Samba", "Folklore", "Ballad", "Power Ballad",
    "Rhythmic Soul", "Freestyle", "Duet", "Punk Rock", "Drum Solo", "Acapella",
    "Euro-House", "Dance Hall", "Goa", "Drum & Bass", "Club-House", "Hardcore",
    "Terror", "Indie", "BritPop", "Negerpunk", "Polsk Punk", "Beat", "Christian Gangsta",
    "Heavy Metal", "Black Metal", "Crossover", "Contemporary Christian", "Christian Rock",
    "Merengue", "Salsa", "Thrash Metal", "Anime", "JPop", "SynthPop"
]


def resolve_id3_genre(raw_genre: str) -> str:
    """ID3の '(13)' 形式や数値表記のジャンルを文字列に解決"""
    if not raw_genre:
        return raw_genre
    raw_genre = str(raw_genre).strip()
    
    # "(17)" または "(17)Rock" のパターン
    match = re.match(r'^\((\d+)\)(.*)$', raw_genre)
    if match:
        idx = int(match.group(1))
        suffix = match.group(2).strip()
        if 0 <= idx < len(ID3_GENRES):
            name = ID3_GENRES[idx]
            return f"{name} {suffix}".strip() if suffix and suffix != name else name
        return raw_genre
    
    # 純粋な数値 "17"
    if raw_genre.isdigit():
        idx = int(raw_genre)
        if 0 <= idx < len(ID3_GENRES):
            return ID3_GENRES[idx]
            
    return raw_genre


def extract_track_info(file_path: str) -> dict:
    """音源ファイルからタグ情報（タイトル、アーティスト、アルバム、ジャンルリスト）を抽出"""
    ext = os.path.splitext(file_path)[1].lower()
    title = None
    artist = None
    album = None
    raw_genres = []

    # 1. Mutagen Easyタグによる抽出
    try:
        audio_easy = File(file_path, easy=True)
        if audio_easy:
            if "title" in audio_easy:
                title = audio_easy["title"][0]
            if "artist" in audio_easy:
                artist = audio_easy["artist"][0]
            if "album" in audio_easy:
                album = audio_easy["album"][0]
            if "genre" in audio_easy:
                for g in audio_easy["genre"]:
                    if str(g).strip():
                        raw_genres.append(str(g).strip())
    except Exception:
        pass

    # 2. Easyタグでジャンルが取れなかった場合は Raw タグからフォールバック抽出
    if not raw_genres:
        try:
            audio_raw = File(file_path)
            if audio_raw and audio_raw.tags:
                tags = audio_raw.tags
                # ID3 (MP3, WAV等)
                if hasattr(tags, "getall"):
                    tcon_frames = tags.getall("TCON")
                    for frame in tcon_frames:
                        if hasattr(frame, "text"):
                            for t in frame.text:
                                if str(t).strip():
                                    raw_genres.append(str(t).strip())
                        elif str(frame).strip():
                            raw_genres.append(str(frame).strip())
                # Vorbis / MP4 / WMA / ASF
                elif isinstance(tags, dict) or hasattr(tags, "get"):
                    for key in ["GENRE", "genre", "\xa9gen", "WM/Genre", "gnre"]:
                        val = tags.get(key)
                        if val:
                            if isinstance(val, list):
                                for item in val:
                                    if hasattr(item, "value"):
                                        raw_genres.append(str(item.value).strip())
                                    elif str(item).strip():
                                        raw_genres.append(str(item).strip())
                            else:
                                if hasattr(val, "value"):
                                    raw_genres.append(str(val.value).strip())
                                elif str(val).strip():
                                    raw_genres.append(str(val).strip())
                            break
        except Exception:
            pass

    # ジャンル名の正規化とID3番号の展開
    cleaned_genres = []
    for g in raw_genres:
        resolved = resolve_id3_genre(g)
        if resolved:
            cleaned_genres.append(resolved)

    # 複数ジャンル区切り（/, ;, カンマ等）の分割リストも作成
    split_genres = []
    for g in cleaned_genres:
        # カンマ、スラッシュ、セミコロンで分割（ただし "R&B・ソウル" や "Rock/Pop" などを考慮）
        parts = re.split(r'[/;；／,、]\s*', g)
        for p in parts:
            p_clean = p.strip()
            if p_clean and p_clean not in split_genres:
                split_genres.append(p_clean)

    if not title:
        title = os.path.splitext(os.path.basename(file_path))[0]
    if not artist:
        artist = "Unknown"
    if not album:
        album = "Unknown Album"

    return {
        "file_path": file_path,
        "file_name": os.path.basename(file_path),
        "file_format": ext.lstrip('.'),
        "title": title,
        "artist": artist,
        "album": album,
        "raw_genres": cleaned_genres,
        "raw_genre_str": " / ".join(cleaned_genres) if cleaned_genres else None,
        "split_genres": split_genres if split_genres else ["(ジャンル未設定)"],
        "has_genre": len(cleaned_genres) > 0
    }


def scan_music_library(
    target_dir: str,
    target_format: str = "all",
    limit: int = None,
    progress_interval: int = 100
) -> list:
    """ディレクトリを再帰的にスキャンして全音楽ファイルのメタデータを抽出"""
    if not os.path.exists(target_dir):
        print(f"\n[Error] 指定されたディレクトリが見つかりません: {target_dir}")
        sys.exit(1)

    if target_format == "flac":
        allowed_exts = ('.flac',)
    elif target_format == "mp3":
        allowed_exts = ('.mp3',)
    elif target_format == "wma":
        allowed_exts = ('.wma',)
    elif target_format == "m4a":
        allowed_exts = ('.m4a', '.aac')
    else:
        allowed_exts = SUPPORTED_EXTENSIONS

    print(f"=== スキャン開始: {target_dir} ===")
    print(f"  対象拡張子: {', '.join(allowed_exts)}")
    print(f"  スキャン上限: {f'{limit} 曲' if limit else '無制限（全件）'}\n")

    tracks = []
    scanned_files = 0

    try:
        for root, _, files in os.walk(target_dir):
            for f in files:
                if not f.lower().endswith(allowed_exts):
                    continue

                full_path = os.path.join(root, f)
                info = extract_track_info(full_path)
                tracks.append(info)
                scanned_files += 1

                if scanned_files % progress_interval == 0:
                    print(f"  [スキャン中...] {scanned_files} ファイル解析完了 (現在: {info['artist']} - {info['title']})", end='\r')

                if limit and scanned_files >= limit:
                    print(f"\n  [情報] 指定上限 {limit} ファイルに達したためスキャンを完了します。")
                    break
            if limit and scanned_files >= limit:
                break
    except KeyboardInterrupt:
        print("\n\n[Warning] ユーザーによりスキャンが中断されました。これまでに取得したデータで集計を行います。")

    print(f"\n  [完了] 合計 {len(tracks)} 件の音楽ファイルを解析しました。\n")
    return tracks


def analyze_genres(tracks: list) -> dict:
    """抽出したトラック群からジャンル統計を計算"""
    total_tracks = len(tracks)
    if total_tracks == 0:
        return {
            "total_tracks": 0,
            "format_counts": {},
            "tagged_count": 0,
            "untagged_count": 0,
            "raw_genre_counts": Counter(),
            "split_genre_counts": Counter(),
            "genre_samples": defaultdict(list),
            "artist_genres": defaultdict(lambda: Counter()),
            "format_genres": defaultdict(lambda: Counter())
        }

    format_counts = Counter(t["file_format"] for t in tracks)
    tagged_count = sum(1 for t in tracks if t["has_genre"])
    untagged_count = total_tracks - tagged_count

    raw_genre_counts = Counter()
    split_genre_counts = Counter()
    genre_samples = defaultdict(list)
    artist_genres = defaultdict(lambda: Counter())
    format_genres = defaultdict(lambda: Counter())

    for t in tracks:
        fmt = t["file_format"]
        artist = t["artist"]
        sample_item = f"{t['artist']} - {t['title']} ({t['file_name']})"

        if not t["has_genre"]:
            raw_genre_counts["(ジャンル未設定)"] += 1
            split_genre_counts["(ジャンル未設定)"] += 1
            if len(genre_samples["(ジャンル未設定)"]) < 5:
                genre_samples["(ジャンル未設定)"].append(sample_item)
            format_genres[fmt]["(ジャンル未設定)"] += 1
        else:
            # 生ジャンル文字列での集計
            raw_str = t["raw_genre_str"]
            raw_genre_counts[raw_str] += 1
            if len(genre_samples[raw_str]) < 5:
                genre_samples[raw_str].append(sample_item)

            # 分割ジャンルでの集計
            for g in t["split_genres"]:
                split_genre_counts[g] += 1
                artist_genres[artist][g] += 1
                format_genres[fmt][g] += 1
                if len(genre_samples[g]) < 5 and sample_item not in genre_samples[g]:
                    genre_samples[g].append(sample_item)

    return {
        "total_tracks": total_tracks,
        "format_counts": format_counts,
        "tagged_count": tagged_count,
        "untagged_count": untagged_count,
        "raw_genre_counts": raw_genre_counts,
        "split_genre_counts": split_genre_counts,
        "genre_samples": genre_samples,
        "artist_genres": artist_genres,
        "format_genres": format_genres
    }


def print_terminal_report(analysis: dict, top_n: int = 50):
    """ターミナルに整形されたジャンル統計レポートを出力"""
    total = analysis["total_tracks"]
    if total == 0:
        print("[Info] 解析対象の音楽ファイルが見つかりませんでした。")
        return

    tagged = analysis["tagged_count"]
    untagged = analysis["untagged_count"]
    tag_pct = (tagged / total * 100) if total > 0 else 0
    untag_pct = (untagged / total * 100) if total > 0 else 0

    print("=" * 80)
    print(" 🎵 NAS 音楽ライブラリ ジャンル集計レポート")
    print("=" * 80)
    print(f"  総スキャン曲数  : {total:,} 曲")
    print(f"  ジャンル設定あり: {tagged:,} 曲 ({tag_pct:.1f}%)")
    print(f"  ジャンル未設定  : {untagged:,} 曲 ({untag_pct:.1f}%)")
    print(f"  フォーマット内訳: " + ", ".join(f"{k.upper()}: {v:,}曲" for k, v in analysis["format_counts"].items()))
    print(f"  検出ジャンル種類: {len(analysis['split_genre_counts']):,} 種類 (個別タグ: {len(analysis['raw_genre_counts']):,} 種類)")
    print("=" * 80)

    # 1. 分割ジャンルランキング
    print(f"\n【ジャンル別 楽曲数ランキング (上位 {min(top_n, len(analysis['split_genre_counts']))} 件)】")
    print(f"{'順位':>4} | {'ジャンル名':<28} | {'曲数':>7} | {'割合':>6} | {'代表曲 / アーティスト 例'}")
    print("-" * 80)

    items = analysis["split_genre_counts"].most_common(top_n if top_n > 0 else None)
    for rank, (genre, count) in enumerate(items, 1):
        pct = (count / total * 100) if total > 0 else 0
        samples = analysis["genre_samples"].get(genre, [])
        sample_str = samples[0] if samples else "-"
        if len(sample_str) > 35:
            sample_str = sample_str[:32] + "..."
        print(f"{rank:>4} | {genre:<28} | {count:>7,} | {pct:>5.1f}% | {sample_str}")

    # 2. フォーマット別ジャンルトップ3
    print("\n" + "=" * 80)
    print("【ファイル形式別 主要ジャンル Top 3】")
    print("-" * 80)
    for fmt, g_counts in sorted(analysis["format_genres"].items()):
        top3 = g_counts.most_common(3)
        top3_str = ", ".join(f"{g} ({c:,}曲)" for g, c in top3)
        print(f"  - {fmt.upper():<6} : {top3_str}")

    print("=" * 80 + "\n")


def generate_markdown_report(analysis: dict, output_path: str, target_dir: str):
    """Markdown形式でジャンル分析レポートを生成"""
    total = analysis["total_tracks"]
    tagged = analysis["tagged_count"]
    untagged = analysis["untagged_count"]
    tag_pct = (tagged / total * 100) if total > 0 else 0
    untag_pct = (untagged / total * 100) if total > 0 else 0

    lines = []
    lines.append("# NAS 音楽ライブラリ ジャンル洗い出しレポート\n")
    lines.append(f"- **スキャンディレクトリ**: `{target_dir}`")
    lines.append(f"- **総曲数**: {total:,} 曲")
    lines.append(f"- **ジャンル設定あり**: {tagged:,} 曲 ({tag_pct:.1f}%)")
    lines.append(f"- **ジャンル未設定**: {untagged:,} 曲 ({untag_pct:.1f}%)")
    lines.append(f"- **ユニークジャンル数**: {len(analysis['split_genre_counts']):,} 種類\n")

    lines.append("## 1. ファイル形式別内訳\n")
    lines.append("| フォーマット | 曲数 | 割合 |")
    lines.append("| :--- | :---: | :---: |")
    for fmt, cnt in analysis["format_counts"].most_common():
        pct = (cnt / total * 100) if total > 0 else 0
        lines.append(f"| {fmt.upper()} | {cnt:,} | {pct:.1f}% |")
    lines.append("")

    lines.append("## 2. ジャンル別楽曲数ランキング\n")
    lines.append("| 順位 | ジャンル名 | 楽曲数 | 割合 (%) | 代表曲・アーティスト例 |")
    lines.append("| :---: | :--- | :---: | :---: | :--- |")
    for rank, (genre, count) in enumerate(analysis["split_genre_counts"].most_common(), 1):
        pct = (count / total * 100) if total > 0 else 0
        samples = analysis["genre_samples"].get(genre, [])
        sample_txt = "<br>".join(samples[:2]) if samples else "-"
        lines.append(f"| {rank} | **{genre}** | {count:,} | {pct:.1f}% | {sample_txt} |")
    lines.append("")

    lines.append("## 3. フォーマット別 主要ジャンル\n")
    for fmt, g_counts in sorted(analysis["format_genres"].items()):
        lines.append(f"### {fmt.upper()} (計 {analysis['format_counts'][fmt]:,} 曲)")
        lines.append("| ジャンル | 曲数 | 割合 |")
        lines.append("| :--- | :---: | :---: |")
        fmt_total = analysis["format_counts"][fmt]
        for g, c in g_counts.most_common(10):
            p = (c / fmt_total * 100) if fmt_total > 0 else 0
            lines.append(f"| {g} | {c:,} | {p:.1f}% |")
        lines.append("")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"[Export] Markdownレポートを保存しました: {output_path}")


def export_csv_report(analysis: dict, output_path: str):
    """CSV形式でジャンル集計データを保存"""
    total = analysis["total_tracks"]
    with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["順位", "ジャンル名", "楽曲数", "割合(%)", "代表曲例1", "代表曲例2", "代表曲例3"])
        for rank, (genre, count) in enumerate(analysis["split_genre_counts"].most_common(), 1):
            pct = round((count / total * 100), 2) if total > 0 else 0
            samples = analysis["genre_samples"].get(genre, [])
            s1 = samples[0] if len(samples) > 0 else ""
            s2 = samples[1] if len(samples) > 1 else ""
            s3 = samples[2] if len(samples) > 2 else ""
            writer.writerow([rank, genre, count, pct, s1, s2, s3])
    print(f"[Export] CSV集計データを保存しました: {output_path}")


def export_json_report(analysis: dict, tracks: list, output_path: str):
    """JSON形式で全トラック情報および統計を保存"""
    data = {
        "summary": {
            "total_tracks": analysis["total_tracks"],
            "tagged_count": analysis["tagged_count"],
            "untagged_count": analysis["untagged_count"],
            "format_counts": dict(analysis["format_counts"]),
            "distinct_genres_count": len(analysis["split_genre_counts"])
        },
        "genre_ranking": [
            {
                "genre": g,
                "count": c,
                "percentage": round((c / analysis["total_tracks"] * 100), 2) if analysis["total_tracks"] > 0 else 0,
                "samples": analysis["genre_samples"].get(g, [])
            }
            for g, c in analysis["split_genre_counts"].most_common()
        ],
        "tracks": tracks
    }
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"[Export] 詳細JSONデータを保存しました: {output_path}")


def export_genre_list_txt(analysis: dict, output_path: str):
    """全ジャンル名のみの一覧テキストファイルを保存（50音/アルファベット順および曲数順）"""
    split_genres = analysis["split_genre_counts"]
    raw_genres = analysis["raw_genre_counts"]

    lines = []
    lines.append(f"# NAS 音楽ライブラリ 抽出ジャンル一覧 (全 {len(split_genres)} 種類)\n")
    lines.append("## ■ ジャンル一覧（曲数順）")
    for rank, (g, count) in enumerate(split_genres.most_common(), 1):
        lines.append(f"{rank:3d}. {g} ({count}曲)")

    lines.append("\n## ■ ジャンル名一覧（プレーンテキスト / コピー用）")
    for g, _ in split_genres.most_common():
        if g != "(ジャンル未設定)":
            lines.append(g)

    lines.append(f"\n## ■ 元タグ表記一覧 (全 {len(raw_genres)} 種類)")
    for rank, (g, count) in enumerate(raw_genres.most_common(), 1):
        lines.append(f"{rank:3d}. {g} ({count}曲)")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"[Export] 全ジャンル一覧テキストを保存しました: {output_path}")


def export_untagged_list(tracks: list, output_path: str):
    """ジャンルが未設定の音源ファイル一覧をテキストファイルに出力"""
    untagged_tracks = [t for t in tracks if not t["has_genre"]]
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(f"# ジャンル未設定 音源ファイル一覧 ({len(untagged_tracks)} 件)\n\n")
        for t in untagged_tracks:
            f.write(f"{t['file_format'].upper()}\t{t['artist']}\t{t['title']}\t{t['file_path']}\n")
    print(f"[Export] 未タグ音源一覧を保存しました ({len(untagged_tracks)} 件): {output_path}")


def compare_with_database(nas_analysis: dict, db_path: str = DB_PATH):
    """SQLite DB (music_meta.db) との比較・対比"""
    if not os.path.exists(db_path):
        print(f"\n[Warning] データベースファイルが見つかりません ({db_path})。比較をスキップします。")
        return

    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM tracks;")
        db_total = cur.fetchone()[0]

        cur.execute("SELECT genre, COUNT(*) FROM tracks GROUP BY genre ORDER BY COUNT(*) DESC;")
        db_genres = cur.fetchall()
        conn.close()

        print("\n" + "=" * 80)
        print(f" 🗄️  SQLite DB ({db_path}) との対比情報")
        print("=" * 80)
        print(f"  DB登録総曲数    : {db_total:,} 曲")
        print(f"  DB内ジャンル種類: {len(db_genres):,} 種類 (LLM分類による正規化ジャンル)")
        print("-" * 80)
        print(f"{'順位':>4} | {'DB分類ジャンル (ALLOWED_GENRES)':<32} | {'DB曲数':>7} | {'割合':>6}")
        print("-" * 80)
        for rank, (genre, count) in enumerate(db_genres[:20], 1):
            pct = (count / db_total * 100) if db_total > 0 else 0
            print(f"{rank:>4} | {(genre or '(NULL)'):<32} | {count:>7,} | {pct:>5.1f}%")
        print("=" * 80 + "\n")
    except Exception as e:
        print(f"[Warning] DB比較中にエラーが発生しました: {e}")


def main():
    parser = argparse.ArgumentParser(
        description="NAS/ローカル音楽ライブラリのジャンルタグ洗い出し・集計ツール",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用例:
  # NAS全体をスキャンして全ジャンルをファイルに出力＆コンソール表示
  python scan_genres.py

  # テストとして最初の100曲だけスキャン
  python scan_genres.py --limit 100

  # 出力ファイル名を指定して実行
  python scan_genres.py --md genre_report.md --txt genre_list.txt --csv genre_summary.csv

  # FLACファイルのみを対象に集計
  python scan_genres.py --flac-only

  # ファイル保存を行わずにコンソール表示のみ
  python scan_genres.py --no-save

  # 既存の music_meta.db との対比情報も表示
  python scan_genres.py --compare-db
"""
    )
    parser.add_argument("--dir", "-d", default=DEFAULT_MUSIC_DIR, help=f"スキャン対象ディレクトリ (デフォルト: {DEFAULT_MUSIC_DIR})")
    parser.add_argument("--limit", "-l", type=int, default=None, help="スキャンする曲数の上限 (0または未指定で全件)")
    parser.add_argument("--format", choices=["all", "flac", "mp3", "wma", "m4a"], default="all", help="対象ファイル形式 (デフォルト: all)")
    parser.add_argument("--flac-only", action="store_true", help="FLACファイルのみを対象にする (--format flac と同等)")
    parser.add_argument("--top", type=int, default=50, help="コンソールに表示するジャンルランキングの上位件数 (デフォルト: 50, 0で全件)")
    parser.add_argument("--md", "--output-md", dest="output_md", nargs="?", const="genre_report.md", default="genre_report.md", help="Markdown形式のレポートを出力 (デフォルト: 'genre_report.md')")
    parser.add_argument("--txt", "--output-txt", dest="output_txt", nargs="?", const="genre_list.txt", default="genre_list.txt", help="ジャンル一覧テキストを出力 (デフォルト: 'genre_list.txt')")
    parser.add_argument("--csv", "--output-csv", dest="output_csv", nargs="?", const="genre_summary.csv", default="genre_summary.csv", help="CSV形式の集計データを出力 (デフォルト: 'genre_summary.csv')")
    parser.add_argument("--json", "--output-json", dest="output_json", default=None, help="全曲および集計詳細をJSONファイルに出力")
    parser.add_argument("--export-untagged", dest="export_untagged", default=None, help="ジャンル未設定の音源ファイル一覧をテキスト出力")
    parser.add_argument("--no-save", action="store_true", help="ファイルへの自動保存を行わず、コンソール表示のみにする")
    parser.add_argument("--compare-db", action="store_true", help="ローカルの music_meta.db 内のジャンル集計と対比表示")

    args = parser.parse_args()

    target_dir = args.dir
    target_format = "flac" if args.flac_only else args.format
    limit_val = None if (args.limit is not None and args.limit <= 0) else args.limit

    tracks = scan_music_library(
        target_dir=target_dir,
        target_format=target_format,
        limit=limit_val
    )

    analysis = analyze_genres(tracks)

    # コンソールレポート
    print_terminal_report(analysis, top_n=args.top)

    # ファイル出力処理（デフォルトで全ジャンル出力）
    if not args.no_save:
        if args.output_md:
            generate_markdown_report(analysis, args.output_md, target_dir)

        if args.output_txt:
            export_genre_list_txt(analysis, args.output_txt)

        if args.output_csv:
            export_csv_report(analysis, args.output_csv)

        if args.output_json:
            export_json_report(analysis, tracks, args.output_json)

        if args.export_untagged:
            export_untagged_list(tracks, args.export_untagged)

    if args.compare_db:
        compare_with_database(analysis, db_path=DB_PATH)


if __name__ == "__main__":
    main()

