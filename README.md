# Audio SQL - 音楽ライブラリ メタデータ収集・DB構築ツール

NASなどのストレージに保存された楽曲ファイル（MP3 / FLAC）からタグ情報（ID3 / Vorbis）を抽出し、Web検索とLLM（Lemonade Server）を活用してリッチなメタデータ（ジャンル、ムード、エネルギーレベル、日本語の楽曲説明など）を生成して SQLite データベースに蓄積するツールです。

---

## 主な機能

- **タグ・音源情報自動抽出**: MP3 / FLAC ファイルからタイトル、アーティスト、アルバム、リリース年に加え、ファイル形式 (`file_format`)、サンプリングレート (`sample_rate`)、ビット深度 (`bit_depth`) を抽出
- **ハイレゾ自動判定 (`is_hires`)**: ロスレス音源でサンプリングレートが 48kHz 超または量子化ビット数が 16bit 超（24bit等）のハイレゾ音源を自動判定
- **Web検索による情報補完**: DuckDuckGo 検索で楽曲の背景情報やレビューを自動取得
- **LLMによるメタデータ構造化**: Lemonade Server 経由で以下の情報を自動生成
  - 楽曲区分 (`MUSIC_CATEGORY`: `邦楽` / `洋楽` / `その他`)
  - ジャンル (`GENRE`: 下記固定ジャンルから単一または複数カンマ区切りで抽出)
    - ジャズ / ロック / ポップ / クラシック / R&B・ソウル / ブルース / エレクトロニック / フォーク・カントリー / ヒップホップ / サウンドトラック・インスト / その他
  - 気分・ムード (`MOOD`)
  - エネルギーレベル (`ENERGY_LEVEL`: 1〜5)
  - 作曲者 (`COMPOSER`)
  - 演奏者・参加アーティスト (`PERFORMERS`)
  - リリース年 (`RELEASE_YEAR`)
  - **楽曲の概要・背景説明 (`DESCRIPTION` / 日本語)**
- **差分更新・リセット対応**: 登録済みファイルをスキップする差分更新と、最初からやり直す全件リセットを切り替え可能

---

## 必要環境

- Python 3.10 以上
- [Lemonade Server](http://localhost:13305) がローカルで稼働していること

### 依存パッケージ

```bash
pip install mutagen openai duckduckgo-search
```

---

## 設定

`build_music_db.py` 内の設定変数を必要に応じて変更してください。

| 変数名 | デフォルト値 | 説明 |
| :--- | :--- | :--- |
| `NAS_MUSIC_DIR` | `r"\\homenas\music"` | 音楽ファイルが保存されているディレクトリのパス |
| `DB_PATH` | `"music_meta.db"` | 保存先 SQLite データベースファイルのパス |
| `LEMONADE_BASE_URL` | `"http://localhost:13305/v1"` | Lemonade Server の API エンドポイント |

---

## 使い方

### 1. 動作確認（テスト実行）

デフォルトでは最初の **3曲** のみを対象にテスト実行します。

```bash
python build_music_db.py
```

### 2. 曲数を指定して実行

処理する新規曲数を指定して実行します（例: 10曲）。

```bash
python build_music_db.py --limit 10
```

### 3. 全曲をスキャンして差分更新（登録済みはスキップ）

`--limit 0` を指定すると、ライブラリ全体の未登録曲をすべて処理します。

```bash
python build_music_db.py --limit 0
```

### 4. データベースを初期化して全件再構築

既存のテーブルを削除し、全曲最初から解析し直す場合は `--reset` を付与します。

```bash
# 全件最初からやり直す
python build_music_db.py --limit 0 --reset

# 最初の5曲だけでリセットしてテスト
python build_music_db.py --limit 5 --reset
```

### 5. FLAC音源のみを対象にして実行

`--flac-only`（または `--format flac`）を指定すると、FLACファイルのみを対象にメタデータ収集・登録を行います。

```bash
# FLAC音源のみを対象にテスト実行 (3曲)
python build_music_db.py --flac-only

# FLAC音源のみを全件スキャンして差分更新
python build_music_db.py --flac-only --limit 0

# FLAC音源のみでDBを初期化して再構築
python build_music_db.py --flac-only --limit 0 --reset
```

---

## コマンドライン引数一覧

| 引数 | 型 / 選択肢 | デフォルト値 | 説明 |
| :--- | :--- | :--- | :--- |
| `--limit` | 整数 (`int`) | `3` | 処理する曲数の上限（`0` を指定すると無制限） |
| `--reset` | フラグ | - | 既存の DB データを削除して最初から作り直す |
| `--mode` | `update` / `reset` | `None` | 動作モード指定（`reset` は `--reset` と同等） |
| `--flac-only` | フラグ | - | **FLAC音源のみを対象にする** (`--format flac` と同等) |
| `--format` | `all` / `flac` / `mp3` | `all` | 対象ファイル形式の指定 (`all`: 全形式, `flac`: FLACのみ, `mp3`: MP3のみ) |

---

## データベース構造 (`tracks` テーブル)

| カラム名 | 型 | 説明 |
| :--- | :--- | :--- |
| `id` | `INTEGER` | 主キー (AUTOINCREMENT) |
| `file_path` | `TEXT` | ファイルの絶対パス (UNIQUE) |
| `relative_path` | `TEXT` | `NAS_MUSIC_DIR` からの相対パス |
| `file_format` | `TEXT` | ファイル形式 (`mp3`, `flac` 等) |
| `is_hires` | `INTEGER` | ハイレゾ音源フラグ (`1`: ハイレゾ, `0`: 通常音源) |
| `sample_rate` | `INTEGER` | サンプリング周波数 (Hz, 例: `44100`, `96000`) |
| `bit_depth` | `INTEGER` | 量子化ビット数 (bit, 例: `16`, `24`) |
| `title` | `TEXT` | 楽曲タイトル |
| `artist` | `TEXT` | アーティスト名 |
| `album` | `TEXT` | アルバム名 |
| `release_year` | `INTEGER` | リリース年 |
| `music_category` | `TEXT` | 楽曲区分 (`邦楽`, `洋楽`, `その他`) |
| `genre` | `TEXT` | 固定ジャンル (複数該当時はカンマ区切り。例: `ロック, ポップ`) |
| `mood` | `TEXT` | ムード・雰囲気キーワード (カンマ区切り) |
| `energy_level` | `INTEGER` | エネルギーレベル (1: 静か 〜 5: 非常にエネルギッシュ) |
| `composer` | `TEXT` | 作曲者 |
| `performers` | `TEXT` | 演奏者・参加アーティスト |
| `description` | `TEXT` | **楽曲の背景・解説文（日本語）** |
| `analyzed_at` | `TIMESTAMP` | 解析・登録日時 |
