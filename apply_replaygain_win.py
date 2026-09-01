# -*- coding: utf-8 -*-
"""
apply_replaygain_win.py
Windows / NAS環境向け 非破壊 ReplayGain (EBU R128) 一括タグ付加・管理スクリプト

【特徴・非破壊処理について】
- 音声データ領域（PCM/圧縮ストリーム）には1ビットも触れません。
- メタデータタグ領域（FLAC: Vorbis Comment, MP3: ID3v2 TXXX, M4A: iTunes Tags 等）にのみ
  音量調整指示値 (Track Gain / Album Gain [dB]) をテキストとして書き込みます。
- 完全な非破壊処理のため、いつでもタグを削除（--remove-tags）すれば元の状態に戻せます。
- NAS (UNCパス \\homenas\\music やネットワークドライブ Z:\\ 等) への接続と負荷を考慮した設計です。
"""

import os
import sys
import shutil
import subprocess
import argparse
import time
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed

# mutagenの読み込み
try:
    from mutagen import File as MutagenFile
    from mutagen.flac import FLAC
    from mutagen.mp3 import MP3
    from mutagen.id3 import ID3, TXXX, RVA2
    from mutagen.mp4 import MP4
    from mutagen.oggvorbis import OggVorbis
    from mutagen.oggopus import OggOpus
except ImportError:
    print("[エラー] mutagen がインストールされていません。")
    print("実行コマンド: pip install mutagen")
    sys.exit(1)

# WindowsコンソールのUTF-8出力対応
if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

# ==========================================
# デフォルト設定パラメータ
# ==========================================
DEFAULT_MUSIC_DIR = r"\\homenas\music"
DEFAULT_WORKERS = 2  # NASへのSMB同時接続負荷を抑えるための推奨値 (2〜4)
SUPPORTED_EXTENSIONS = {
    ".flac", ".mp3", ".m4a", ".aac", ".ogg",
    ".opus", ".alac", ".wav", ".aiff", ".aif", ".wma"
}

REPLAYGAIN_TAG_KEYWORDS = (
    "replaygain_track_gain",
    "replaygain_track_peak",
    "replaygain_album_gain",
    "replaygain_album_peak",
    "replaygain_reference_loudness",
    "r128_track_gain",
    "r128_album_gain",
)


def find_rsgain_binary(custom_path: str | None = None) -> str | None:
    """rsgain 実行ファイルのパスを自動検出"""
    if custom_path:
        p = Path(custom_path)
        if p.is_file():
            return str(p.resolve())
        elif p.is_dir() and (p / "rsgain.exe").is_file():
            return str((p / "rsgain.exe").resolve())

    # 1. カレントディレクトリ or スクリプト階層
    script_dir = Path(__file__).parent
    candidates = [
        script_dir / "rsgain.exe",
        script_dir / "tools" / "rsgain.exe",
        script_dir / "bin" / "rsgain.exe",
        Path("rsgain.exe"),
        Path("tools/rsgain.exe"),
    ]
    for cand in candidates:
        if cand.is_file():
            return str(cand.resolve())

    # 2. PATH環境変数
    which_path = shutil.which("rsgain") or shutil.which("rsgain.exe")
    if which_path:
        return which_path

    # 3. 一般的なWindowsインストール先
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    additional_paths = [
        Path(program_files) / "rsgain" / "rsgain.exe",
        Path(local_app_data) / "Programs" / "rsgain" / "rsgain.exe",
        Path(r"C:\tools\rsgain\rsgain.exe"),
    ]
    for p in additional_paths:
        if p.is_file():
            return str(p.resolve())

    return None


def get_replaygain_info(file_path: Path) -> dict[str, str]:
    """
    ファイルから ReplayGain 関連のメタデータタグを取得
    戻り値: {'track_gain': '-6.20 dB', 'album_gain': '-5.10 dB', ...}
    """
    info = {}
    ext = file_path.suffix.lower()
    try:
        if ext == ".flac":
            audio = FLAC(str(file_path))
            for k, v in audio.items():
                k_lower = k.lower()
                if "replaygain" in k_lower or "r128" in k_lower:
                    val = v[0] if isinstance(v, list) and v else str(v)
                    info[k_lower] = val
        elif ext == ".mp3":
            try:
                tags = ID3(str(file_path))
                for key, frame in tags.items():
                    if isinstance(frame, TXXX) and "replaygain" in frame.desc.lower():
                        info[frame.desc.lower()] = frame.text[0] if frame.text else ""
                    elif key.startswith("RVA2"):
                        info["rva2"] = "present"
            except Exception:
                audio = MP3(str(file_path))
                for k in audio.keys():
                    if "replaygain" in k.lower():
                        info[k.lower()] = str(audio[k])
        elif ext in {".m4a", ".aac", ".alac"}:
            audio = MP4(str(file_path))
            for k, v in audio.items():
                if "replaygain" in k.lower() or "itunnorm" in k.lower():
                    val = v[0] if isinstance(v, list) and v else str(v)
                    info[k.lower()] = str(val)
        elif ext == ".ogg":
            audio = OggVorbis(str(file_path))
            for k, v in audio.items():
                if "replaygain" in k.lower():
                    val = v[0] if isinstance(v, list) and v else str(v)
                    info[k.lower()] = val
        elif ext == ".opus":
            audio = OggOpus(str(file_path))
            for k, v in audio.items():
                if "r128" in k.lower() or "replaygain" in k.lower():
                    val = v[0] if isinstance(v, list) and v else str(v)
                    info[k.lower()] = val
        else:
            audio = MutagenFile(str(file_path))
            if audio and audio.tags:
                for k in audio.tags.keys():
                    if "replaygain" in str(k).lower():
                        info[str(k).lower()] = str(audio.tags[k])
    except Exception:
        pass
    return info


def is_already_tagged(file_path: Path) -> bool:
    """既存のReplayGainタグが存在するか判定"""
    info = get_replaygain_info(file_path)
    return len(info) > 0


def remove_replaygain_tags(file_path: Path) -> tuple[bool, str]:
    """
    指定ファイルの ReplayGain メタデータタグのみを完全に削除（音声データは不変）
    戻り値: (成功フラグ, メッセージ)
    """
    ext = file_path.suffix.lower()
    try:
        if ext == ".flac":
            audio = FLAC(str(file_path))
            keys_to_del = [k for k in audio.keys() if "replaygain" in k.lower() or "r128" in k.lower()]
            if not keys_to_del:
                return (True, "NO_TAGS")
            for k in keys_to_del:
                del audio[k]
            audio.save()
            return (True, f"REMOVED_{len(keys_to_del)}_TAGS")

        elif ext == ".mp3":
            try:
                tags = ID3(str(file_path))
                keys_to_del = []
                for key, frame in list(tags.items()):
                    if isinstance(frame, TXXX) and "replaygain" in frame.desc.lower():
                        keys_to_del.append(key)
                    elif key.startswith("RVA2") or "replaygain" in key.lower():
                        keys_to_del.append(key)
                if not keys_to_del:
                    return (True, "NO_TAGS")
                for k in keys_to_del:
                    del tags[k]
                tags.save()
                return (True, f"REMOVED_{len(keys_to_del)}_TAGS")
            except Exception as e:
                return (False, f"MP3_TAG_ERROR: {e}")

        elif ext in {".m4a", ".aac", ".alac"}:
            audio = MP4(str(file_path))
            keys_to_del = [k for k in audio.keys() if "replaygain" in k.lower() or "itunnorm" in k.lower()]
            if not keys_to_del:
                return (True, "NO_TAGS")
            for k in keys_to_del:
                del audio[k]
            audio.save()
            return (True, f"REMOVED_{len(keys_to_del)}_TAGS")

        elif ext == ".ogg":
            audio = OggVorbis(str(file_path))
            keys_to_del = [k for k in audio.keys() if "replaygain" in k.lower()]
            if not keys_to_del:
                return (True, "NO_TAGS")
            for k in keys_to_del:
                del audio[k]
            audio.save()
            return (True, f"REMOVED_{len(keys_to_del)}_TAGS")

        elif ext == ".opus":
            audio = OggOpus(str(file_path))
            keys_to_del = [k for k in audio.keys() if "r128" in k.lower() or "replaygain" in k.lower()]
            if not keys_to_del:
                return (True, "NO_TAGS")
            for k in keys_to_del:
                del audio[k]
            audio.save()
            return (True, f"REMOVED_{len(keys_to_del)}_TAGS")

        else:
            audio = MutagenFile(str(file_path))
            if audio and audio.tags:
                keys_to_del = [k for k in audio.tags.keys() if "replaygain" in str(k).lower()]
                if not keys_to_del:
                    return (True, "NO_TAGS")
                for k in keys_to_del:
                    del audio.tags[k]
                audio.save()
                return (True, f"REMOVED_{len(keys_to_del)}_TAGS")
            return (True, "NO_TAGS")

    except Exception as e:
        return (False, f"ERROR: {str(e)}")


def process_album_folder(
    folder_path: Path,
    rsgain_exe: str,
    force: bool = False,
    album_mode: bool = True,
    dry_run: bool = False
) -> tuple[str, str, int]:
    """
    アルバムフォルダ単位で rsgain を呼び出し、ReplayGainタグを書き込む
    戻り値: (フォルダパス, ステータス, 処理トラック数)
    """
    try:
        audio_files = [
            f for f in folder_path.iterdir()
            if f.is_file() and f.suffix.lower() in SUPPORTED_EXTENSIONS
        ]
    except Exception as e:
        return (str(folder_path), f"ACCESS_ERROR: {str(e)}", 0)

    if not audio_files:
        return (str(folder_path), "NO_AUDIO", 0)

    # 既存タグの確認
    if not force:
        untagged_files = [f for f in audio_files if not is_already_tagged(f)]
        if not untagged_files:
            return (str(folder_path), "SKIPPED_ALREADY_TAGGED", len(audio_files))

    if dry_run:
        return (str(folder_path), "DRY_RUN_READY", len(audio_files))

    # rsgain コマンドの構築
    # easy モード: フォルダ内の楽曲をスキャンしてタグ書き込み
    # -S: スキップ既存タグ (forceがFalseの場合)
    # -O: アルバム＆トラック両計算
    # -q: ログ抑制
    cmd = [rsgain_exe, "easy"]
    if not force:
        cmd.append("-S")
    if album_mode:
        cmd.append("-O")
    cmd.extend(["-q", str(folder_path)])

    # リトライ処理（NASの一時的SMB遅延対策）
    max_retries = 3
    for attempt in range(max_retries):
        try:
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                check=True,
                startupinfo=startupinfo
            )
            return (str(folder_path), "SUCCESS", len(audio_files))
        except subprocess.CalledProcessError as e:
            err_msg = e.stderr.strip() if e.stderr else "Execution failed"
            if attempt < max_retries - 1:
                time.sleep(1.0)
                continue
            return (str(folder_path), f"RSGAIN_ERROR: {err_msg}", len(audio_files))
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(1.0)
                continue
            return (str(folder_path), f"FAILED: {str(e)}", len(audio_files))

    return (str(folder_path), "FAILED_MAX_RETRIES", len(audio_files))


def process_album_clean_folder(folder_path: Path, dry_run: bool = False) -> tuple[str, str, int, int]:
    """
    アルバムフォルダ内の全楽曲から ReplayGain タグを削除（原状復帰）
    戻り値: (フォルダパス, ステータス, 削除曲数, 総曲数)
    """
    try:
        audio_files = [
            f for f in folder_path.iterdir()
            if f.is_file() and f.suffix.lower() in SUPPORTED_EXTENSIONS
        ]
    except Exception as e:
        return (str(folder_path), f"ACCESS_ERROR: {str(e)}", 0, 0)

    if not audio_files:
        return (str(folder_path), "NO_AUDIO", 0, 0)

    removed_count = 0
    for f in audio_files:
        if is_already_tagged(f):
            if not dry_run:
                ok, msg = remove_replaygain_tags(f)
                if ok:
                    removed_count += 1
            else:
                removed_count += 1

    if removed_count == 0:
        return (str(folder_path), "NO_TAGS_FOUND", 0, len(audio_files))

    status = "DRY_RUN_CLEANED" if dry_run else "TAGS_REMOVED"
    return (str(folder_path), status, removed_count, len(audio_files))


def scan_folder_tags_summary(folder_path: Path) -> dict:
    """フォルダ内の ReplayGain タグ付加状況を集計"""
    try:
        audio_files = [
            f for f in folder_path.iterdir()
            if f.is_file() and f.suffix.lower() in SUPPORTED_EXTENSIONS
        ]
    except Exception as e:
        return {"folder": str(folder_path), "error": str(e)}

    if not audio_files:
        return {"folder": str(folder_path), "total": 0, "tagged": 0, "untagged": 0}

    tagged_count = 0
    sample_tags = {}
    for f in audio_files:
        tags = get_replaygain_info(f)
        if tags:
            tagged_count += 1
            if not sample_tags:
                sample_tags = tags

    return {
        "folder": str(folder_path),
        "total": len(audio_files),
        "tagged": tagged_count,
        "untagged": len(audio_files) - tagged_count,
        "sample": sample_tags
    }


def main():
    parser = argparse.ArgumentParser(
        description="Windows / NAS 向け ReplayGain (EBU R128) 非破壊バッチ処理・管理ツール",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
【使用例】
  # 1. NASの音楽ライブラリを標準設定でスキャン・新規タグ付加
  python apply_replaygain_win.py

  # 2. ネットワークドライブ Z:\\Music を指定して実行
  python apply_replaygain_win.py --dir "Z:\\Music"

  # 3. 既存タグがあっても強制再計算・上書き (--force)
  python apply_replaygain_win.py --force

  # 4. タグ付加状況の確認・スキャンのみ実行 (--check)
  python apply_replaygain_win.py --check

  # 5. 全音源から ReplayGain タグを完全消去して元の状態に戻す (--remove-tags)
  python apply_replaygain_win.py --remove-tags

  # 6. 書き込みを行わずにシミュレーション (--dry-run)
  python apply_replaygain_win.py --dry-run
        """
    )
    parser.add_argument(
        "--dir", "-d",
        default=os.environ.get("MUSIC_DIR", DEFAULT_MUSIC_DIR),
        help=f"対象の音楽ディレクトリ（UNCパスまたはドライブ文字）。デフォルト: {DEFAULT_MUSIC_DIR}"
    )
    parser.add_argument(
        "--workers", "-w",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"並列プロセス数（NAS負荷軽減のため 2〜4 推奨）。デフォルト: {DEFAULT_WORKERS}"
    )
    parser.add_argument(
        "--force", "-f",
        action="store_true",
        help="既存のReplayGainタグが存在する場合でも強制再計算・上書きする"
    )
    parser.add_argument(
        "--dry-run", "-n",
        action="store_true",
        help="実際のタグ書き込み・削除を行わずに対象フォルダを確認する"
    )
    parser.add_argument(
        "--check", "--scan",
        action="store_true",
        help="タグ付加状況（Track Gain, Album Gain）をスキャンして表示する（変更なし）"
    )
    parser.add_argument(
        "--remove-tags", "--clean",
        action="store_true",
        help="【原状復帰】対象フォルダ内の音声ファイルから ReplayGain タグを完全に削除する"
    )
    parser.add_argument(
        "--track-only",
        action="store_true",
        help="アルバムゲインを計算せず、トラックゲインのみ計算する"
    )
    parser.add_argument(
        "--rsgain-path",
        default=None,
        help="rsgain.exe の実行ファイルパスを明示的に指定"
    )
    parser.add_argument(
        "--format",
        choices=["flac", "mp3", "m4a", "ogg", "opus", "all"],
        default="all",
        help="対象フォーマットを絞り込む（デフォルト: all）"
    )

    args = parser.parse_args()

    # 対象拡張子の絞り込み
    global SUPPORTED_EXTENSIONS
    if args.format != "all":
        ext = f".{args.format}"
        SUPPORTED_EXTENSIONS = {ext}
        print(f"[Filter] 対象フォーマット: {ext}")

    root = Path(args.dir)
    print("==================================================")
    print("  Windows / NAS ReplayGain (EBU R128) Manager")
    print("  音声データは完全非破壊（タグ情報領域のみ操作）")
    print("==================================================")
    print(f"対象ディレクトリ : {root}")
    print(f"並列プロセス数   : {args.workers}")
    if args.dry_run:
        print("実行モード       : ⚠️ DRY-RUN (シミュレーション / 変更は書き込まれません)")

    # 1. ディレクトリ接続確認
    if not root.exists():
        print(f"\n[エラー] 指定されたパスが見つかりません: {root}")
        print("  - NASの電源が入っているか確認してください。")
        print("  - ネットワーク共有 (\\homenas\\music) または ドライブ割り当て (Z:\\ 等) を確認してください。")
        sys.exit(1)

    # 2. rsgain 検出（タグ書き込みモード時のみ必須）
    rsgain_exe = None
    if not args.check and not args.remove_tags:
        rsgain_exe = find_rsgain_binary(args.rsgain_path)
        if not rsgain_exe:
            print("\n[エラー] 'rsgain.exe' が見つかりませんでした。")
            print("------------------------------------------------------------------")
            print("ReplayGainの音量測定・タグ書き込みには 'rsgain' が必要です。")
            print("以下のいずれかの方法で配置してください：")
            print("  1. GitHub から rsgain (Windows 64-bit) をダウンロード:")
            print("     https://github.com/complexlogic/rsgain/releases")
            print(f"  2. 解凍した 'rsgain.exe' を本スクリプトと同じ階層 ({Path(__file__).parent}) に配置")
            print("  3. または --rsgain-path 引数で rsgain.exe のパスを指定")
            print("------------------------------------------------------------------")
            print("※ すでに付加されたタグの確認 (--check) や削除 (--remove-tags) は")
            print("   rsgain がなくても Python (mutagen) 単体で実行可能です。")
            sys.exit(1)
        else:
            print(f"使用 rsgain      : {rsgain_exe}")

    # 3. 音楽フォルダの探索
    print("\nライブラリを探索中...")
    album_folders = []
    for current_dir, _, files in os.walk(root):
        if any(Path(f).suffix.lower() in SUPPORTED_EXTENSIONS for f in files):
            album_folders.append(Path(current_dir))

    total_folders = len(album_folders)
    print(f"検出アルバム/楽曲フォルダ数: {total_folders}\n")

    if total_folders == 0:
        print("対象となる音声ファイルが見つかりませんでした。")
        return

    # ----------------------------------------------------
    # モードA: タグ確認・スキャン (--check)
    # ----------------------------------------------------
    if args.check:
        print("=== ReplayGain タグ付加状況スキャン ===")
        total_tracks = 0
        total_tagged_tracks = 0
        fully_tagged_folders = 0
        partial_tagged_folders = 0
        untagged_folders = 0

        for i, folder in enumerate(album_folders, 1):
            info = scan_folder_tags_summary(folder)
            t = info.get("total", 0)
            tag_cnt = info.get("tagged", 0)
            total_tracks += t
            total_tagged_tracks += tag_cnt

            status_str = ""
            if tag_cnt == t and t > 0:
                fully_tagged_folders += 1
                status_str = "全曲タグ付与済"
            elif tag_cnt > 0:
                partial_tagged_folders += 1
                status_str = f"一部タグ付与 ({tag_cnt}/{t}曲)"
            else:
                untagged_folders += 1
                status_str = "未付与"

            sample_desc = ""
            sample = info.get("sample", {})
            if sample:
                tg = sample.get("replaygain_track_gain") or sample.get("r128_track_gain") or ""
                ag = sample.get("replaygain_album_gain") or sample.get("r128_album_gain") or ""
                if tg or ag:
                    sample_desc = f" [例: Track {tg}, Album {ag}]"

            print(f"[{i}/{total_folders}] [{status_str}] {folder}{sample_desc}")

        print("\n=== スキャン結果サマリー ===")
        print(f"総フォルダ数       : {total_folders}")
        print(f"  - 全曲タグ付与済 : {fully_tagged_folders} フォルダ")
        print(f"  - 一部タグ付与   : {partial_tagged_folders} フォルダ")
        print(f"  - 未付与         : {untagged_folders} フォルダ")
        print(f"総楽曲数           : {total_tracks} 曲")
        print(f"  - タグ付与済楽曲 : {total_tagged_tracks} 曲 ({(total_tagged_tracks/total_tracks*100 if total_tracks else 0):.1f}%)")
        return

    # ----------------------------------------------------
    # モードB: タグ削除・原状復帰 (--remove-tags)
    # ----------------------------------------------------
    if args.remove_tags:
        print("=== 【原状復帰】ReplayGain タグ一括削除処理 ===")
        print("※ 音声データは一切変更されず、メタデータ内の ReplayGain 指示値のみを削除します。\n")

        cleaned_folders = 0
        skipped_folders = 0
        total_cleaned_tracks = 0
        error_folders = 0

        with ProcessPoolExecutor(max_workers=args.workers) as executor:
            futures = {
                executor.submit(process_album_clean_folder, folder, args.dry_run): folder
                for folder in album_folders
            }

            for i, future in enumerate(as_completed(futures), 1):
                folder_str, status, removed_cnt, tot = future.result()
                if status in {"TAGS_REMOVED", "DRY_RUN_CLEANED"}:
                    cleaned_folders += 1
                    total_cleaned_tracks += removed_cnt
                    prefix = "[DRY-RUN 削除対象]" if args.dry_run else "[タグ削除完了]"
                    print(f"[{i}/{total_folders}] {prefix} ({removed_cnt}/{tot}曲) {folder_str}")
                elif status == "NO_TAGS_FOUND":
                    skipped_folders += 1
                else:
                    error_folders += 1
                    print(f"[{i}/{total_folders}] [{status}] {folder_str}")

        print("\n=== タグ削除サマリー ===")
        print(f"タグ削除実施フォルダ : {cleaned_folders} フォルダ")
        print(f"削除対象楽曲数       : {total_cleaned_tracks} 曲")
        print(f"タグ未存在（スキップ）: {skipped_folders} フォルダ")
        if error_folders > 0:
            print(f"エラー発生フォルダ   : {error_folders} フォルダ")
        return

    # ----------------------------------------------------
    # モードC: ReplayGain 計算・タグ付加処理
    # ----------------------------------------------------
    print("=== ReplayGain (EBU R128) バッチ書き込み開始 ===")
    album_mode = not args.track_only
    print(f"計算モード       : {'アルバム＆トラックゲイン' if album_mode else 'トラックゲインのみ'}")
    print(f"既存タグスキップ : {'無効 (強制上書き)' if args.force else '有効 (付加済はスキップ)'}\n")

    success_count = 0
    skipped_count = 0
    error_count = 0
    processed_tracks = 0

    with ProcessPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(
                process_album_folder,
                folder,
                rsgain_exe,
                args.force,
                album_mode,
                args.dry_run
            ): folder
            for folder in album_folders
        }

        for i, future in enumerate(as_completed(futures), 1):
            folder_str, status, track_cnt = future.result()
            if status == "SUCCESS":
                success_count += 1
                processed_tracks += track_cnt
                print(f"[{i}/{total_folders}] [タグ付加完了] ({track_cnt}曲) {folder_str}")
            elif status == "DRY_RUN_READY":
                success_count += 1
                processed_tracks += track_cnt
                print(f"[{i}/{total_folders}] [DRY-RUN 対象] ({track_cnt}曲) {folder_str}")
            elif status == "SKIPPED_ALREADY_TAGGED":
                skipped_count += 1
            elif status != "NO_AUDIO":
                error_count += 1
                print(f"[{i}/{total_folders}] [{status}] {folder_str}")

    print("\n=== 処理完了サマリー ===")
    print(f"タグ付加完了 (対象) : {success_count} フォルダ ({processed_tracks} 曲)")
    print(f"スキップ (付加済)   : {skipped_count} フォルダ")
    print(f"エラー発生          : {error_count} フォルダ")


if __name__ == "__main__":
    main()