# populate_english_names.py 仕様書

## 1. 概要
`populate_english_names.py` は、既存の音楽ライブラリデータベース（`music_meta.db`）の `tracks` テーブルに対し、英語DJモード（`voice_bot.py`）で利用される **`title_en`（英語曲名・ローマ字）** および **`artist_en`（英語アーティスト名・ローマ字）** を高速・高精度に一括生成・更新（UPDATE）する専用スクリプトです。

他のメタデータ（区分、ジャンル、ムード、エネルギー、日本語・英語解説文など）を一切変更・破壊することなく、未設定（NULL / 空文字）の楽曲のみを対象として効率的に補完します。

---

## 2. 主な特徴と変換アルゴリズム

### 2.1 3段階ハイブリッド変換ロジック (`--mode hybrid`, デフォルト)
1. **Pass-Through 高速即時判定 (英語・ラテン文字)**:
   - タイトルおよびアーティスト名に日本語文字（漢字・ひらがな・カタカナ・長音記号）が含まれていない場合、LLMを呼び出さずそのまま `title_en = title`, `artist_en = artist` として瞬時に登録。洋楽やアルファベット表記の邦楽をミリ秒単位で処理します。
2. **ローカルLLM (Lemonade Server) による公式英題 / ローマ字抽出**:
   - 日本語文字が含まれる楽曲について、ローカルLLMに問い合わせて「公式英語タイトル（Official English Title）」または「自然なヘボン式ローマ字（Hepburn Romanization）」を取得。
3. **pykakasi によるフォールバック & 超高速一括変換**:
   - LLMオフライン時、または高速処理モード（`--mode kakasi`）において、`pykakasi` を用いて日本語をヘボン式ローマ字（単語先頭大文字）に自動変換。

---

## 3. コマンドラインオプション

| オプション | 短縮形 | 型 | デフォルト | 説明 |
| :--- | :--- | :--- | :--- | :--- |
| `--mode` | - | `choice` | `hybrid` | 動作モード (`hybrid`: 英語即時+日本語LLM/kakasi, `kakasi`: 全件kakasi高速変換, `llm`: 日本語曲すべてLLM) |
| `--limit` | - | `int` | `None` (全件) | 処理する曲数の上限（テスト・確認用） |
| `--all` / `--force` | - | `flag` | `False` | すでに設定済みの曲も含めて全曲上書き再生成 |
| `--dry-run` | - | `flag` | `False` | データベースへの書き込みを行わず、結果プレビューのみ表示 |
| `--batch-size` | - | `int` | `50` | データベースのコミット間隔 |
| `--db-path` | - | `str` | `music_meta.db` | 対象データベースファイルのパス |

---

## 4. 使用例

```bash
# 1. 未設定の全曲をハイブリッドモード（英語パススルー ＋ 日本語LLM/kakasi）で更新 (推奨)
python populate_english_names.py

# 2. 超高速モード（pykakasiのみ使用、全件を数秒〜十数秒で一括ローマ字化）
python populate_english_names.py --mode kakasi

# 3. 最初の20曲でドライラン（DB更新なし・動作確認）
python populate_english_names.py --limit 20 --dry-run

# 4. すでに設定済みの曲も含めて全件再生成
python populate_english_names.py --all
```

---

## 5. 安全設計
- **トランザクション管理**: 指定バッチサイズ（デフォルト50件）ごとにコミット。
- **安全な中断対応**: `Ctrl+C`（KeyboardInterrupt）で中断された場合も、それまでに処理したレコードをコミットして正常終了。次回実行時は未処理レコードから自動再開されます。
