import os
import sys
import sqlite3
import json
import re
import argparse
import traceback
from pathlib import Path
from mutagen import File
from openai import OpenAI
try:
    from ddgs import DDGS
except ImportError:
    from duckduckgo_search import DDGS
import time

# 設定
NAS_MUSIC_DIR = r"\\homenas\music"
DB_PATH = "music_meta.db"
LEMONADE_BASE_URL = "http://localhost:13305/v1"
MAX_DURATION_SECONDS = 20 * 60  # 20分（1200秒）以上のファイルは除外

ALLOWED_GENRES = [
    "ジャズ",
    "ロック",
    "ポップ",
    "クラシック",
    "R&B・ソウル",
    "ブルース",
    "エレクトロニック",
    "フォーク・カントリー",
    "ヒップホップ",
    "サウンドトラック・インスト",
    "その他"
]

ALLOWED_CATEGORIES = ["邦楽", "洋楽", "その他"]

client = OpenAI(base_url=LEMONADE_BASE_URL, api_key="lemonade", timeout=120.0)

def get_active_model_name(exit_on_error: bool = False) -> str:
    """Lemonade Server でアクティブなモデル名を自動取得"""
    try:
        models = client.models.list()
        if models.data:
            return models.data[0].id
    except Exception as e:
        if exit_on_error:
            print(f"\n[Error] Lemonade Server ({LEMONADE_BASE_URL}) への接続・モデル取得に失敗しました: {e}")
            print("  Lemonade Server が起動しているか確認してください。")
            sys.exit(1)
        return "default"
    return "default"

ACTIVE_MODEL = get_active_model_name(exit_on_error=False)
if ACTIVE_MODEL != "default":
    print(f"[System] 使用モデル: {ACTIVE_MODEL}")

def init_db(reset: bool = False):
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        if reset:
            print("[System] 既存データを削除してデータベースを初期化します。")
            cur.execute("DROP TABLE IF EXISTS tracks;")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS tracks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_path TEXT UNIQUE NOT NULL,
            relative_path TEXT,
            file_format TEXT,
            is_hires INTEGER DEFAULT 0,
            sample_rate INTEGER,
            bit_depth INTEGER,
            duration_seconds INTEGER,
            title TEXT,
            artist TEXT,
            title_en TEXT,
            artist_en TEXT,
            album TEXT,
            release_year INTEGER,
            music_category TEXT,
            genre TEXT,
            mood TEXT,
            energy_level INTEGER,
            composer TEXT,
            performers TEXT,
            description_ja TEXT,
            description_en TEXT,
            analyzed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)
        # 既存DBへのカラムマイグレーション対応
        cur.execute("PRAGMA table_info(tracks);")
        columns = [col[1] for col in cur.fetchall()]
        if "file_format" not in columns:
            cur.execute("ALTER TABLE tracks ADD COLUMN file_format TEXT;")
        if "is_hires" not in columns:
            cur.execute("ALTER TABLE tracks ADD COLUMN is_hires INTEGER DEFAULT 0;")
        if "sample_rate" not in columns:
            cur.execute("ALTER TABLE tracks ADD COLUMN sample_rate INTEGER;")
        if "bit_depth" not in columns:
            cur.execute("ALTER TABLE tracks ADD COLUMN bit_depth INTEGER;")
        if "duration_seconds" not in columns:
            cur.execute("ALTER TABLE tracks ADD COLUMN duration_seconds INTEGER;")
        if "title_en" not in columns:
            cur.execute("ALTER TABLE tracks ADD COLUMN title_en TEXT;")
        if "artist_en" not in columns:
            cur.execute("ALTER TABLE tracks ADD COLUMN artist_en TEXT;")
        if "music_category" not in columns:
            cur.execute("ALTER TABLE tracks ADD COLUMN music_category TEXT;")
        if "description_ja" not in columns:
            cur.execute("ALTER TABLE tracks ADD COLUMN description_ja TEXT;")
            # 旧descriptionカラムが存在する場合はデータを移行
            if "description" in columns:
                cur.execute("UPDATE tracks SET description_ja = description WHERE description_ja IS NULL AND description IS NOT NULL;")
        if "description_en" not in columns:
            cur.execute("ALTER TABLE tracks ADD COLUMN description_en TEXT;")
        conn.commit()
        return conn
    except Exception as e:
        print(f"\n[Error] データベース初期化に失敗しました ({DB_PATH}): {e}")
        traceback.print_exc()
        sys.exit(1)

def cleanup_long_tracks_from_db(conn):
    """既存DB内に登録されている20分（1200秒）以上の楽曲を削除"""
    try:
        cur = conn.cursor()
        # duration_seconds >= 1200 のものを抽出
        cur.execute("SELECT id, title, artist, file_path, duration_seconds FROM tracks WHERE duration_seconds >= ?", (MAX_DURATION_SECONDS,))
        rows = cur.fetchall()
        
        # duration_seconds が未設定（NULL）のレコードもタグを再確認
        cur.execute("SELECT id, title, artist, file_path FROM tracks WHERE duration_seconds IS NULL;")
        null_rows = cur.fetchall()
        for row_id, title, artist, file_path in null_rows:
            if os.path.exists(file_path):
                try:
                    audio = File(file_path, easy=True)
                    length = int(getattr(audio.info, "length", 0)) if audio and audio.info else 0
                    if length >= MAX_DURATION_SECONDS:
                        rows.append((row_id, title, artist, file_path, length))
                    else:
                        cur.execute("UPDATE tracks SET duration_seconds = ? WHERE id = ?", (length, row_id))
                except Exception:
                    pass

        if rows:
            print(f"[Cleanup] データベース内に20分以上のファイルが {len(rows)} 件見つかりました。削除します:")
            for row_id, title, artist, file_path, dur in rows:
                dur_min = (dur or 0) / 60
                print(f"  - 削除: ID={row_id} | {artist} - {title} ({dur_min:.1f}分) | {Path(file_path).name}")
                cur.execute("DELETE FROM tracks WHERE id = ?", (row_id,))
            conn.commit()
            print(f"[Cleanup] {len(rows)} 件の長尺ファイルをデータベースから除外しました。\n")
        else:
            conn.commit()
    except Exception as e:
        print(f"[Warning] 既存長尺ファイルのクリーンアップ中にエラーが発生しました: {e}")

def extract_tags(file_path: str):
    """MP3 / FLAC からID3/Vorbisタグおよびオーディオ仕様（フォーマット・ハイレゾ判定・再生時間）を抽出"""
    try:
        audio = File(file_path, easy=True)
        if audio is None:
            print(f"\n[Error] 音源ファイルとして読み込めませんでした: {file_path}")
            sys.exit(1)
        
        ext = Path(file_path).suffix.lstrip('.').lower()
        sample_rate = None
        bit_depth = None
        duration_seconds = None
        if audio.info:
            sample_rate = getattr(audio.info, "sample_rate", None)
            bit_depth = getattr(audio.info, "bits_per_sample", None)
            duration_seconds = int(getattr(audio.info, "length", 0))
        
        # ハイレゾ判定: 可逆圧縮/非圧縮音源で、サンプリングレートが48kHz超または量子化ビット数が16bit超
        is_lossless = ext in ("flac", "wav", "aiff", "alac", "dsd", "dsf", "dff")
        is_hires = 1 if is_lossless and ((sample_rate and sample_rate > 48000) or (bit_depth and bit_depth > 16)) else 0

        return {
            "title": audio.get("title", [Path(file_path).stem])[0],
            "artist": audio.get("artist", ["Unknown"])[0],
            "album": audio.get("album", [""])[0],
            "year": audio.get("date", [""])[0][:4],
            "file_format": ext,
            "sample_rate": sample_rate,
            "bit_depth": bit_depth,
            "duration_seconds": duration_seconds,
            "is_hires": is_hires
        }
    except Exception as e:
        print(f"\n[Error] タグ抽出中にエラーが発生しました: {file_path}")
        print(f"  詳細: {type(e).__name__} - {e}")
        traceback.print_exc()
        sys.exit(1)

def search_web_info(title: str, artist: str) -> str:
    """楽曲の背景やエピソードをWeb検索"""
    if artist == "Unknown" or not title:
        return ""
    query = f"{artist} {title} song music review background genre"
    try:
        results = DDGS().text(query, max_results=3)
        time.sleep(1)  # レートリミット回避
        return "\n".join([r['body'] for r in results]) if results else ""
    except Exception as e:
        return ""

def parse_key_value(text: str) -> dict:
    """Key-Value形式（およびJSON）のLLM出力を柔軟にパース"""
    result = {
        "music_category": "その他",
        "genre": None,
        "mood": None,
        "energy_level": 3,
        "composer": None,
        "performers": None,
        "release_year": None,
        "title_en": None,
        "artist_en": None,
        "description_ja": None,
        "description_en": None
    }
    
    # JSON形式で返ってきた場合のフォールバックパース
    try:
        json_match = re.search(r"\{[\s\S]*\}", text)
        if json_match:
            data = json.loads(json_match.group(0))
            for k, v in data.items():
                if not v or str(v).lower() in ("null", "none", "unknown", "n/a"):
                    continue
                k_upper = k.upper().replace("_", "")
                if "CATEGORY" in k_upper:
                    for cat in ALLOWED_CATEGORIES:
                        if cat in str(v):
                            result["music_category"] = cat
                            break
                elif any(d in k_upper for d in ("TITLEEN", "ENGLISHTITLE", "TITLEENGLISH", "ENGLISHTRACK")):
                    result["title_en"] = str(v).strip()
                elif any(d in k_upper for d in ("ARTISTEN", "ENGLISHARTIST", "ARTISTENGLISH")):
                    result["artist_en"] = str(v).strip()
                elif "GENRE" in k_upper:
                    matched = [g for g in ALLOWED_GENRES if g in str(v)]
                    result["genre"] = ", ".join(matched) if matched else str(v)
                elif "MOOD" in k_upper:
                    result["mood"] = str(v)
                elif "ENERGY" in k_upper:
                    digits = re.findall(r"\d+", str(v))
                    if digits:
                        result["energy_level"] = max(1, min(5, int(digits[0])))
                elif "COMPOSER" in k_upper:
                    result["composer"] = str(v)
                elif "PERFORMER" in k_upper:
                    result["performers"] = str(v)
                elif "YEAR" in k_upper:
                    digits = re.findall(r"\b(19\d\d|20\d\d)\b", str(v))
                    if digits:
                        result["release_year"] = int(digits[0])
                elif "JA" in k_upper or "JAPANESE" in k_upper or "日本語" in k_upper:
                    result["description_ja"] = str(v).strip()
                elif "EN" in k_upper or "ENGLISH" in k_upper or "英語" in k_upper:
                    result["description_en"] = str(v).strip()
                elif any(d in k_upper for d in ("DESC", "EXPLAIN", "SUMMARY")):
                    if not result.get("description_ja"):
                        result["description_ja"] = str(v).strip()

            if result.get("description_ja") or result.get("description_en") or result.get("title_en"):
                return result
    except Exception:
        pass

    # 行ごとのパース（全角コロン・マークダウン記号・複数行DESCRIPTION対応）
    current_key = None
    desc_ja_lines = []
    desc_en_lines = []
    desc_fallback_lines = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        # 全角コロンを半角に統一
        normalized_line = line.replace("：", ":")

        if ":" in normalized_line:
            k_part, v_part = normalized_line.split(":", 1)
            # マークダウン記号や余計な文字を除去
            clean_k = re.sub(r"[\*\-\#`]", "", k_part).strip().upper()
            k_upper = clean_k.replace("_", "").replace(" ", "")
            v = v_part.strip().strip('"').strip("'")

            # 既知のキーか判定
            if any(term in k_upper for term in ("MUSICCATEGORY", "CATEGORY", "区分", "カテゴリ")):
                current_key = "MUSIC_CATEGORY"
                for cat in ALLOWED_CATEGORIES:
                    if cat in v:
                        result["music_category"] = cat
                        break
            elif any(term in k_upper for term in ("TITLEEN", "ENGLISHTITLE", "曲名英語", "英語曲名", "タイトル英語", "英語タイトル")):
                current_key = "TITLE_EN"
                if v and v.lower() not in ("null", "none", "unknown", "n/a"):
                    result["title_en"] = v
            elif any(term in k_upper for term in ("ARTISTEN", "ENGLISHARTIST", "アーティスト英語", "英語アーティスト", "歌手英語", "英語歌手")):
                current_key = "ARTIST_EN"
                if v and v.lower() not in ("null", "none", "unknown", "n/a"):
                    result["artist_en"] = v
            elif "GENRE" in k_upper or "ジャンル" in k_upper:
                current_key = "GENRE"
                if v and v.lower() not in ("null", "none", "unknown", "n/a"):
                    matched_genres = [g for g in ALLOWED_GENRES if g in v]
                    if matched_genres:
                        result["genre"] = ", ".join(matched_genres)
                    else:
                        result["genre"] = v if v in ALLOWED_GENRES else "その他"
            elif "MOOD" in k_upper or "ムード" in k_upper or "気分" in k_upper:
                current_key = "MOOD"
                if v and v.lower() not in ("null", "none", "unknown", "n/a"):
                    result["mood"] = v
            elif "ENERGY" in k_upper or "エネルギー" in k_upper:
                current_key = "ENERGY_LEVEL"
                digits = re.findall(r"\d+", v)
                if digits:
                    result["energy_level"] = max(1, min(5, int(digits[0])))
            elif "COMPOSER" in k_upper or "作曲" in k_upper:
                current_key = "COMPOSER"
                if v and v.lower() not in ("null", "none", "unknown", "n/a"):
                    result["composer"] = v
            elif "PERFORMER" in k_upper or "演奏" in k_upper or "アーティスト" in k_upper:
                current_key = "PERFORMERS"
                if v and v.lower() not in ("null", "none", "unknown", "n/a"):
                    result["performers"] = v
            elif "RELEASEYEAR" in k_upper or "YEAR" in k_upper or "年" in k_upper:
                current_key = "RELEASE_YEAR"
                digits = re.findall(r"\b(19\d\d|20\d\d)\b", v)
                if digits:
                    result["release_year"] = int(digits[0])
            elif any(term in k_upper for term in ("DESCRIPTIONJA", "DESCJA", "日本語説明", "日本語解説", "解説日本語", "説明日本語", "JAPANESE")):
                current_key = "DESCRIPTION_JA"
                if v and v.lower() not in ("null", "none", "unknown", "n/a"):
                    desc_ja_lines.append(v)
            elif any(term in k_upper for term in ("DESCRIPTIONEN", "DESCEN", "英語説明", "英語解説", "解説英語", "説明英語", "ENGLISH")):
                current_key = "DESCRIPTION_EN"
                if v and v.lower() not in ("null", "none", "unknown", "n/a"):
                    desc_en_lines.append(v)
            elif any(term in k_upper for term in ("DESCRIPTION", "DESC", "説明", "解説", "概要", "紹介")):
                current_key = "DESCRIPTION_FALLBACK"
                if v and v.lower() not in ("null", "none", "unknown", "n/a"):
                    desc_fallback_lines.append(v)
            else:
                # 不明なキーでかつ直前がDESCRIPTION系の場合は追記
                if current_key == "DESCRIPTION_JA":
                    desc_ja_lines.append(line)
                elif current_key == "DESCRIPTION_EN":
                    desc_en_lines.append(line)
                elif current_key == "DESCRIPTION_FALLBACK":
                    desc_fallback_lines.append(line)
                else:
                    current_key = None
        else:
            # コロンを含まない行（改行された説明文など）
            if current_key == "DESCRIPTION_JA":
                desc_ja_lines.append(line)
            elif current_key == "DESCRIPTION_EN":
                desc_en_lines.append(line)
            elif current_key == "DESCRIPTION_FALLBACK":
                desc_fallback_lines.append(line)

    if desc_ja_lines and not result.get("description_ja"):
        result["description_ja"] = " ".join(desc_ja_lines).strip()

    if desc_en_lines and not result.get("description_en"):
        result["description_en"] = " ".join(desc_en_lines).strip()

    if desc_fallback_lines and not result.get("description_ja"):
        result["description_ja"] = " ".join(desc_fallback_lines).strip()

    def clean_text_val(val: str) -> str:
        if not val:
            return ""
        # マークダウンの閉じタグやJSONの閉じ括弧、余計なクォートやカンマを除去
        v = re.sub(r'[\s\}、,`]+$', '', val.strip())
        v = re.sub(r'^[\s`"]+', '', v)
        v = v.strip().strip('"').strip("'").strip()
        return v

    for field in ("mood", "composer", "performers", "title_en", "artist_en"):
        if result.get(field):
            result[field] = clean_text_val(result[field])

    for field in ("description_ja", "description_en"):
        if result.get(field):
            desc = re.sub(r'[\s\}\]`]+$', '', result[field].strip())
            desc = re.sub(r'^[\s`"]+', '', desc)
            result[field] = desc.strip().strip('"').strip("'").strip()

    return result

def enrich_metadata_with_llm(tag_info: dict, web_context: str) -> dict:
    """Lemonade Server (LLM) を利用してメタデータを構造化"""
    genres_options = ", ".join(ALLOWED_GENRES)
    prompt = f"""Analyze this music track and generate metadata in JSON format.

Title: {tag_info['title']}
Artist: {tag_info['artist']}
Album: {tag_info['album']}
Estimated Year: {tag_info['year']}

Web Context:
{web_context}

Output strictly a JSON object with these keys:
- "category": "邦楽" (for Japanese artists/music) or "洋楽" (for Western/International music) or "その他"
- "title_en": Official English title, or natural Hepburn romanization (e.g. "Sparkle", "Umi no Mieru Machi")
- "artist_en": Official English artist name, or natural Hepburn romanization (e.g. "Tatsuro Yamashita", "Hikaru Utada")
- "genre": One or more matching genres from [{genres_options}]
- "mood": Comma-separated mood keywords (e.g. Upbeat, Energetic, Calm, Melancholic)
- "energy_level": Integer from 1 (quiet) to 5 (very energetic)
- "composer": Composer name or null
- "performers": Performer names or null
- "release_year": 4-digit year as integer
- "description_ja": 1-2 sentences introduction and background of the song written in Japanese (日本語で1〜2文の解説)
- "description_en": 1-2 sentences introduction and background of the song written in English (英語で1〜2文の解説)
"""
    response = client.chat.completions.create(
        model=ACTIVE_MODEL,
        messages=[
            {
                "role": "system",
                "content": "You are a music metadata analysis assistant. You always output valid JSON with English title/artist and both Japanese and English descriptions."
            },
            {"role": "user", "content": prompt}
        ],
        max_tokens=800,
        temperature=0.1
    )
    raw_content = response.choices[0].message.content or ""
    return parse_key_value(raw_content)

def process_music_library(limit: int = None, reset: bool = False, target_format: str = "all"):
    conn = init_db(reset=reset)
    cur = conn.cursor()
    
    # 既存DB内の20分以上の長尺音源をクリーンアップ
    if not reset:
        cleanup_long_tracks_from_db(conn)

    processed_count = 0
    attempt_count = 0
    skipped_count = 0

    if target_format == "flac":
        allowed_exts = ('.flac',)
        format_label = "FLACのみ"
    elif target_format == "mp3":
        allowed_exts = ('.mp3',)
        format_label = "MP3のみ"
    else:
        allowed_exts = ('.mp3', '.flac')
        format_label = "全形式 (MP3 / FLAC)"

    mode_name = "全件リセット＆再作成 (reset)" if reset else "差分追加（登録済みスキップ）"
    print(f"=== スキャン開始: {NAS_MUSIC_DIR} ===")
    print(f"  実行モード: {mode_name}")
    print(f"  対象形式: {format_label}")
    print(f"  除外条件: 再生時間 {MAX_DURATION_SECONDS // 60}分 以上のファイル")
    print(f"  処理上限: {f'新規 {limit} 曲' if limit else '無制限'}\n")

    for root, _, files in os.walk(NAS_MUSIC_DIR):
        for f in files:
            if not f.lower().endswith(allowed_exts):
                continue
            
            full_path = os.path.join(root, f)
            rel_path = os.path.relpath(full_path, NAS_MUSIC_DIR).replace("\\", "/")
            
            # 登録済みスキップ
            cur.execute("SELECT id FROM tracks WHERE file_path = ?", (full_path,))
            if cur.fetchone():
                skipped_count += 1
                continue

            attempt_count += 1
            print(f"[{attempt_count}曲目] 処理中: {f}")
            tags = extract_tags(full_path)
            if not tags:
                if limit and attempt_count >= limit:
                    print(f"\n=== 試行上限 {limit} 曲に達したため終了します ===")
                    print(f"  新規登録: {processed_count} 件 / スキップ: {skipped_count} 件")
                    conn.close()
                    return
                continue

            # 再生時間が20分以上のファイルを除外（スキップ）
            if tags.get("duration_seconds") and tags["duration_seconds"] >= MAX_DURATION_SECONDS:
                dur_min = tags["duration_seconds"] / 60
                print(f"  [Skip] 再生時間が20分以上の長尺音源のためスキップします ({dur_min:.1f}分)")
                skipped_count += 1
                continue

            dur_str = f"{tags['duration_seconds'] // 60}:{tags['duration_seconds'] % 60:02d}" if tags.get("duration_seconds") else "不明"
            print(f"  基本情報: {tags['title']} / {tags['artist']} [{tags['file_format'].upper()}{' (Hi-Res)' if tags['is_hires'] else ''} | {dur_str}]")
            web_context = search_web_info(tags["title"], tags["artist"])
            
            try:
                meta = enrich_metadata_with_llm(tags, web_context)
                title_en_val = meta.get('title_en') or tags.get('title')
                artist_en_val = meta.get('artist_en') or tags.get('artist')
                desc_ja_prev = (meta.get('description_ja')[:20] + '...') if meta.get('description_ja') else 'なし'
                desc_en_prev = (meta.get('description_en')[:20] + '...') if meta.get('description_en') else 'なし'
                print(f"  抽出結果: 英語名={title_en_val} / {artist_en_val} | 区分={meta.get('music_category')} | ジャンル={meta.get('genre')} | 気分={meta.get('mood')} | エネルギー={meta.get('energy_level')} | 解説(日)={desc_ja_prev} | 解説(英)={desc_en_prev}")
                
                cur.execute("""
                INSERT INTO tracks (
                    file_path, relative_path, file_format, is_hires, sample_rate, bit_depth, duration_seconds,
                    title, artist, title_en, artist_en, album,
                    release_year, music_category, genre, mood, energy_level, composer, performers,
                    description_ja, description_en
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    full_path,
                    rel_path,
                    tags["file_format"],
                    tags["is_hires"],
                    tags["sample_rate"],
                    tags["bit_depth"],
                    tags["duration_seconds"],
                    tags["title"],
                    tags["artist"],
                    meta.get("title_en"),
                    meta.get("artist_en"),
                    tags["album"],
                    meta.get("release_year") or (int(tags["year"]) if tags["year"].isdigit() else None),
                    meta.get("music_category"),
                    meta.get("genre"),
                    meta.get("mood"),
                    meta.get("energy_level", 3),
                    meta.get("composer"),
                    meta.get("performers"),
                    meta.get("description_ja"),
                    meta.get("description_en")
                ))
                conn.commit()
                processed_count += 1

            except Exception as e:
                print(f"\n[Error] メタデータ解析またはDB登録中にエラーが発生したため、処理を停止します。")
                print(f"  対象ファイル: {full_path}")
                print(f"  エラー種類: {type(e).__name__}")
                print(f"  エラー詳細: {e}")
                print("\n--- トレースバック ---")
                traceback.print_exc()
                conn.close()
                sys.exit(1)

            if limit and attempt_count >= limit:
                print(f"\n=== 試行上限 {limit} 曲に達したため終了します ===")
                print(f"  新規登録: {processed_count}/{attempt_count} 件 (スキップ: {skipped_count} 件)")
                conn.close()
                return

    conn.close()
    print(f"\n=== 全曲処理完了 ===")
    print(f"  新規登録: {processed_count} 件")
    print(f"  スキップ(登録済): {skipped_count} 件")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="音楽ライブラリのメタデータ収集・DB構築スクリプト")
    parser.add_argument("--limit", type=int, default=3, help="処理する曲数の上限 (テスト用。0で無制限)")
    parser.add_argument("--reset", action="store_true", help="既存のDBテーブルを削除し、全曲最初からやり直す")
    parser.add_argument("--mode", choices=["update", "reset"], default=None, help="動作モード (update: 未登録曲のみ追加, reset: 最初からやり直す)")
    parser.add_argument("--flac-only", action="store_true", help="FLACファイルのみを対象にする")
    parser.add_argument("--format", choices=["all", "flac", "mp3"], default="all", help="対象ファイル形式の指定 (all: 全形式, flac: FLACのみ, mp3: MP3のみ)")
    args = parser.parse_args()
    
    limit_val = None if args.limit == 0 else args.limit
    is_reset = args.reset or (args.mode == "reset")
    target_format = "flac" if args.flac_only else args.format
    
    try:
        process_music_library(limit=limit_val, reset=is_reset, target_format=target_format)
    except KeyboardInterrupt:
        print("\n[System] ユーザーによって処理が中断されました。")
        sys.exit(0)
