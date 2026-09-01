# ReplayGain (EBU R128) 非破壊音量メタデータ管理 仕様書

本ドキュメントは、音楽ライブラリ（NAS / ローカル）に対して音声データを一切改変せずに音量調整値を付加・管理・削除するためのスクリプト `apply_replaygain_win.py` の仕様書です。

---

## 1. 概要と基本方針

### 1.1 ReplayGain 方式（メタデータ付加方式）の原理
音楽ファイルには、**「音声データ領域（PCM波形や圧縮オーディオフレーム）」** と **「メタデータ領域（タグヘッダー情報）」** が存在します。

本ツールおよび ReplayGain 方式では：
- **音声データ領域には 1ビットも触れません（完全非破壊）。**
- ファイル先頭のタグ情報領域（FLAC の場合は Vorbis Comment 領域、MP3 は ID3v2 TXXX 領域 等）に、「再生時に音量を何 dB 増減させるか」という指示数値をテキストとして書き込むだけです。
- 音質の劣化や不可逆なエンコード処理は一切発生しません。

### 1.2 完全な原状復帰（いつでもタグ削除可能）
本スクリプトには、ReplayGain 関連タグのみを安全に一括削除する機能（`--remove-tags` / `--clean`）が搭載されています。
元のファイル状態に戻したい場合は、いつでもワンコマンドで ReplayGain 付加前の初期状態に復元できます。

---

## 2. 各オーディオフォーマットと書き込みタグ仕様

EBU R128（標準ターゲット音量 -18 LUFS / 89 dB SPL）に準拠した以下のメタデータタグを付与・管理します。

| フォーマット | 格納タグ形式 | 主な書き込みタグ名 | 説明 |
| :--- | :--- | :--- | :--- |
| **FLAC** | Vorbis Comment | `REPLAYGAIN_TRACK_GAIN`<br>`REPLAYGAIN_TRACK_PEAK`<br>`REPLAYGAIN_ALBUM_GAIN`<br>`REPLAYGAIN_ALBUM_PEAK`<br>`REPLAYGAIN_REFERENCE_LOUDNESS` | トラック単位およびアルバム単位のゲイン値 (dB) とピーク値をテキスト記録 |
| **MP3** | ID3v2.3 / ID3v2.4 (TXXX) | `TXXX:replaygain_track_gain`<br>`TXXX:replaygain_track_peak`<br>`TXXX:replaygain_album_gain`<br>`TXXX:replaygain_album_peak` | ID3v2 ユーザー定義テキストフレームに格納 |
| **M4A / AAC / ALAC** | MP4 iTunes Tags | `----:com.apple.iTunes:replaygain_track_gain`<br>`----:com.apple.iTunes:replaygain_album_gain` または `iTunNORM` | Apple形式のメタデータアトムに記録 |
| **Ogg Vorbis / Opus** | Vorbis / Opus Comment | `REPLAYGAIN_*` または `R128_TRACK_GAIN`, `R128_ALBUM_GAIN` | 標準 Vorbis/Opus コメントに記録 |

---

## 3. NAS 環境への接続・最適化設計

NAS（UNC パス `\\homenas\music` やネットワークドライブ `Z:\` 等）でのバッチ処理において、以下の安全対策・最適化が施されています。

1. **UNCパス / ネットワークドライブ両対応**:
   - デフォルトで `\\homenas\music` を参照し、`--dir` オプションで任意のUNCパス・ドライブ文字（`Z:\Music` 等）を指定可能。
2. **SMB接続の負荷制御（並列プロセス制限）**:
   - デフォルトの並列ワーカー数を `2`（推奨 2〜4）に設定し、NAS の SMB リダイレクタ飽和やネットワーク遅延、ファイルロック競合を防止。
3. **ネットワーク遅延・切断に対する自動リトライ**:
   - 一時的な SMB 通信遅延が発生した場合、自動で最大3回のリトライを実施。
4. **差分スキップ機能**:
   - 既に ReplayGain タグが付与されているアルバムフォルダは自動的にスキップ（`--force` で強制再計算も可能）。

---

## 4. 必要環境・ツール

1. **Python 3.10 以上**
2. **mutagen**（Python タグ処理ライブラリ）:
   ```bash
   pip install mutagen
   ```
3. **rsgain.exe**（ReplayGain 2.0 スキャン・タギングCLIツール）:
   - [rsgain Releases (GitHub)](https://github.com/complexlogic/rsgain/releases) より Windows 64-bit 版をダウンロード。
   - 解凍した `rsgain.exe` を本スクリプトと同じディレクトリ、または `tools/` ディレクトリ、または PATH の通った場所に配置してください。
   - ※ `--check`（タグ確認）および `--remove-tags`（タグ削除）は、`rsgain` がなくても Python (mutagen) 単体で動作します。

---

## 5. コマンドライン引数と使用方法

### 5.1 コマンドラインオプション一覧

| オプション | 短縮形 | デフォルト値 | 説明 |
| :--- | :--- | :--- | :--- |
| `--dir <PATH>` | `-d` | `\\homenas\music` | 対象の音楽ライブラリディレクトリ（UNCパスまたはドライブ文字） |
| `--workers <N>` | `-w` | `2` | 並列処理プロセス数（NAS負荷を考慮し 2〜4 推奨） |
| `--force` | `-f` | `False` | 既存タグが存在する場合でも強制的に再計算・上書き |
| `--check` | `--scan` | `False` | ライブラリ内の ReplayGain タグ付加状況をスキャン・集計表示（変更なし） |
| `--remove-tags` | `--clean` | `False` | **【原状復帰】** 全楽曲から ReplayGain タグを完全消去 |
| `--dry-run` | `-n` | `False` | 実際の書き込み・削除を行わずに対象フォルダをシミュレーション表示 |
| `--track-only` | - | `False` | アルバムゲインを計算せず、トラックゲインのみ計算 |
| `--format <FMT>` | - | `all` | 対象ファイル形式を限定（`flac`, `mp3`, `m4a`, `ogg`, `opus`, `all`） |
| `--rsgain-path <EXE>` | - | 自動検出 | `rsgain.exe` の実行ファイルパスを明示的に指定 |

---

### 5.2 実行例

#### 1. NAS 内の未タグ楽曲をスキャンして新規タグ付加（標準実行）
```bash
python apply_replaygain_win.py
```

#### 2. ネットワークドライブ `Z:\Music` を指定して実行
```bash
python apply_replaygain_win.py --dir "Z:\Music"
```

#### 3. タグ付加状況の確認・レポート表示（変更なし）
```bash
python apply_replaygain_win.py --check
```

#### 4. 全音源から ReplayGain タグを完全消去して元の状態に戻す（原状復帰）
```bash
# まずシミュレーション（ドライラン）で確認
python apply_replaygain_win.py --remove-tags --dry-run

# 実際にタグを完全削除
python apply_replaygain_win.py --remove-tags
```

#### 5. FLAC 音源のみを対象にして強制再計算・上書き
```bash
python apply_replaygain_win.py --format flac --force
```

---

## 6. 音声再生プレイヤーでの利用方法

ReplayGain タグが付加された楽曲は、以下の対応プレイヤー等で「ReplayGain」または「音量均一化（Loudness Normalization）」を有効にすることで、アルバム全体のダイナミクスを保ったまま快適に音量が揃えられます。

- **MusicBee / foobar2000 / AIMP**: 設定で ReplayGain を有効化
- **Volumio / MoOde Audio / MPD**: 設定の ReplayGain モード（Album / Track）を有効化
- **LMS (Lyrion Music Server / Squeezebox)**: ReplayGain 設定でスマートゲインを有効化
