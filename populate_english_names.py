# -*- coding: utf-8 -*-
"""
populate_english_names.py
music_meta.db の tracks テーブルに対して title_en, artist_en を一括生成・更新する専用ツール。

機能:
1. 英語・ラテン文字楽曲の高速即時判定 (LLM不要・バイパス)
2. 日本語楽曲の公式英語名 / 自然なヘボン式ローマ字抽出 (ローカルLLM / Lemonade Server)
3. LLMオフライン時および高速処理用の pykakasi ローマ字変換フォールバック
4. バッチコミット・中断対応・ドライランモード
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

# WindowsコンソールのUTF-8出力対応
if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
if sys.stderr.encoding and sys.stderr.encoding.lower() != 'utf-8':
    try:
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

try:
    import pykakasi
    _kakasi = pykakasi.kakasi()
except ImportError:
    _kakasi = None

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "music_meta.db")
LEMONADE_BASE_URL = "http://localhost:13305/v1"

RE_JAPANESE = re.compile(r'[\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFF\u30FC]')

def has_japanese(text: str) -> bool:
    """日本語文字（ひらがな、カタカナ、漢字、長音符等）が含まれているか判定"""
    if not text:
        return False
    return bool(RE_JAPANESE.search(str(text)))

def convert_to_roman(text: str) -> str:
    """pykakasi によるヘボン式ローマ字変換（単語先頭大文字）"""
    if not text:
        return ""
    text_str = str(text).strip()
    if not has_japanese(text_str):
        return text_str
    if _kakasi is None:
        return text_str
    try:
        res = _kakasi.convert(text_str)
        words = []
        for item in res:
            hep = item.get("hepburn", "").strip()
            if hep:
                words.append(hep.capitalize())
            else:
                orig = item.get("orig", "").strip()
                if orig:
                    words.append(orig)
        roman = " ".join(words).strip()
        roman = re.sub(r"\s+", " ", roman)
        return roman if roman else text_str
    except Exception:
        return text_str

def get_active_model_name(base_url: str = LEMONADE_BASE_URL) -> str:
    """Lemonade Server から稼働中のモデル名を取得"""
    try:
        req = urllib.request.Request(f"{base_url}/models", headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=2.0) as response:
            if response.status == 200:
                data = json.loads(response.read().decode('utf-8'))
                if "data" in data and len(data["data"]) > 0:
                    model_id = data["data"][0]["id"]
                    return model_id
    except Exception:
        pass
    return "default"

def clean_name_str(val: str) -> str:
    """名前文字列のクレンジング（改行除去、引用符、構文ゴミ、末尾記号等の除去）"""
    if not val:
        return ""
    v = str(val).strip()
    # 改行やタブを空白化
    v = re.sub(r'[\r\n\t]+', ' ', v)
    v = re.sub(r'[\s\}、,`]+$', '', v)
    v = re.sub(r'^[\s`"]+', '', v)
    v = v.strip().strip('"').strip("'").strip()
    return v

def fetch_english_names_from_llm(
    client,
    model: str,
    title: str,
    artist: str,
    album: str = "",
    desc: str = ""
) -> tuple[str, str]:
    """Lemonade Server (LLM) から公式英題またはヘボン式ローマ字を取得"""
    if not client:
        return "", ""

    prompt = f"""Given this track information, output the official English title and official English artist name. If there is no official English name, provide natural Hepburn romanization (e.g., 'Sparkle', 'Umi no Mieru Machi', 'Tatsuro Yamashita').

Title: {title}
Artist: {artist}
Album: {album}
Context/Description: {desc[:200] if desc else 'None'}

Output strictly a JSON object with these two keys:
{{
  "title_en": "Official English title or Hepburn Romanization",
  "artist_en": "Official English artist name or Hepburn Romanization"
}}
"""
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": "You are a professional music metadata translator. Output strictly valid JSON with title_en and artist_en."
                },
                {"role": "user", "content": prompt}
            ],
            max_tokens=150,
            temperature=0.1,
            timeout=30.0
        )
        content = response.choices[0].message.content or ""

        # 1. 最短 JSON マッチ
        json_match = re.search(r"\{[^{}]*\}", content)
        if json_match:
            try:
                data = json.loads(json_match.group(0))
                t_en = clean_name_str(data.get("title_en", ""))
                a_en = clean_name_str(data.get("artist_en", ""))
                if t_en or a_en:
                    return t_en, a_en
            except Exception:
                pass

        # 2. 全体 JSON マッチ
        json_match_greedy = re.search(r"\{[\s\S]*\}", content)
        if json_match_greedy:
            try:
                data = json.loads(json_match_greedy.group(0))
                t_en = clean_name_str(data.get("title_en", ""))
                a_en = clean_name_str(data.get("artist_en", ""))
                if t_en or a_en:
                    return t_en, a_en
            except Exception:
                pass

        # 3. 行単位 / キーワード正規表現抽出
        t_en = ""
        a_en = ""
        m_title = re.search(r'["\']?title_en["\']?\s*[:=]\s*["\']([^"\'\n]+)["\']?', content, re.IGNORECASE)
        if m_title:
            t_en = clean_name_str(m_title.group(1))

        m_artist = re.search(r'["\']?artist_en["\']?\s*[:=]\s*["\']([^"\'\n]+)["\']?', content, re.IGNORECASE)
        if m_artist:
            a_en = clean_name_str(m_artist.group(1))

        if t_en or a_en:
            return t_en, a_en

    except Exception:
        pass
    return "", ""

def populate_english_names(
    db_path: str = DB_PATH,
    mode: str = "hybrid",
    limit: int = None,
    force_all: bool = False,
    dry_run: bool = False,
    batch_size: int = 1
):
    """tracks テーブルの title_en, artist_en を一括生成・更新"""
    if not os.path.exists(db_path):
        print(f"❌ [Error] データベースが見つかりません: {db_path}", flush=True)
        return

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # カラムの存在確認
    cur.execute("PRAGMA table_info(tracks);")
    columns = [col[1] for col in cur.fetchall()]
    if "title_en" not in columns or "artist_en" not in columns:
        print("[System] tracks テーブルに title_en / artist_en カラムを追加します。", flush=True)
        if "title_en" not in columns:
            cur.execute("ALTER TABLE tracks ADD COLUMN title_en TEXT;")
        if "artist_en" not in columns:
            cur.execute("ALTER TABLE tracks ADD COLUMN artist_en TEXT;")
        conn.commit()

    # 対象レコードの抽出（未設定または空文字のレコード）
    if force_all:
        query_sql = "SELECT id, title, artist, album, description_ja, description_en, title_en, artist_en FROM tracks ORDER BY id ASC"
    else:
        query_sql = """
        SELECT id, title, artist, album, description_ja, description_en, title_en, artist_en
        FROM tracks
        WHERE (title_en IS NULL OR title_en = '')
           OR (artist IS NOT NULL AND artist != '' AND artist NOT IN ('Unknown', 'unknown', 'アーティスト未設定') AND (artist_en IS NULL OR artist_en = ''))
        ORDER BY id ASC
        """

    if limit and limit > 0:
        query_sql += f" LIMIT {limit}"

    cur.execute(query_sql)
    records = cur.fetchall()
    total_targets = len(records)

    print(f"=== 英語名・ローマ字一括追加処理 ({'全件対象' if force_all else '未設定対象'}) ===", flush=True)
    print(f"  対象データベース: {db_path}", flush=True)
    print(f"  動作モード: {mode}", flush=True)
    print(f"  コミット間隔: 1件ごと即時コミット" if batch_size == 1 else f"  コミット間隔: {batch_size}件ごと", flush=True)
    print(f"  ドライラン: {'有効 (DB書き込みなし)' if dry_run else '無効 (DBに保存)'}", flush=True)
    print(f"  対象曲数: {total_targets} 件\n", flush=True)

    if total_targets == 0:
        print("🎉 すべての楽曲に title_en および artist_en が設定済みです。", flush=True)
        conn.close()
        return

    # LLM クライアントの初期化（hybrid / llm モード時）
    active_model = "default"
    llm_client = None
    if mode in ("hybrid", "llm"):
        active_model = get_active_model_name(LEMONADE_BASE_URL)
        if OpenAI and active_model != "default":
            llm_client = OpenAI(base_url=LEMONADE_BASE_URL, api_key="not-needed")
            print(f"🤖 [LLM] Lemonade Server 接続成功: 使用モデル = {active_model}", flush=True)
        else:
            print("⚠️ [LLM] Lemonade Server が利用できないため、pykakasi ローマ字変換フォールバックで実行します。", flush=True)

    count_passthrough = 0
    count_llm = 0
    count_kakasi = 0
    updated_count = 0
    start_time = time.time()

    try:
        for idx, row in enumerate(records, 1):
            track_id = row["id"]
            raw_title = (row["title"] or "").strip()
            raw_artist = (row["artist"] or "").strip()
            raw_album = (row["album"] or "").strip()
            desc = row["description_ja"] or row["description_en"] or ""

            # アーティスト名のクリーンアップ
            clean_artist = raw_artist
            if clean_artist in ("アーティスト未設定", "Unknown", "unknown", "None", "none", "null", ""):
                clean_artist = ""

            title_has_ja = has_japanese(raw_title)
            artist_has_ja = has_japanese(clean_artist)

            title_en_res = ""
            artist_en_res = ""
            method = ""

            # 1. タイトル・アーティスト共にラテン文字（英語等）のみの場合 -> 即時パススルー
            if not title_has_ja and not artist_has_ja:
                title_en_res = raw_title
                artist_en_res = clean_artist or raw_artist or "Unknown"
                method = "Pass-Through (Latin/English)"
                count_passthrough += 1
            else:
                # 2. 日本語を含む場合
                if mode in ("hybrid", "llm") and llm_client:
                    t_llm, a_llm = fetch_english_names_from_llm(
                        llm_client,
                        active_model,
                        raw_title,
                        clean_artist,
                        album=raw_album,
                        desc=desc
                    )
                    # LLM結果に日本語が残っている、または空の場合は pykakasi フォールバック
                    if t_llm and not has_japanese(t_llm):
                        title_en_res = t_llm
                    else:
                        title_en_res = convert_to_roman(raw_title)

                    if a_llm and not has_japanese(a_llm):
                        artist_en_res = a_llm
                    else:
                        artist_en_res = convert_to_roman(clean_artist) if clean_artist else (raw_artist or "Unknown")

                    method = "LLM (with kakasi fallback)"
                    count_llm += 1
                else:
                    # 3. pykakasi による高速ローマ字化
                    title_en_res = convert_to_roman(raw_title)
                    artist_en_res = convert_to_roman(clean_artist) if clean_artist else (raw_artist or "Unknown")
                    method = "pykakasi (Romanization)"
                    count_kakasi += 1

            # 結果クレンジング
            title_en_res = clean_name_str(title_en_res) or raw_title
            artist_en_res = clean_name_str(artist_en_res) or clean_artist or raw_artist or "Unknown"

            # ログ表示
            if idx <= 10 or idx % 50 == 0 or method != "Pass-Through (Latin/English)" or idx == total_targets:
                print(f"[{idx}/{total_targets}] ID:{track_id} | 原曲: {raw_title} / {raw_artist} -> 英語名: '{title_en_res}' / '{artist_en_res}' [{method}]", flush=True)

            if not dry_run:
                cur.execute(
                    "UPDATE tracks SET title_en = ?, artist_en = ? WHERE id = ?",
                    (title_en_res, artist_en_res, track_id)
                )
                updated_count += 1
                if batch_size == 1 or updated_count % batch_size == 0:
                    conn.commit()

        if not dry_run:
            conn.commit()

    except KeyboardInterrupt:
        print("\n\n⚠️ ユーザーによって処理が中断されました。処理済みの変更をコミットして終了します...", flush=True)
        if not dry_run:
            conn.commit()
    except Exception as e:
        print(f"\n❌ [Error] 処理中に予期せぬエラーが発生しました: {e}", flush=True)
        traceback.print_exc()
        if not dry_run:
            conn.commit()
    finally:
        conn.close()

    elapsed = time.time() - start_time
    print(f"\n=== 処理完了サマリー ===", flush=True)
    print(f"  総対象曲数: {total_targets} 件", flush=True)
    print(f"  更新完了数: {updated_count} 件" if not dry_run else f"  プレビュー数: {total_targets} 件 (ドライラン)", flush=True)
    print(f"  内訳: パススルー={count_passthrough} 件 / LLM={count_llm} 件 / Kakasi={count_kakasi} 件", flush=True)
    print(f"  所要時間: {elapsed:.2f} 秒 ({elapsed / max(1, total_targets):.3f} 秒/曲)", flush=True)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="music_meta.db の tracks テーブルに title_en, artist_en を一括追加・更新")
    parser.add_argument("--mode", choices=["hybrid", "kakasi", "llm"], default="hybrid", help="動作モード (hybrid: 英語パススルー+日本語LLM/kakasi, kakasi: 全件kakasi高速変換, llm: 日本語曲すべてLLM)")
    parser.add_argument("--limit", type=int, default=None, help="処理する曲数の上限 (テスト用)")
    parser.add_argument("--all", "--force", action="store_true", dest="force_all", help="設定済みの曲も含めて全曲上書き再生成")
    parser.add_argument("--dry-run", action="store_true", help="DB書き込みを行わず結果プレビューのみ表示")
    parser.add_argument("--batch-size", type=int, default=1, help="DBコミット間隔 (デフォルト: 1件ごと即時コミット)")
    parser.add_argument("--db-path", type=str, default=DB_PATH, help="対象データベースファイルのパス")
    args = parser.parse_args()

    populate_english_names(
        db_path=args.db_path,
        mode=args.mode,
        limit=args.limit,
        force_all=args.force_all,
        dry_run=args.dry_run,
        batch_size=args.batch_size
    )

