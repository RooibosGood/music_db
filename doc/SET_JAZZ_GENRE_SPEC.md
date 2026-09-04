# set_jazz_genre.py 仕様書

## 1. 概要
`set_jazz_genre.py` は、NAS上の `music/JAZZ` フォルダ配下に存在するすべての楽曲ファイル（FLAC, DSF, DFF, MP3, M4A, AAC, OGG, OPUS, WAV, AIFF, WMA等）を再帰的にスキャンし、ジャンルメタデータタグに `"JAZZ"` を設定・書き込むスクリプトです。

Windows 環境および Linux / Jetson 環境の双方向で実行可能であり、必要に応じて SQLite データベース（`music_meta.db`）の `genre` カラムも同期更新できます。

---

## 2. 主な機能と特徴

1. **多彩な音源フォーマットへの対応**
   - 可逆圧縮・非圧縮・不可逆圧縮の主要フォーマットをすべて網羅。
   - `mutagen` の `easy=True` インターフェースによる統一的な書き込みを基本としつつ、DSF（DSD ID3タグ）や特殊ヘッダのフォーマットには専用の書き込みフォールバックを実装。
2. **柔軟なジャンル設定モード (`--mode`)**
   - `overwrite` (デフォルト): 既存ジャンルを `"JAZZ"` に書き換え。既に `"JAZZ"` 単独の場合は書き込みをスキップ。
   - `append`: 既存ジャンルを保持し、リストに `"JAZZ"` が含まれていなければ追加（例: `['Vocal']` -> `['Vocal', 'JAZZ']`）。
   - `if_empty`: ジャンルタグが未設定または空の場合のみ `"JAZZ"` を設定。
3. **安全設計（Dry-Runモード）**
   - `--dry-run` オプションにより、実際にファイルを変更することなく、変更対象ファイルや更新前後のジャンルタグの差分プレビューを確認可能。
4. **データベース同期機能 (`--update-db`)**
   - オプション指定により、音楽ファイル本体のタグ更新と同時に、`music_meta.db` の `tracks` テーブルの `genre` カラムも一括更新可能。
5. **Windows 読み取り専用属性の自動解除**
   - ファイルに Read-Only 属性が付与されている場合でも、書き込み前に自動で書き込み許可属性を付与して安全に保存。

---

## 3. コマンドライン引数仕様

```bash
python set_jazz_genre.py [オプション]
```

| 引数 | 型 | デフォルト値 | 説明 |
| :--- | :---: | :--- | :--- |
| `--target-dir` | 文字列 | `\\homenas\music\JAZZ` (Win)<br>`/mnt/music/JAZZ` (Linux) | スキャンおよび設定対象のフォルダパス |
| `--genre` | 文字列 | `JAZZ` | 設定するジャンル文字列 |
| `--mode` | 選択 | `overwrite` | 動作モード (`overwrite`, `append`, `if_empty`) |
| `--dry-run` | フラグ | 無効 | 変更を適用せずシミュレーション表示 |
| `--update-db` | フラグ | 無効 | `music_meta.db` の `tracks` テーブルの `genre` も同期更新 |
| `--db-path` | 文字列 | `music_meta.db` | SQLite データベースファイルのパス |
| `--db-genre` | 文字列 | `--genre` と同じ | DBに書き込むジャンル名（例: `'ジャズ'` と日本語で登録したい場合に使用） |
| `--limit` | 整数 | `None` (無制限) | 処理する最大ファイル件数（テスト用） |
| `--verbose`, `-v` | フラグ | 無効 | 全ファイルの変更前後ログを出力 |

---

## 4. 使用例

### 4.1 シミュレーション確認 (Dry-Run)
```bash
# 先頭10件を対象に変更内容をプレビュー
python set_jazz_genre.py --dry-run --limit 10
```

### 4.2 全ファイルを "JAZZ" で上書き更新
```bash
python set_jazz_genre.py
```

### 4.3 既存ジャンルを維持しつつ "JAZZ" を追加
```bash
python set_jazz_genre.py --mode append
```

### 4.4 ファイルタグと同時にデータベースも更新
```bash
python set_jazz_genre.py --update-db --db-genre "ジャズ"
```

### 4.5 Jetson / Linux 環境での実行
```bash
python set_jazz_genre.py --target-dir "/mnt/music/JAZZ"
```

---

## 5. 処理結果とエラーハンドリング
- スキャン終了時、総スキャン件数、更新件数、スキップ件数、書き込み失敗件数、所要時間のサマリーが表示されます。
- 万が一特定のファイルで書き込みエラーが発生した場合でも、例外がキャッチされてログに出力され、残りのファイルの処理が継続されます。
