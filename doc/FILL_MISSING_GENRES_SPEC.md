# fill_missing_genres.py 仕様書

## 1. 概要
`fill_missing_genres.py` は、NAS（デフォルト: `\\homenas\music`）またはローカルディレクトリの音楽ライブラリを再帰的にスキャンし、ジャンルタグ（Genre）が設定されていない（未設定・空文字・Unknown）楽曲ファイルに対して、ローカルLLM（Lemonade Server）を活用して適切なジャンルを自動推論・設定するスクリプトです。

音源ファイル本体（WMA, FLAC, MP3, M4A, DSF等）のメタデータ欄へのタグ書き込みに加え、オプションで SQLite データベース（`music_meta.db`）の `genre` カラムも同期更新可能です。

---

## 2. 主な機能と特徴

1. **未設定ファイルの自動検出**
   - 音楽ファイル（FLAC, WMA, MP3, M4A, DSF, OGG, WAV等）のタグを精査し、ジャンル情報が存在しない、空文字、または `(ジャンル未設定)` のファイルを自動リストアップ。
2. **メタデータ自動補完（ディレクトリ階層・ファイル名解析）**
   - タグが空の古いファイル（WMA等）でも、フォルダ階層やファイル名（例: `Aerosmith\Devil's Got A New Disguise...\01 Dude.wma` や `Disney\アナと雪の女王...\14-Coronation Day.wma`）からアーティスト名、アルバム名、トラック番号除去後の曲名を高精度に自動補完してLLMに提供。
3. **ローカルLLM連携（Lemonade Server）**
   - `http://localhost:13305/v1` の OpenAI 互換 API 経由で稼働中のローカルモデル（Qwen2.5, Llama等）に問い合わせ。
   - ライブラリのジャンル体系（J ポップ, ロック, JAZZ, サウンドトラック, クラシック, アニメ映画 等）に合致するようプロンプトを制御。
4. **多彩なフォーマットへの確実なタグ保存**
   - `mutagen` の `easy=True` を主系統としつつ、WMA(`WM/Genre`), FLAC(`GENRE`), MP3(`TCON`), M4A(`\xa9gen`), DSF(`TCON`) の専用フォールバックを完備。
   - Windows の読み取り専用属性を自動解除して安全に保存。
5. **データベース同期 (`--update-db`)**
   - `music_meta.db` の `tracks` テーブルの `genre` カラムも同期更新可能。
6. **安全設計（Dry-Runモード）**
   - `--dry-run` により、ファイルへの書き込みを行わずにLLMの推論結果と適用予定タグをプレビュー確認可能。

---

## 3. コマンドライン引数仕様

```bash
python fill_missing_genres.py [オプション]
```

| 引数 | 型 | デフォルト値 | 説明 |
| :--- | :---: | :--- | :--- |
| `--music-dir` | 文字列 | `\\homenas\music` (Win)<br>`/mnt/music` (Linux) | スキャン対象の音楽ライブラリディレクトリ |
| `--base-url` | 文字列 | `http://localhost:13305/v1` | Lemonade Server のベースURL |
| `--model` | 文字列 | 自動検出 | 使用するLLMモデル名（未指定時は稼働中モデルを自動選択） |
| `--dry-run` | フラグ | 無効 | ファイルへの書き込みを行わず推論結果のみプレビュー表示 |
| `--update-db` | フラグ | 無効 | `music_meta.db` の `tracks` テーブルの `genre` も同期更新 |
| `--db-path` | 文字列 | `music_meta.db` | SQLite データベースファイルのパス |
| `--limit` | 整数 | `None` (無制限) | 処理する最大未設定ファイル件数（テスト用） |
| `--verbose`, `-v` | フラグ | 無効 | 詳細ログを出力 |

---

## 4. 使用例

### 4.1 シミュレーション確認 (Dry-Run)
```bash
# 先頭5件の未設定曲に対して推論結果をプレビュー
python fill_missing_genres.py --dry-run --limit 5
```

### 4.2 本番実行（音源ファイルタグにジャンルを設定）
```bash
python fill_missing_genres.py
```

### 4.3 音源ファイルタグの更新と同時にデータベースも更新
```bash
python fill_missing_genres.py --update-db
```

### 4.4 特定のフォルダのみを対象にする場合
```bash
python fill_missing_genres.py --music-dir "\\homenas\music\Disney"
```
