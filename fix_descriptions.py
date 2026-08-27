# -*- coding: utf-8 -*-
"""
fix_descriptions.py
SQLiteデータベース (music_meta.db) の楽曲解説文 (description_ja / description_en) 修復ツール

機能:
1. 高精度な異常検出・分類
   - description_ja: 純中国語文、日中混在文、簡体字混入、LLMメタ発言、文字化け、構文ゴミ
   - description_en: 日本語/中国語混入、LLMメタ発言・プロンプト漏れ、構文ゴミ、引用符破損
2. ルールベースの高速構文クリーンアップ (前後のゴミ記号除去・年号正規化)
3. ローカルLLM (Lemonade Server) による高品質な解説文再生成
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

# WindowsコンソールのUTF-8出力対応
if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

DB_PATH = "music_meta.db"
LEMONADE_BASE_URL = "http://localhost:13305/v1"

# --- 文字・構文判定用正規表現 ---

HIRAGANA_REGEX = re.compile(r'[\u3040-\u309F]')
KATAKANA_REGEX = re.compile(r'[\u30A0-\u30FF]')
CJK_REGEX = re.compile(r'[\u4E00-\u9FFF\u3400-\u4DBF\uF900-\uFAFF]')

# 日本語では基本的に使用されない明確な簡体字
SIMPLIFIED_CHINESE_CHARS = set(
    "这为发产并关兴写农凉减凤凭凯创动华单变响广张弹总执摇杂标条极样桥历汉汤波滥爱脸谱质载轻边达运进连选释钟录键"
    "录乐发专辑创现编词简欢听众动带历摇滚队艺术响标志质轻运连选钟键们为产关兴写单变总执样桥气汤滥脸谱载边达进释"
    "说话让给从对岁时后当时之后由在与和它她他您么吗呢吧着得过把被"
)

# 明確な中国語構文・語彙パターン (description_ja 内の中国語混入を精密検出)
ZH_SYNTAX_WORDS_REGEX = re.compile(
    r'(这首|这支|这首歌曲|这首曲子|该曲|此曲|本曲|一首歌曲|一首曲子|'
    r'收录于|发行于|发布于|创作于|推出于|收录在|'
    r'展现了|展现出|融合了|充满了|具有.*魅力|体现了|营造出|传递出|流露出|令[人听][沉醉感受]|'
    r'不仅.*而且|不仅如此|乐手|主唱|吉他手|贝斯手|鼓手|老鹰乐队|'
    r'经典之作|代表性作品|里程碑式|'
    r'的一首|是一首|是一支|的专辑|中的一首|作为.*的一首|'
    r'充满力量感|旋律优美|节奏感强|独特的旋律|深情的演唱|现场专辑|现场版|'
    r'[\u4e00-\u9fa5]{2,}[的是在了和与被把让从到对][\u4e00-\u9fa5]{2,})'
)

# LLMメタ発言・プロンプト漏れ・ゴミテキスト
LLM_META_REGEX = re.compile(
    r'(here is|here\'s|alternatively|let me know|if you\'d like|if you need|note:|note :|option \d|'
    r'i rewrote|i have rewritten|natural english|standard english|as requested|'
    r'japanese:|english:|description:|explanation:|translate|translation|'
    r'best regards|\[your name\]|\[your contact|\[your email|\[your phone|'
    r'以下是|这是|中文|日文|英文|```|不，|请注意|注意：|注：|当然|这篇|介绍|'
    r'json|performperf|performper)',
    re.IGNORECASE
)


def get_lemonade_model(base_url: str = LEMONADE_BASE_URL) -> str:
    """Lemonade Server からアクティブなモデル名を取得"""
    try:
        req = urllib.request.Request(f"{base_url}/models", headers={"User-Agent": "fix-descriptions-script"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            if "data" in data and len(data["data"]) > 0:
                return data["data"][0]["id"]
    except Exception:
        pass
    return "Meta-Llama-3.1-8B-Instruct-Hybrid"


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

    # 8. 1600s, 1700s -> 1960s, 1970s (文脈による誤表記)
    text = re.sub(r'\b1600s\b', '1960s', text)
    text = re.sub(r'\b1700s\b', '1970s', text)

    return text


def clean_syntax_garbage(text: str) -> str:
    """前後の引用符・コロン・中括弧・マークダウン・余計なプレフィックス等の構文ゴミを除去"""
    if not text:
        return text

    t = text.strip()
    # マークダウンコードブロック除去
    t = re.sub(r'```[\s\S]*?```', '', t)
    # エスケープされた引用符の解除
    t = t.replace('\\"', '"').replace("\\'", "'")
    
    # 先頭の不要な記号・空白
    t = re.sub(r'^[\s:`"\'\{\},]+', '', t)

    # 先頭の壊れたプロンプトゴミ（例: `1 1n 1 sentence, `, `1, "`, `1. `）
    t = re.sub(r'^(?:\d+\s+)*\d+\s*n\s*\d+\s*sentence[,:\s]*', '', t, flags=re.IGNORECASE)
    t = re.sub(r'^(?:\d+[,.]\s*)+["\']?', '', t)
    t = re.sub(r'^[A-Za-z]\s*["\'][A-Za-z][,\s]+', '', t)  # 例: S "S, Spoonful -> Spoonful

    # 末尾の構文ゴミ（例: `",`, `"}`, `"`, `'`, `',`, `.",` 等）
    t = re.sub(r'[\s:`"\'\{\},]+$', '', t)
    t = re.sub(r'\.{2,}$', '.', t)

    # 先頭の不要な記号・空白（再チェック）
    t = re.sub(r'^[\s:`"\'\{\},]+', '', t)

    # 年号の正規化
    t = normalize_year_text(t)

    return t.strip()


def check_ja_issues(text: str) -> list:
    """description_ja の問題点を判定"""
    if not text or not text.strip():
        return ["empty"]
    
    reasons = []
    has_hira = bool(HIRAGANA_REGEX.search(text))
    has_cjk = bool(CJK_REGEX.search(text))
    zh_syntax = ZH_SYNTAX_WORDS_REGEX.findall(text)
    sim_chars = [c for c in text if c in SIMPLIFIED_CHINESE_CHARS]

    # 1. ひらがなが存在せず漢字のみ -> 純中国語文
    if not has_hira and has_cjk:
        reasons.append("pure_chinese")
    # 2. 中国語構文・語彙が含まれる -> 日中混在文
    elif zh_syntax:
        reasons.append("mixed_chinese_syntax")
    # 3. 簡体字が複数含まれる
    elif len(sim_chars) >= 2:
        reasons.append("simplified_chinese_chars")

    # 4. LLMメタ発言
    if LLM_META_REGEX.search(text):
        reasons.append("llm_meta_junk")

    # 5. 文字化け・壊れたテキスト
    if '\ufffd' in text or len(text.strip()) < 10 or '' in text:
        reasons.append("mojibake_or_broken")

    return reasons


def check_en_issues(text: str) -> list:
    """description_en の問題点を判定"""
    if not text or not text.strip():
        return ["empty"]

    reasons = []
    has_cjk = bool(CJK_REGEX.search(text))
    has_hira = bool(HIRAGANA_REGEX.search(text))
    has_kata = bool(KATAKANA_REGEX.search(text))

    # 1. 日本語・中国語文字の混入
    if has_hira or has_kata or has_cjk:
        reasons.append("contains_cjk_or_japanese")

    # 2. LLMメタ発言
    if LLM_META_REGEX.search(text):
        reasons.append("llm_meta_junk")

    # 3. 文字化け・短すぎる壊れたテキスト・異常な繰り返し
    words = re.findall(r'[A-Za-z]{3,}', text)
    if '\ufffd' in text or '' in text or len(text.strip()) < 15 or len(words) < 4:
        reasons.append("malformed_or_broken")

    return reasons


def clean_syntax_all(db_path: str = DB_PATH, dry_run: bool = False, auto_confirm: bool = False) -> int:
    """データベース全体の構文ゴミ（クォート・コロン・年号）をルールベースで高速クリーンアップ"""
    if not os.path.exists(db_path):
        print(f"[Error] データベースファイルが見つかりません: {db_path}")
        return 0

    print(f"\n==================================================")
    print(f"  構文ゴミ・引用符・年号のルールベースクリーンアップ")
    print(f"  データベース: {db_path}")
    print(f"==================================================\n")

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT id, title, artist, album, release_year, description_ja, description_en FROM tracks;")
    rows = cur.fetchall()

    updates = []
    for row_id, title, artist, album, year, ja, en in rows:
        new_ja = clean_syntax_garbage(ja) if ja else ja
        new_en = clean_syntax_garbage(en) if en else en

        if new_ja != ja or new_en != en:
            updates.append({
                "id": row_id,
                "title": title or "",
                "artist": artist or "",
                "old_ja": ja,
                "new_ja": new_ja,
                "old_en": en,
                "new_en": new_en
            })

    if not updates:
        print("[Result] クリーンアップ対象の構文ゴミは見つかりませんでした。")
        conn.close()
        return 0

    print(f"[Result] 構文ゴミを含むレコードが {len(updates)} 件見つかりました。\n")
    print("--- サンプル (先頭 5 件) ---")
    for i, u in enumerate(updates[:5], 1):
        print(f"[{i}] ID {u['id']} [{u['artist']} - {u['title']}]")
        if u['old_ja'] != u['new_ja']:
            print(f"  [JA Old] {u['old_ja']}")
            print(f"  [JA New] {u['new_ja']}")
        if u['old_en'] != u['new_en']:
            print(f"  [EN Old] {u['old_en']}")
            print(f"  [EN New] {u['new_en']}")
        print()

    if dry_run:
        print(f"[Dry Run] --dry-run が指定されているため、DB更新はスキップしました。（対象: {len(updates)} 件）")
        conn.close()
        return len(updates)

    if not auto_confirm:
        ans = input(f"\n上記の {len(updates)} 件の構文クリーンアップをDBに反映しますか？ (y/N): ").strip().lower()
        if ans != 'y':
            print("[Canceled] 構文クリーンアップをキャンセルしました。")
            conn.close()
            return 0

    for u in updates:
        cur.execute("UPDATE tracks SET description_ja = ?, description_en = ? WHERE id = ?", (u["new_ja"], u["new_en"], u["id"]))

    conn.commit()
    conn.close()
    print(f"[Success] {len(updates)} 件のレコードの構文ゴミを正常にクリーンアップしました。\n")
    return len(updates)


def call_llm(messages: list, active_model: str, base_url: str = LEMONADE_BASE_URL, max_tokens: int = 150) -> str:
    """Lemonade Server に推論リクエストを送信 (stop sequence を設定して会話ループを遮断)"""
    payload = {
        "model": active_model,
        "messages": messages,
        "temperature": 0.1,
        "max_tokens": max_tokens,
        "stop": ["\nUser:", "\nAssistant:", "\nHuman:", "\n\n", "User:", "【", "Note:", "(Note:"]
    }
    req = urllib.request.Request(
        f"{base_url}/chat/completions",
        headers={"Content-Type": "application/json", "User-Agent": "fix-descriptions-script"},
        data=json.dumps(payload).encode('utf-8')
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        res_data = json.loads(resp.read().decode('utf-8'))
        return res_data["choices"][0]["message"]["content"].strip()


def extract_clean_text(raw_text: str, is_ja: bool = True) -> str:
    """LLM出力からメタ発言・前置き・会話ループを除去して純粋な1〜2文を抽出"""
    if not raw_text:
        return ""

    # User: や Note: 以降をカット
    cut_patterns = [
        r'\n\s*(?:User|Assistant|Human|Note|Option\s*\d|Alternatively|Let me know|If you|Here is|Here\'s)\b.*$',
        r'(?:User|Assistant|Human)\s*:.*$',
        r'【(?:厳格|楽曲|注意).*$',
        r'\(Note:.*?\)',
        r'\(注:.*?\)'
    ]
    t = raw_text
    for cp in cut_patterns:
        t = re.split(cp, t, flags=re.IGNORECASE | re.DOTALL)[0]

    # 前置きの除去 (例: "ここでは、〜を解説します。\n", "解説: ", "最終回答: ", "Here is...")
    prefix_patterns = [
        r'^(?:ここから(?:始まる|は)|ここでは|以下(?:は|の)|最終回答|日本語解説|楽曲解説|解説|紹介文|説明)\s*[:：\n]\s*',
        r'^(?:Here\s+(?:is|are)|Here\'s)\s+[^:\n]+[:：\n]\s*',
        r'^(?:Sure|Certainly)[!.,]?\s*',
    ]
    for pp in prefix_patterns:
        t = re.sub(pp, '', t, flags=re.IGNORECASE).strip()

    t = clean_syntax_garbage(t)
    return t


def fix_ja_descriptions(db_path: str = DB_PATH, limit: int = None, dry_run: bool = False, auto_confirm: bool = False, base_url: str = LEMONADE_BASE_URL):
    """中国語・日中混在・メタ発言・壊れた日本語解説文 (description_ja) をLLMで修復"""
    if not os.path.exists(db_path):
        print(f"[Error] データベースファイルが見つかりません: {db_path}")
        return False

    print(f"\n==================================================")
    print(f"  日本語解説文 (description_ja) の中国語・異常修復")
    print(f"  データベース: {db_path}")
    print(f"  LLM サーバー: {base_url}")
    print(f"==================================================\n")

    active_model = get_lemonade_model(base_url)
    print(f"[System] 使用モデル: {active_model}")

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT id, title, artist, album, release_year, description_ja, description_en FROM tracks;")
    rows = cur.fetchall()

    targets = []
    for row_id, title, artist, album, year, ja, en in rows:
        clean_ja = clean_syntax_garbage(ja) if ja else ja
        issues = check_ja_issues(clean_ja)
        if issues:
            targets.append({
                "id": row_id,
                "title": title or "",
                "artist": artist or "Unknown",
                "album": album or "",
                "year": year,
                "old_ja": ja or "",
                "clean_ja": clean_ja or "",
                "en": en or "",
                "issues": issues
            })

    if not targets:
        print("[Result] 修正が必要な日本語解説文は見つかりませんでした。すべて正常です。")
        conn.close()
        return True

    print(f"[Result] 修正対象の日本語解説文が {len(targets)} 件見つかりました。")
    if limit:
        targets = targets[:limit]
        print(f"  ※ --limit により先頭 {len(targets)} 件を処理対象とします。")

    if dry_run:
        print(f"\n[Dry Run] --dry-run モードのため、先頭 {min(5, len(targets))} 件の推論サンプルを表示します:")
        sample_targets = targets[:5]
    else:
        if not auto_confirm:
            print(f"\n{len(targets)} 件の日本語解説文を LLM で修復・更新しますか？")
            ans = input("実行する場合は 'y' を入力してください (y/N): ").strip().lower()
            if ans != 'y':
                print("[Canceled] 日本語解説文の修復をキャンセルしました。")
                conn.close()
                return True
        sample_targets = targets

    success_count = 0
    start_time = time.time()
    system_role = (
        "あなたは日本のFMラジオのプロ音楽アナウンサーです。"
        "与えられた楽曲情報から、中国語を一切排除し、100%自然で美しい日本語の楽曲紹介文（1〜2文）のみを出力してください。"
        "前置きや挨拶、注釈は絶対に含めず、紹介文本文だけを出力してください。"
    )

    for idx, t in enumerate(sample_targets, 1):
        print(f"\n[{idx}/{len(sample_targets)}] JA修復中: ID {t['id']} [{t['artist']} - {t['title']}]")
        print(f"  [Issues] {', '.join(t['issues'])}")
        print(f"  [OLD JA] {t['old_ja']}")

        prompt = f"""以下の楽曲の日本語紹介文（1〜2文）を作成してください。

【厳格ルール】
・中国語の文字（簡体字等）や中国語構文（这首, 由…创作, 展现了, 融合了, 充满了 等）は完全排除し、綺麗な日本語のみを使うこと。
・4桁西暦（例: 1973年、1960年代）を正しく使うこと。
・前置きや解説ラベル、Markdownは出力せず、本文（1〜2文）のみを出力すること。

曲名: {t['title']}
アーティスト: {t['artist']}
アルバム: {t['album']}
リリース年: {t['year']}
既存テキスト(参考): {t['clean_ja']}"""

        messages = [
            {"role": "system", "content": system_role},
            {"role": "user", "content": "曲名: Hotel California\nアーティスト: Eagles\nアルバム: Hotel California\nリリース年: 1976\n既存テキスト: 「ホテルカリフォルニア」は、老鹰乐队的经典作品。"},
            {"role": "assistant", "content": "1976年にリリースされたEaglesの代表曲「Hotel California」は、哀愁漂うツインギターと深みのあるメロディーが心に響く、アメリカン・ロック不朽の名曲です。"},
            {"role": "user", "content": prompt}
        ]

        try:
            raw_new_ja = call_llm(messages, active_model, base_url, max_tokens=120)
            clean_new_ja = extract_clean_text(raw_new_ja, is_ja=True)

            print(f"  [NEW JA] {clean_new_ja}")

            if not dry_run:
                cur.execute("UPDATE tracks SET description_ja = ? WHERE id = ?", (clean_new_ja, t["id"]))
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
    print(f"\n[Success] {success_count}/{len(sample_targets)} 件の日本語解説文の修復が{status_label}しました ({elapsed:.1f}秒)。\n")
    return True


def fix_en_descriptions(db_path: str = DB_PATH, limit: int = None, dry_run: bool = False, auto_confirm: bool = False, base_url: str = LEMONADE_BASE_URL):
    """日本語/中国語文字混入・LLMメタ発言・壊れた英語解説文 (description_en) をLLMで修復"""
    if not os.path.exists(db_path):
        print(f"[Error] データベースファイルが見つかりません: {db_path}")
        return False

    print(f"\n==================================================")
    print(f"  英語解説文 (description_en) の日中文字混入・メタ発言修復")
    print(f"  データベース: {db_path}")
    print(f"  LLM サーバー: {base_url}")
    print(f"==================================================\n")

    active_model = get_lemonade_model(base_url)
    print(f"[System] 使用モデル: {active_model}")

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT id, title, artist, album, release_year, description_ja, description_en FROM tracks;")
    rows = cur.fetchall()

    targets = []
    for row_id, title, artist, album, year, ja, en in rows:
        clean_en = clean_syntax_garbage(en) if en else en
        issues = check_en_issues(clean_en)
        if issues:
            targets.append({
                "id": row_id,
                "title": title or "",
                "artist": artist or "Unknown",
                "album": album or "",
                "year": year,
                "ja": ja or "",
                "old_en": en or "",
                "clean_en": clean_en or "",
                "issues": issues
            })

    if not targets:
        print("[Result] 修正が必要な英語解説文は見つかりませんでした。すべてクリーンです。")
        conn.close()
        return True

    print(f"[Result] 修正対象の英語解説文が {len(targets)} 件見つかりました。")
    if limit:
        targets = targets[:limit]
        print(f"  ※ --limit により先頭 {len(targets)} 件を処理対象とします。")

    if dry_run:
        print(f"\n[Dry Run] --dry-run モードのため、先頭 {min(5, len(targets))} 件の推論サンプルを表示します:")
        sample_targets = targets[:5]
    else:
        if not auto_confirm:
            print(f"\n{len(targets)} 件の英語解説文を LLM で修復・更新しますか？")
            ans = input("実行する場合は 'y' を入力してください (y/N): ").strip().lower()
            if ans != 'y':
                print("[Canceled] 英語解説文の修復をキャンセルしました。")
                conn.close()
                return True
        sample_targets = targets

    success_count = 0
    start_time = time.time()
    system_role = (
        "You are an expert English music announcer. "
        "You output strictly 1-2 sentences of natural, engaging English description "
        "using Latin/Romaji alphabet only, with NO notes, NO conversational filler, and NO markdown."
    )

    for idx, t in enumerate(sample_targets, 1):
        print(f"\n[{idx}/{len(sample_targets)}] EN修復中: ID {t['id']} [{t['artist']} - {t['title']}]")
        print(f"  [Issues] {', '.join(t['issues'])}")
        print(f"  [OLD EN] {t['old_en']}")

        prompt = f"""Please provide a 1-2 sentence natural English description for this song.

【Rules】
- Latin/Romaji characters only (NO Kanji, Hiragana, Katakana, or Chinese characters).
- 4-digit years only (e.g. 1976, 1970s).
- Output ONLY the 1-2 sentence description text without any labels, introductions, or markdown.

Title: {t['title']}
Artist: {t['artist']}
Album: {t['album']}
Release Year: {t['year']}
Current Text: {t['clean_en']}
Japanese Context: {t['ja']}"""

        messages = [
            {"role": "system", "content": system_role},
            {"role": "user", "content": "Title: Hotel California\nArtist: Eagles\nAlbum: Hotel California\nRelease Year: 1976\nCurrent Text: Hotel California is 老鹰乐队's famous song.\nJapanese Context: 「ホテルカリフォルニア」はEaglesの名曲。"},
            {"role": "assistant", "content": "Released in 1976, Eagles' iconic rock classic \"Hotel California\" features unforgettable twin-guitar harmonies and melancholic storytelling that continues to captivate listeners worldwide."},
            {"role": "user", "content": prompt}
        ]

        try:
            raw_new_en = call_llm(messages, active_model, base_url, max_tokens=120)
            clean_new_en = extract_clean_text(raw_new_en, is_ja=False)

            print(f"  [NEW EN] {clean_new_en}")

            if not dry_run:
                cur.execute("UPDATE tracks SET description_en = ? WHERE id = ?", (clean_new_en, t["id"]))
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


def run_all(db_path: str = DB_PATH, limit: int = None, dry_run: bool = False, auto_confirm: bool = False, base_url: str = LEMONADE_BASE_URL):
    """構文クリーンアップ、日本語解説修復、英語解説修復を一括実行"""
    print(f"\n==================================================")
    print(f"  全解説文 (JA / EN) の総合修復を一括実行")
    print(f"==================================================")

    # 1. 構文ゴミのクリーンアップ
    clean_syntax_all(db_path=db_path, dry_run=dry_run, auto_confirm=auto_confirm)

    # 2. 日本語解説文の修復
    fix_ja_descriptions(db_path=db_path, limit=limit, dry_run=dry_run, auto_confirm=auto_confirm, base_url=base_url)

    # 3. 英語解説文の修復
    fix_en_descriptions(db_path=db_path, limit=limit, dry_run=dry_run, auto_confirm=auto_confirm, base_url=base_url)

    print(f"\n[Complete] すべての解説文修復処理が完了しました。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="SQLiteデータベース (music_meta.db) の楽曲解説文 (description_ja / description_en) 修復ツール"
    )
    parser.add_argument("--db", default=DB_PATH, help=f"SQLiteデータベースのパス (デフォルト: {DB_PATH})")
    parser.add_argument("--base-url", default=LEMONADE_BASE_URL, help=f"Lemonade Server のURL (デフォルト: {LEMONADE_BASE_URL})")
    
    # 実行モード
    mode_group = parser.add_argument_group("実行モード")
    mode_group.add_argument("--all", action="store_true", help="構文クリーンアップ、日本語解説修復、英語解説修復をすべて実行")
    mode_group.add_argument("--clean-syntax-only", action="store_true", help="LLMを呼び出さず、構文ゴミ・引用符・年号のルールベースクリーンアップのみ実行")
    mode_group.add_argument("--ja", action="store_true", help="日本語解説文 (description_ja) の中国語・異常のみを修復")
    mode_group.add_argument("--en", action="store_true", help="英語解説文 (description_en) の日中文字混入・メタ発言のみを修復")
    
    # オプション
    parser.add_argument("--limit", type=int, default=None, help="修復処理の最大件数（テスト・検証用）")
    parser.add_argument("--dry-run", action="store_true", help="データベースを変更せず、対象と修正内容のプレビューのみを表示")
    parser.add_argument("-y", "--yes", action="store_true", help="確認プロンプトをスキップして即座に実行")

    args = parser.parse_args()

    try:
        if args.all:
            run_all(
                db_path=args.db,
                limit=args.limit,
                dry_run=args.dry_run,
                auto_confirm=args.yes,
                base_url=args.base_url
            )
        elif args.clean_syntax_only:
            clean_syntax_all(
                db_path=args.db,
                dry_run=args.dry_run,
                auto_confirm=args.yes
            )
        elif args.ja:
            fix_ja_descriptions(
                db_path=args.db,
                limit=args.limit,
                dry_run=args.dry_run,
                auto_confirm=args.yes,
                base_url=args.base_url
            )
        elif args.en:
            fix_en_descriptions(
                db_path=args.db,
                limit=args.limit,
                dry_run=args.dry_run,
                auto_confirm=args.yes,
                base_url=args.base_url
            )
        else:
            print("\n=== 楽曲解説文修復メニュー ===")
            print("1. 構文ゴミ・引用符の高速クリーンアップ (--clean-syntax-only)")
            print("2. 日本語解説文 (description_ja) の中国語・異常修復 (--ja)")
            print("3. 英語解説文 (description_en) の日中文字・メタ発言修復 (--en)")
            print("4. すべて実行 (--all)")
            print("0. 終了")
            
            choice = input("\n実行する番号を入力してください (1-4, 0): ").strip()
            if choice == "1":
                clean_syntax_all(db_path=args.db, dry_run=args.dry_run, auto_confirm=args.yes)
            elif choice == "2":
                fix_ja_descriptions(db_path=args.db, limit=args.limit, dry_run=args.dry_run, auto_confirm=args.yes, base_url=args.base_url)
            elif choice == "3":
                fix_en_descriptions(db_path=args.db, limit=args.limit, dry_run=args.dry_run, auto_confirm=args.yes, base_url=args.base_url)
            elif choice == "4":
                run_all(db_path=args.db, limit=args.limit, dry_run=args.dry_run, auto_confirm=args.yes, base_url=args.base_url)
            else:
                print("[Info] 処理を終了しました。")

    except KeyboardInterrupt:
        print("\n[System] ユーザーによって処理が中断されました。")
        sys.exit(0)
