# music_meta.db データベース仕様書

本書は、音楽ライブラリメタデータデータベース（`music_meta.db`）の設計および仕様をまとめたドキュメントです。  
**Jetson Orin Nano Super** などのエッジデバイスや組み込み環境上で動作する音楽再生・検索・推薦・自然言語検索（NL-to-SQL / AIアシスタント）アプリケーションの開発に利用されることを想定しています。

---

## 1. データベース概要

| 項目 | 内容 |
| :--- | :--- |
| **データベース名** | `music_meta.db` |
| **DBMS** | SQLite 3 |
| **文字エンコーディング** | UTF-8 |
| **主用途** | 音楽ファイルのメタデータ管理、曲検索、AIによる楽曲推薦、自然言語クエリ |
| **主な特徴** | タグ情報に加え、ローカルLLMによって生成された「楽曲区分」「気分/ムード」「エネルギーレベル」「日本語解説文」を格納 |

---

## 2. テーブル仕様

データベースには楽曲メタデータを管理する **`tracks`** テーブルが定義されています。

### `tracks` テーブル定義

| カラム名 | データ型 | NULL | 初期値 | 制約 | 説明 |
| :--- | :--- | :---: | :--- | :--- | :--- |
| `id` | `INTEGER` | NO | - | **PK (AUTOINCREMENT)** | レコード固有の一意識別子 |
| `file_path` | `TEXT` | NO | - | **UNIQUE** | 登録時の音源ファイルの絶対パス |
| `relative_path` | `TEXT` | YES | `NULL` | - | 音楽ルートディレクトリからの相対パス（区切り文字: `/`） |
| `file_format` | `TEXT` | YES | `NULL` | - | ファイル拡張子・形式（`flac`, `mp3` 等） |
| `is_hires` | `INTEGER` | YES | `0` | - | ハイレゾ音源フラグ（`1`: ハイレゾ, `0`: 通常音源） |
| `sample_rate` | `INTEGER` | YES | `NULL` | - | サンプリング周波数（Hz、例: `44100`, `48000`, `96000`, `192000`） |
| `bit_depth` | `INTEGER` | YES | `NULL` | - | 量子化ビット数（bit、例: `16`, `24`） |
| `duration_seconds` | `INTEGER` | YES | `NULL` | - | 再生時間（秒数）。※20分（1200秒）以上の音源は除外 |
| `title` | `TEXT` | YES | `NULL` | - | 楽曲タイトル |
| `artist` | `TEXT` | YES | `Unknown` | - | アーティスト名 / 演奏グループ名 |
| `album` | `TEXT` | YES | `NULL` | - | アルバム名 |
| `release_year` | `INTEGER` | YES | `NULL` | - | リリース年（西暦4桁、例: `1982`, `2023`） |
| `music_category` | `TEXT` | YES | `その他` | - | 楽曲区分（`邦楽` / `洋楽` / `その他`） |
| `genre` | `TEXT` | YES | `その他` | - | ジャンル（固定リストより抽出。複数該当時はカンマ区切り） |
| `mood` | `TEXT` | YES | `NULL` | - | 曲のムード・雰囲気キーワード（カンマ区切り、例: `Upbeat, Energetic`） |
| `energy_level` | `INTEGER` | YES | `3` | `1 <= energy_level <= 5` | エネルギーレベル（1: 静か・穏やか 〜 5: 非常に激しい・高揚） |
| `composer` | `TEXT` | YES | `NULL` | - | 作曲者名 |
| `performers` | `TEXT` | YES | `NULL` | - | 演奏者・客演・参加メンバー名 |
| `description_ja` | `TEXT` | YES | `NULL` | - | **楽曲の概要・歴史・エピソード解説（日本語、1〜2文）** |
| `description_en` | `TEXT` | YES | `NULL` | - | **楽曲の概要・歴史・エピソード解説（英語、1〜2文）** |
| `analyzed_at` | `TIMESTAMP` | YES | `CURRENT_TIMESTAMP` | - | メタデータ解析・登録日時 |

---

## 3. カラム詳細仕様・値のルール

### 3.1 `music_category`（楽曲区分）
以下のいずれかの固定値が設定されます。
- `邦楽`: 日本のアーティスト、J-POP、歌謡曲、日本のインストなど
- `洋楽`: 海外のアーティスト、洋楽ロック、海外ジャズ、クラシックなど
- `その他`: 国籍不明または分類困難なもの

### 3.2 `genre`（ジャンル）
以下のジャンル定義から、1つまたは複数（カンマ区切り、例: `ロック, ポップ`）が設定されます。
- `ジャズ`
- `ロック`
- `ポップ`
- `クラシック`
- `R&B・ソウル`
- `ブルース`
- `エレクトロニック`
- `フォーク・カントリー`
- `ヒップホップ`
- `サウンドトラック・インスト`
- `その他`

### 3.3 `is_hires`（ハイレゾ判定）
音源ファイル形式が可逆圧縮/非圧縮（`flac`, `wav`, `alac`, `aiff`, `dsd` 等）であり、かつ以下のいずれかの条件を満たす場合に `1` となります。
- `sample_rate > 48000`（48kHz超: 88.2kHz, 96kHz, 192kHz 等）
- `bit_depth > 16`（16bit超: 24bit, 32bit 等）

### 3.4 `energy_level`（エネルギーレベル）
楽曲の勢いやテンポ感を 1 〜 5 の 5段階で表現します。
- `1`: 非常に穏やか、アンビエント、静かなクラシック・ピアノソロ、睡眠用
- `2`: ゆったりしたバラード、落ち着いたアコースティック、スロージャズ
- `3`: 標準的なミディアムテンポのポップス、日常リスニング向け
- `4`: アップテンポでノリの良いロック・ダンスミュージック、ドライブ向け
- `5`: 非常に激しいハードロック、ハイテンポなEDM、アグレッシブな楽曲

### 3.5 `description_ja` / `description_en`（日本語解説文 / 英語解説文）
Web検索結果および音源タグ情報を元に、ローカルLLMが生成した解説文（1〜2文）です。楽曲の背景、代表曲としての位置づけ、特徴などが記述されており、検索UIでの表示やAIによる楽曲紹介、英語DJモード、RAG（Retrieval-Augmented Generation）に活用できます。
- **`description_ja`**: 日本語による解説文（1〜2文）
- **`description_en`**: 英語による解説文（1〜2文、FMラジオDJアナウンス等に直接利用可能）

---

## 4. Jetson Orin Nano Super での開発・活用ガイド

### 4.1 パス解決（ストレージのマウント対応）
データベース内の `file_path` は登録時の絶対パスですが、Jetson側でNASや外付けSSDをマウントするパスが変わる場合は、**`relative_path`** を使用して動的にパスを結合してください。

```python
import os
from pathlib import Path

# Jetson Orin 上での音源マウント先
JETSON_MUSIC_MOUNT = "/mnt/nas_music"

def get_playable_path(relative_path: str) -> str:
    """Jetson側のローカルパスに解決"""
    return os.path.normpath(os.path.join(JETSON_MUSIC_MOUNT, relative_path))
```

---

### 4.2 推奨インデックス作成（検索高速化）
Jetson Orin Nano のメモリとCPUを節約し、高速な検索・フィルタリングを実現するために、以下のインデックスを作成することを推奨します。

```sql
-- アーティスト・タイトル検索用
CREATE INDEX IF NOT EXISTS idx_tracks_artist ON tracks(artist);
CREATE INDEX IF NOT EXISTS idx_tracks_title ON tracks(title);

-- ジャンル・区分・気分・エネルギー検索用
CREATE INDEX IF NOT EXISTS idx_tracks_genre ON tracks(genre);
CREATE INDEX IF NOT EXISTS idx_tracks_category ON tracks(music_category);
CREATE INDEX IF NOT EXISTS idx_tracks_energy ON tracks(energy_level);
CREATE INDEX IF NOT EXISTS idx_tracks_hires ON tracks(is_hires);
CREATE INDEX IF NOT EXISTS idx_tracks_relative_path ON tracks(relative_path);
```

---

### 4.3 実践的 SQL クエリ例

#### ① 気分やエネルギーに合わせたプレイリスト生成（例: ドライブ用アップテンポ）
```sql
SELECT id, title, artist, album, energy_level, mood, relative_path
FROM tracks
WHERE energy_level >= 4
  AND (genre LIKE '%ロック%' OR genre LIKE '%ポップ%' OR genre LIKE '%エレクトロニック%')
ORDER BY RANDOM()
LIMIT 20;
```

#### ② リラックス用・就寝前（穏やかな曲）
```sql
SELECT id, title, artist, album, energy_level, relative_path
FROM tracks
WHERE energy_level <= 2
  AND (mood LIKE '%Calm%' OR mood LIKE '%Relax%' OR genre LIKE '%ジャズ%' OR genre LIKE '%クラシック%')
ORDER BY RANDOM()
LIMIT 15;
```

#### ③ ハイレゾ音源のみを抽出
```sql
SELECT id, title, artist, sample_rate, bit_depth, file_format, relative_path
FROM tracks
WHERE is_hires = 1
ORDER BY artist, album, id;
```

#### ④ 70年代・80年代の邦楽名曲を検索
```sql
SELECT id, title, artist, release_year, description_ja, relative_path
FROM tracks
WHERE music_category = '邦楽'
  AND release_year BETWEEN 1970 AND 1989
ORDER BY release_year ASC;
```

#### ⑤ 全文あいまい検索（キーワードから曲・解説文を横断検索）
```sql
SELECT id, title, artist, album, genre, description_ja, description_en, relative_path
FROM tracks
WHERE title LIKE '%キーワード%'
   OR artist LIKE '%キーワード%'
   OR album LIKE '%キーワード%'
   OR description_ja LIKE '%キーワード%'
   OR description_en LIKE '%キーワード%'
LIMIT 10;
```

---

### 4.4 LLM / Text-to-SQL（自然言語検索）用プロンプト定義

Jetson 上でローカルLLM（Ollama, vLLM, NanoLLM 等）を使って「ユーザーの自然言語の要望からSQLを生成する」場合のシステムプロンプト定義例です。

```text
You are an expert SQL assistant for a music database.
Generate only a valid SQLite SELECT query based on user request.

Table: tracks
Columns:
- id (INTEGER): Primary Key
- title (TEXT): Track title
- artist (TEXT): Artist or band name
- album (TEXT): Album name
- release_year (INTEGER): 4-digit release year
- music_category (TEXT): '邦楽', '洋楽', or 'その他'
- genre (TEXT): 'ジャズ', 'ロック', 'ポップ', 'クラシック', 'R&B・ソウル', 'ブルース', 'エレクトロニック', 'フォーク・カントリー', 'ヒップホップ', 'サウンドトラック・インスト', 'その他' (supports LIKE '%genre%')
- mood (TEXT): Mood keywords (e.g. Calm, Energetic, Upbeat, Melancholic, Relaxing)
- energy_level (INTEGER): 1 (quiet) to 5 (very energetic)
- is_hires (INTEGER): 1 for Hi-Res audio, 0 for standard
- duration_seconds (INTEGER): Track length in seconds
- composer (TEXT): Composer name
- performers (TEXT): Performers/musicians
- description_ja (TEXT): Japanese background and introduction
- description_en (TEXT): English background and introduction
- relative_path (TEXT): Relative file path to music file
```

---

### 4.5 Python による基本アクセスコード例 (Jetson 向け)

```python
import sqlite3
from typing import List, Dict, Any

class MusicDatabase:
    def __init__(self, db_path: str = "music_meta.db"):
        self.db_path = db_path

    def get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row  # カラム名でアクセス可能にする
        return conn

    def query(self, sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(sql, params)
            return [dict(row) for row in cur.fetchall()]

# 使用例
if __name__ == "__main__":
    db = MusicDatabase("music_meta.db")
    
    # ハイレゾのJAZZ曲を5件ランダム取得
    results = db.query("""
        SELECT title, artist, album, sample_rate, bit_depth, relative_path
        FROM tracks
        WHERE is_hires = 1 AND genre LIKE '%ジャズ%'
        ORDER BY RANDOM()
        LIMIT 5;
    """)
    
    for r in results:
        print(f"[{r['artist']}] {r['title']} ({r['sample_rate']}Hz/{r['bit_depth']}bit)")
```
