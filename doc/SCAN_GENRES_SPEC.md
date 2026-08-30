# NAS音楽ライブラリ ジャンル洗い出し・集計ツール仕様書 (scan_genres.py)

## 1. 概要
`scan_genres.py` は、NAS（デフォルト: `\\homenas\music`）やローカルストレージに保存されている音源ファイル（MP3, FLAC, WMA, M4A/AAC, OGG, WAV 等）からメタデータ（ジャンルタグ）を高速かつ高精度に抽出し、ジャンル別の楽曲数・比率・代表曲例を洗い出して集計・可視化するための専用ツールです。

---

## 2. 背景と目的
- **背景**: 音楽データベース（`music_meta.db`）の構築や楽曲推薦・自然言語検索システムの運用において、NAS上の音源にどのようなジャンルタグが実際に付与されているか（日本語タグ、英語タグ、表記揺れ、未設定曲の割合など）を事前に把握・棚卸しする必要がありました。
- **目的**:
  1. NAS上の全音源ファイルのジャンル分布を高速に洗い出し・可視化する。
  2. ジャンル未設定（タグなし）の音源ファイルを特定し、タグ整備やLLM補完の対象を明確にする。
  3. 各種フォーマット（Markdown / CSV / JSON）でレポートを出力し、ドキュメント化やデータ分析を容易にする。
  4. 既存の `music_meta.db` のLLM分類ジャンルとの比較を行う。

---

## 3. 主な機能とアーキテクチャ

```mermaid
flowchart TD
    A[NAS / 音源ディレクトリ<br/>\\\\homenas\\music] --> B[scan_music_library<br/>再帰的ファイル走査]
    B --> C[extract_track_info<br/>mutagen 多層タグ抽出]
    
    subgraph メタデータ抽出エンジン
        C --> D1[Easy mutagen 抽出]
        C --> D2[Raw Tags フォールバック<br/>TCON / GENRE / WM_Genre / ©gen]
        C --> D3[ID3標準コード展開<br/>数値タグ解決]
        C --> D4[複数ジャンル区切り分割]
    end

    D1 & D2 & D3 & D4 --> E[analyze_genres<br/>ジャンル統計・集計]
    
    subgraph レポート・エクスポート
        E --> F1[コンソール表示<br/>ランキング & サマリー]
        E --> F2[Markdownレポート<br/>--md genre_report.md]
        E --> F3[CSV集計出力<br/>--csv genre_summary.csv]
        E --> F4[JSON詳細出力<br/>--json details.json]
        E --> F5[未タグ曲リスト<br/>--export-untagged untagged.txt]
        E --> F6[SQLite DB対比<br/>--compare-db]
    end
```

---

## 4. 対応音声フォーマットと抽出仕様

| 拡張子 | フォーマット種別 | 主なタグ規格 | ジャンル抽出先タグ |
| :--- | :--- | :--- | :--- |
| `.mp3` | MPEG-1/2 Audio Layer-3 | ID3v1 / ID3v2 | `genre` (EasyID3), `TCON`, 数値展開 (例: `(17)` $\to$ `Rock`) |
| `.flac` | Free Lossless Audio Codec | Vorbis Comment | `genre` (EasyFLAC), `GENRE` |
| `.wma` | Windows Media Audio | ASF / WMA Tag | `WM/Genre`, `genre` |
| `.m4a`, `.aac` | MPEG-4 Audio / AAC | MP4 Tag / iTunes | `genre` (EasyMP4), `\xa9gen`, `gnre` |
| `.ogg` | Ogg Vorbis | Vorbis Comment | `genre`, `GENRE` |
| `.wav` | Waveform Audio | ID3 / RIFF INFO | `genre`, `TCON` |
| `.alac`, `.aiff`, `.dsf`, `.dff` | ハイレゾ / ロスレス等 | 各種タグ | `genre`, 各種メタフレーム |

---

## 5. コマンドライン引数仕様

```bash
python scan_genres.py [オプション]
```

| オプション | 短縮形 | デフォルト値 | 説明 |
| :--- | :---: | :---: | :--- |
| `--dir` | `-d` | `\\homenas\music` | スキャン対象のディレクトリパス |
| `--limit` | `-l` | なし (全件) | スキャンする最大ファイル数（テスト実行用。`0` で無制限） |
| `--format` | - | `all` | 対象ファイル形式の絞り込み (`all`, `flac`, `mp3`, `wma`, `m4a`) |
| `--flac-only` | - | `False` | FLACファイルのみを対象にスキャン (`--format flac` と同等) |
| `--top` | - | `50` | ターミナルに表示するランキング件数 (`0` で全件表示) |
| `--md`, `--output-md` | - | `genre_report.md` | Markdownレポート出力（全ジャンル集計表） |
| `--txt`, `--output-txt` | - | `genre_list.txt` | 全ジャンル一覧テキスト出力（コピー用リスト等） |
| `--csv`, `--output-csv` | - | `genre_summary.csv` | CSV集計データ出力（全ジャンル順位・曲数・割合・代表曲） |
| `--json`, `--output-json` | - | `None` | 全トラック情報および詳細統計をJSON出力 |
| `--export-untagged` | - | `None` | ジャンル未設定（タグなし）の曲一覧をテキストファイルに出力 |
| `--no-save` | - | `False` | ファイルへの自動保存を行わず、コンソール表示のみにする |
| `--compare-db` | - | `False` | ローカルの `music_meta.db` のジャンル集計結果と対比表示 |

---

## 6. 使用例

### 基本的な実行（全件スキャン＆ファイル自動出力＆コンソール表示）
```bash
python scan_genres.py
```
実行すると、自動的に以下のファイルが生成されます：
- `genre_report.md`: 全ジャンルの詳細ランキングおよびフォーマット別内訳
- `genre_list.txt`: 全ジャンル名一覧（曲数順およびコピー用プレーンテキスト）
- `genre_summary.csv`: 全ジャンルの順位・曲数・割合・代表曲入りCSVデータ

### テスト実行（最初の100件のみ）
```bash
python scan_genres.py --limit 100
```

### MarkdownレポートとCSVファイルを同時出力
```bash
python scan_genres.py --md genre_report.md --csv genre_summary.csv
```

### FLACファイルのみ対象にし、DB対比も実行
```bash
python scan_genres.py --flac-only --compare-db
```

### 未タグ曲の洗い出し
```bash
python scan_genres.py --export-untagged untagged_files.txt
```
