# Audio SQL - 音楽ライブラリ メタデータ収集・DB構築ツール

NASなどのストレージに保存された楽曲ファイル（MP3 / FLAC）からタグ情報（ID3 / Vorbis）を抽出し、Web検索とLLM（Lemonade Server）を活用してリッチなメタデータ（ジャンル、ムード、エネルギーレベル、日本語の楽曲説明など）を生成して SQLite データベースに蓄積するツールです。

> 📖 **関連仕様書ドキュメント**:
> - データベース設計・SQL活用例: [doc/DB_SPEC.md](doc/DB_SPEC.md)
> - NAS音楽ジャンル洗い出し仕様書: [doc/SCAN_GENRES_SPEC.md](doc/SCAN_GENRES_SPEC.md)
> - 楽曲解説文修復ツール仕様書: [doc/FIX_DESCRIPTIONS_SPEC.md](doc/FIX_DESCRIPTIONS_SPEC.md)
> - 音声＆Web AIアシスタント仕様書: [doc/VOICE_BOT_SPEC.md](doc/VOICE_BOT_SPEC.md)

---

## 主な機能

- **タグ・音源情報自動抽出**: MP3 / FLAC ファイルからタイトル、アーティスト、アルバム、リリース年に加え、ファイル形式 (`file_format`)、サンプリングレート (`sample_rate`)、ビット深度 (`bit_depth`)、再生時間 (`duration_seconds`) を抽出
- **ハイレゾ自動判定 (`is_hires`)**: ロスレス音源でサンプリングレートが 48kHz 超または量子化ビット数が 16bit 超（24bit等）のハイレゾ音源を自動判定
- **長尺音源（20分以上）の自動除外**: DVD音声取り出し等の 20分（1200秒）以上の長尺ファイルは LLM 解析・DB 登録対象から自動除外（既存 DB 内の長尺音源も起動時にクリーンアップ）
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
  - **楽曲の概要・背景説明 (`DESCRIPTION_JA` / 日本語解説、`DESCRIPTION_EN` / 英語解説)**
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

## NAS音源ジャンル洗い出し・集計ツール (`scan_genres.py`)

NAS（`\\homenas\music`）やローカルフォルダ内の音源ファイル（MP3, FLAC, WMA, M4A, OGG, WAV 等）からメタデータ（ジャンルタグ）を高速にスキャン・抽出し、ジャンル別の楽曲数・割合・代表曲の集計やレポート生成を行うツールです。

```bash
# 1. NAS全体をスキャンしてターミナルにジャンルランキングを表示
python scan_genres.py

# 2. テスト実行（最初の100曲のみスキャン）
python scan_genres.py --limit 100

# 3. MarkdownレポートおよびCSVファイルを自動生成
python scan_genres.py --md genre_report.md --csv genre_summary.csv

# 4. FLAC音源のみを対象に集計し、既存DB (music_meta.db) と対比
python scan_genres.py --flac-only --compare-db

# 5. ジャンル未設定（タグなし）の音源ファイルを一覧出力
python scan_genres.py --export-untagged untagged_tracks.txt

# 6. 詳細なトラック情報と集計結果をJSON出力
python scan_genres.py --json genre_details.json
```

---

## メタデータクリーンアップ・修復ツール (`cleanup_long_tracks.py` / `fix_descriptions.py`)

### 1. 楽曲解説文（JA/EN）の中国語・メタ発言・構文ゴミ修復 (`fix_descriptions.py`)
`description_ja` 内の中国語混入（純中国語文、日中混在構文、簡体字）や、`description_en` 内のLLMメタ発言（`Here's...`, `(Note:...)`）、構文ゴミ（`",`, `: " "`）を高精度に検出・修復する専用ツールです。

```bash
# 1. 構文ゴミ・年号のルールベースクリーンアップを高速実行（LLM不使用）
python fix_descriptions.py --clean-syntax-only -y

# 2. 日本語解説文 (description_ja) の中国語・異常をLLMで修復（テストプレビュー）
python fix_descriptions.py --ja --limit 5 --dry-run

# 3. 日本語解説文 (description_ja) の全件修復
python fix_descriptions.py --ja -y

# 4. 英語解説文 (description_en) の日中文字・メタ発言の全件修復
python fix_descriptions.py --en -y

# 5. 全体一括修復（構文クリーンアップ + JA修復 + EN修復）
python fix_descriptions.py --all -y
```

### 2. 長尺音源・年号クリーンアップ (`cleanup_long_tracks.py`)

長尺音源の削除に加えて、年号の異常修復や英語解説文のクリーンアップを行えるメンテナンスツールです。

```bash
# 1. すべてのクリーンアップを一括実行（長尺音源削除・年号修復・英語解説文修復）
python cleanup_long_tracks.py --all

# 2. 年号の異常（11967, 11960s, 1 960s, 160s, 1 177 等の5桁/3桁/スペース混入）のみを検出・修復
python cleanup_long_tracks.py --fix-years

# 3. 英語解説文 (description_en) に混入した日本語やゴミテキストをLLMで修復
python cleanup_long_tracks.py --fix-en-descriptions

# 4. 長尺音源（20分以上）のみを検出・削除
python cleanup_long_tracks.py --cleanup-duration

# 5. 事前に変更内容をプレビュー（DB更新なし）
python cleanup_long_tracks.py --all --dry-run
python cleanup_long_tracks.py --fix-years --dry-run
python cleanup_long_tracks.py --fix-en-descriptions --dry-run

# 6. 確認プロンプトをスキップして即座に実行
python cleanup_long_tracks.py --all -y
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
| `duration_seconds` | `INTEGER` | 再生時間（秒）※ 20分（1200秒）以上は登録除外 |
| `title` | `TEXT` | 楽曲タイトル |
| `artist` | `TEXT` | アーティスト名 |
| `title_en` | `TEXT` | **英語曲名・ローマ字表記（英語DJアナウンス用）** |
| `artist_en` | `TEXT` | **英語アーティスト名・ローマ字表記（英語DJアナウンス用）** |
| `album` | `TEXT` | アルバム名 |
| `release_year` | `INTEGER` | リリース年 |
| `music_category` | `TEXT` | 楽曲区分 (`邦楽`, `洋楽`, `その他`) |
| `genre` | `TEXT` | 固定ジャンル (複数該当時はカンマ区切り。例: `ロック, ポップ`) |
| `mood` | `TEXT` | ムード・雰囲気キーワード (カンマ区切り) |
| `energy_level` | `INTEGER` | エネルギーレベル (1: 静か 〜 5: 非常にエネルギッシュ) |
| `composer` | `TEXT` | 作曲者 |
| `performers` | `TEXT` | 演奏者・参加アーティスト |
| `description_ja` | `TEXT` | **楽曲の背景・解説文（日本語）** |
| `description_en` | `TEXT` | **楽曲の背景・解説文（英語、FMラジオDJアナウンス用）** |
| `analyzed_at` | `TIMESTAMP` | 解析・登録日時 |

---

## moOde 音声 & Web Chat AI システム (`voice_bot/`)

Jetson Orin Nano Super 上で動作し、**マイクによる音声入力** と **Webブラウザ（スマホ・PC）からのチャット入力** の双方から Raspberry Pi 5 上の moOde audio (MPD) をシームレスに操作・音楽再生できるAIアシスタントです。  
> 📖 **詳細なシステム仕様・API・プロトコル定義**: [VOICE_BOT_SPEC.md](doc/VOICE_BOT_SPEC.md) をご覧ください。

### 🌟 特徴
- **ハイブリッド操作**: マイクに向かって「ヘイ、マスター、Jazzをかけて」と話しかけても、ブラウザのチャット欄に入力しても即座に反応
- **リアルタイム双方向同期**: 音声認識された内容・AIの返答・再生中の曲名が WebSocket 経由でブラウザ画面にリアルタイム表示
- **グラスモフィズム Web UI**: アナログレコードアニメーション、オーディオビジュアライザー、再生/一時停止/スキップ/音量調整、ハイレゾバッジ表示
- **Webからの電源制御 (シャットダウン / 再起動)**: Web画面の「⚡ 電源」ボタンから Jetson Orin Nano Super 本体の再起動やシャットダウンを安全に実行可能
- **SQLite DB 連携 & FMラジオDJ曲紹介**: `music_meta.db` のリッチなメタデータ（ムード、エネルギー、ハイレゾ、ジャンル、`description_en` / `description_ja`、`title_en` / `artist_en`）を活用した選曲とアナウンス
- **英語DJモード & 日本語モード**: コマンドライン引数（`--en` / `--ja`）で英語FMラジオDJ風の曲紹介（`title_en` / `artist_en` / `description_en` 読み上げ）と日本語モードを自在に切り替え可能

### 📦 必要パッケージのインストール & 電源制御の権限設定 (Jetson初回設定)

```bash
# 1. Python パッケージのインストール
pip install fastapi uvicorn websockets python-mpd2 faster-whisper pyaudio pydantic edge-tts pykakasi

# 2. Web UI からのシャットダウン/再起動を許可する権限設定 (Jetson上で1度だけ実行)
bash setup_sudo_power.sh
```

### 🚀 起動方法（言語選択）

```bash
# 1. 英語DJモードで起動（description_en を英語音声で読み上げ）
python voice_bot.py --en
# または
python -m voice_bot --lang en

# 2. 日本語モードで起動（description_ja を VOICEVOX 青山龍星で読み上げ）
python voice_bot.py --ja
# または
python -m voice_bot --lang ja

# 3. 音声出力デバイスや moOde の IP を指定して起動
python -m voice_bot --en --audio-dev plughw:0,0 --moode-ip 192.168.68.198

# 4. マイクなし環境 / Web Chat のみで起動
python -m voice_bot --en --no-voice
```

### 📱 ブラウザからのアクセス
起動後、同一ネットワーク内のPCやスマートフォンのブラウザから以下にアクセスします：
```text
http://<Jetson-IPアドレス>:8000
```

### 💬 操作コマンド例
- **チャット / 音声共通**:
  - `「Jazzをかけて」` / `「ジャズを再生して」`
  - `「落ち着いたリラックスできる曲を流して」`
  - `「80年代の邦楽ロックをかけて」`
  - `「ハイレゾ音源の曲を聴きたい」`
  - `「音楽を止めて」` / `「一時停止して」`
  - `「次の曲にして」` / `「スキップ」`
  - `「前の曲に戻って」`
  - `「今日はどんな天気？」` などの一般的な雑談にも回答可能

