"""音楽ファイル本体へのメタデータタグ（Rating等）読み書き・パス解決モジュール。

mutagen を用いて、FLAC / MP3 / M4A / AAC / OGG / WAV / AIFF / DSF 等の各種音源ファイルの
メタデータ欄（Vorbis Comment, ID3 POPM/TXXX, MP4 atoms）にレーティング（★1〜5）を直接読み書きします。
Mp3tag、foobar2000、MusicBee、MediaMonkey、Windows Media Player、moOde との最大互換性を確保しています。

【Jetson / NAS パスマッピング仕様】
- Jetson Orin Nano Super では、NAS のデータが `/mnt/music/` にマウントされています。
- music_meta.db に記録されている `\\homenas\\music\\...` や `\\home\\music\\...` のパスを、
  Jetson 上の実パス `/mnt/music/...` に直接マッピングして NAS 音源ファイルのタグを更新します。
"""
import os
import re
import traceback
from typing import Any, Dict, List, Optional, Tuple

from . import config

try:
    import mutagen
    from mutagen import File as MutagenFile
    from mutagen.flac import FLAC
    from mutagen.id3 import ID3, POPM, TXXX, ID3NoHeaderError
    from mutagen.mp3 import MP3
    from mutagen.mp4 import MP4
    from mutagen.oggopus import OggOpus
    from mutagen.oggvorbis import OggVorbis
except ImportError:
    MutagenFile = None
    FLAC = None
    MP3 = None
    MP4 = None
    ID3 = None
    POPM = None
    TXXX = None


# Jetson (Linux) / Windows で想定される音楽ディレクトリのプレフィックス候補
STATIC_SEARCH_BASE_DIRS: List[str] = [
    # Linux / Jetson マウント先 (最優先: /mnt/music)
    "/mnt/music",
    "/mnt/music/music",
    "/mnt/music/homenas/music",
    "/mnt/music/home/music",
    "/mnt/nas/music",
    "/mnt/nas_music",
    "/mnt/nas",
    "/var/lib/mpd/music/NAS",
    "/var/lib/mpd/music",
    "/mnt/homenas_music",
    "/mnt/homenas/music",
    "/media/music",
    os.path.expanduser("~/music"),
    "/home/takai/music",
    "/home/orin/music",
    "/home/jetson/music",
    # Windows UNC / ドライブ文字
    r"\\homenas\music",
    r"\\home\music",
    r"\\homenas\homenas_music",
    r"//homenas/music",
    r"//home/music",
    r"D:\music",
    r"E:\music",
    r"C:\music",
]


def _safe_log(msg: str):
    """Windows cp932 等でもクラッシュしない安全な標準出力ログ"""
    try:
        print(msg, flush=True)
    except Exception:
        try:
            print(msg.encode("ascii", "replace").decode("ascii"), flush=True)
        except Exception:
            pass


def get_system_nas_mount_points() -> List[str]:
    """Linux システム (/proc/mounts) から NAS / 音楽関連のマウントポイントを自動検出"""
    mounts = []
    if os.path.exists("/proc/mounts"):
        try:
            with open("/proc/mounts", "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    parts = line.strip().split()
                    if len(parts) >= 3:
                        src, target, fstype = parts[0], parts[1], parts[2]
                        is_nas_fs = fstype in ("cifs", "smbfs", "nfs", "nfs4", "fuse", "fuse.rclone")
                        is_nas_path = "homenas" in src.lower() or "home" in src.lower() or "music" in target.lower() or "nas" in target.lower()
                        if is_nas_fs or is_nas_path:
                            if target not in mounts:
                                mounts.append(target)
                            sub_music = os.path.join(target, "music")
                            if os.path.isdir(sub_music) and sub_music not in mounts:
                                mounts.append(sub_music)
        except Exception:
            pass
    return mounts


def resolve_audio_file_path(
    file_path: Optional[str] = None,
    relative_path: Optional[str] = None,
) -> Optional[str]:
    """実在する音楽ファイルへのローカル/UNC絶対パスを探索・解決する。

    Jetson 上では `/mnt/music/` 配下に `\\homenas\\music\\` (または `\\home\\music\\`) の
    ファイルツリーが存在することを前提として、最優先で `/mnt/music/<relative_path>` を解決します。

    Args:
        file_path: 登録されている絶対パスまたはURL (例: \\\\homenas\\music\\Artist\\Album\\01.flac)
        relative_path: 音楽ライブラリのルートからの相対パス (例: Artist/Album/01.flac)

    Returns:
        実在が確認できたファイルパス（存在しない場合は None）
    """
    # 1. relative_path の正規化 (例: "Artist/Album/01.flac")
    rel_clean = None
    if relative_path:
        rel_clean = relative_path.replace("\\", "/").lstrip("/")
        for pfx in ("NAS/", "USB/", "SDCARD/", "nas/", "usb/", "sdcard/"):
            if rel_clean.startswith(pfx):
                rel_clean = rel_clean[len(pfx):].lstrip("/")
                break

    # 2. file_path から relative_path を抽出（\\homenas\music\ や \\home\music\, NAS/ の剥離）
    if file_path and not rel_clean:
        fp_norm = file_path.replace("\\", "/").lstrip("/")
        for pfx in ("NAS/", "USB/", "SDCARD/", "nas/", "usb/", "sdcard/"):
            if fp_norm.startswith(pfx):
                fp_norm = fp_norm[len(pfx):].lstrip("/")
                rel_clean = fp_norm
                break

        if not rel_clean:
            for uncpfx in ("homenas/music/", "home/music/", "homenas/homenas_music/", "music/", "//homenas/music/", "//home/music/"):
                idx = fp_norm.lower().find(uncpfx)
                if idx != -1:
                    rel_clean = fp_norm[idx + len(uncpfx):].lstrip("/")
                    break
            if not rel_clean:
                rel_clean = fp_norm

    # 3. 【最優先】Jetson の標準マウント先 /mnt/music 配下の多階層チェック
    if rel_clean:
        for prefix in ["", "music", "homenas/music", "home/music"]:
            candidate = os.path.normpath(os.path.join("/mnt/music", prefix, rel_clean.replace("/", os.sep)))
            if os.path.isfile(candidate):
                return os.path.abspath(candidate)

    # 4. 【第2優先】config.MUSIC_DIR との結合チェック
    custom_dir = getattr(config, "MUSIC_DIR", None)
    if custom_dir and rel_clean:
        for prefix in ["", "music", "homenas/music", "home/music"]:
            candidate = os.path.normpath(os.path.join(custom_dir, prefix, rel_clean.replace("/", os.sep)))
            if os.path.isfile(candidate):
                return os.path.abspath(candidate)

    # 5. 【第3優先】file_path の直接実在チェック（Windows UNC パス \\homenas\music\... やローカル絶対パス）
    if file_path and os.path.isfile(file_path):
        return os.path.abspath(file_path)
    if file_path:
        norm = os.path.normpath(file_path)
        if os.path.isfile(norm):
            return os.path.abspath(norm)

    # 6. 【第4優先】全マウント候補ディレクトリとの網羅的結合チェック
    base_dirs = []
    if custom_dir:
        base_dirs.append(custom_dir)
    base_dirs.extend(get_system_nas_mount_points())
    base_dirs.extend(STATIC_SEARCH_BASE_DIRS)

    seen_bases = set()
    unique_bases = []
    for b in base_dirs:
        if b and b not in seen_bases:
            seen_bases.add(b)
            unique_bases.append(b)

    rel_candidates = []
    if rel_clean:
        rel_candidates.append(rel_clean)
    if relative_path:
        rel_candidates.append(relative_path.replace("\\", "/").lstrip("/"))
    if file_path:
        rel_candidates.append(file_path.replace("\\", "/").lstrip("/"))

    for base in unique_bases:
        for rel in rel_candidates:
            if not rel:
                continue
            full = os.path.normpath(os.path.join(base, rel.replace("/", os.sep)))
            if os.path.isfile(full):
                return os.path.abspath(full)

    # 7. カレントディレクトリ基準
    for rel in rel_candidates:
        if rel and os.path.isfile(rel):
            return os.path.abspath(rel)

    # 8. ファイル名（basename）単体での探索
    for cand in [file_path, relative_path]:
        if not cand:
            continue
        fname = os.path.basename(cand.replace("\\", "/"))
        if not fname:
            continue
        if os.path.isfile(fname):
            return os.path.abspath(fname)
        for base in unique_bases:
            if os.path.isdir(base):
                direct_file = os.path.join(base, fname)
                if os.path.isfile(direct_file):
                    return os.path.abspath(direct_file)

    return None


def write_rating_to_file(file_path: str, rating: int) -> Tuple[bool, str]:
    """NAS上の音楽ファイル自体のメタデータタグにレーティング（★1〜5）を直接書き込む。

    【フォーマット別タグ仕様・Windows / Mp3tag / foobar2000 / moOde 最大互換マルチタギング】
    - FLAC / OGG / OPUS (Vorbis Comment):
      - `RATING`: "20", "40", "60", "80", "100" (★1〜5: 100点スケール標準)
      - `RATING WMP`: "1", "2", "3", "4", "5" (Mp3tag / WMP 列用)
      - `WM/SharedUserRating`: "1", "25", "50", "75", "99" (Windows Media / シェル互換)
      - `RATING:Windows Media Player 9 Series`: "20"〜"100"
      - `RATING:no@email`: "20"〜"100"
      - `RATING_PERCENT`: "20"〜"100"
      - `RATING_5`: "1"〜"5"
      - `RATING MM`: "20"〜"100"
    - MP3 / WAV / AIFF (ID3v2.3):
      - ID3v2.3 `POPM` (Windows Media Player 9 Series): 1, 64, 128, 196, 255 (★1〜5)
      - ID3v2.3 `POPM` (no@email): 1, 64, 128, 196, 255
      - `TXXX:RATING`: "60", "80", "100"
      - `TXXX:Rating WMP`: "1", "2", "3", "4", "5"
      - `TXXX:WM/SharedUserRating`: "1", "25", "50", "75", "99"
    - M4A / AAC / ALAC (MP4 Atoms):
      - `----:com.apple.iTunes:RATING`: "20"〜"100"
      - `----:com.apple.iTunes:Rating WMP`: "1"〜"5"
      - `----:com.apple.iTunes:WM/SharedUserRating`: "1"〜"99"
      - `rate`: 20, 40, 60, 80, 100

    Args:
        file_path: 対象の音楽ファイルパス (例: /mnt/music/Artist/Album/01.flac)
        rating: 1〜5 の整数

    Returns:
        (success: bool, detail_message: str)
    """
    if not file_path:
        return False, "ファイルパスが指定されていません。"

    if not os.path.isfile(file_path):
        msg = f"音源ファイルが存在しません: {file_path}"
        _safe_log(f"❌ [Tagger] {msg}")
        return False, msg

    if MutagenFile is None:
        msg = "mutagen ライブラリがインストールされていません (pip install mutagen)"
        _safe_log(f"⚠️ [Tagger] {msg}")
        return False, msg

    # 1〜5 にクランプ
    rating = max(1, min(5, int(rating)))
    rating_100 = str(rating * 20)  # 20, 40, 60, 80, 100
    rating_5 = str(rating)         # 1, 2, 3, 4, 5
    
    # Windows Media / Explorer 互換 (WM/SharedUserRating スケール: 1, 25, 50, 75, 99)
    wm_map = {1: "1", 2: "25", 3: "50", 4: "75", 5: "99"}
    wm_val = wm_map.get(rating, "50")

    ext = os.path.splitext(file_path)[1].lower()

    # ファイルのパーミッション確認・書き込み権限の付与試行
    try:
        if not os.access(file_path, os.W_OK):
            os.chmod(file_path, 0o666)
    except Exception:
        pass

    try:
        # 1. FLAC (Vorbis Comment)
        if ext == ".flac":
            audio = FLAC(file_path)
            # 各種プレイヤー・タグ管理ツール用フィールドを全網羅
            audio["RATING"] = [rating_100]
            audio["RATING WMP"] = [rating_5]
            audio["WM/SharedUserRating"] = [wm_val]
            audio["RATING:Windows Media Player 9 Series"] = [rating_100]
            audio["RATING:no@email"] = [rating_100]
            audio["RATING_PERCENT"] = [rating_100]
            audio["RATING_5"] = [rating_5]
            audio["RATING MM"] = [rating_100]
            audio.save()
            msg = f"FLAC タグに RATING={rating_100}, Rating WMP={rating_5}, WM/SharedUserRating={wm_val} (★{rating}) を書き込みました ({file_path})"
            _safe_log(f"✅ [Tagger] {msg}")
            return True, msg

        # 2. MP3 (ID3v2.3)
        elif ext == ".mp3":
            popm_map = {1: 1, 2: 64, 3: 128, 4: 196, 5: 255}
            popm_val = popm_map.get(rating, 128)
            popm_frames = [
                POPM(email="Windows Media Player 9 Series", rating=popm_val, count=0),
                POPM(email="no@email", rating=popm_val, count=0),
                POPM(email="quodlibet@sacredchao.net", rating=popm_val, count=0),
            ]
            txxx_frames = [
                TXXX(desc="RATING", text=[rating_100]),
                TXXX(desc="Rating WMP", text=[rating_5]),
                TXXX(desc="WM/SharedUserRating", text=[wm_val]),
                TXXX(desc="POPM", text=[str(popm_val)]),
            ]

            try:
                audio = MP3(file_path, ID3=ID3)
                if audio.tags is None:
                    audio.add_tags()
                audio.tags.setall("POPM", popm_frames)
                for t in txxx_frames:
                    audio.tags.setall(f"TXXX:{t.desc}", [t])
                # Windows Explorer 最適化のため ID3v2.3 で保存
                audio.save(v2_version=3)
            except Exception:
                try:
                    id3_tags = ID3(file_path)
                except ID3NoHeaderError:
                    id3_tags = ID3()
                id3_tags.setall("POPM", popm_frames)
                for t in txxx_frames:
                    id3_tags.setall(f"TXXX:{t.desc}", [t])
                id3_tags.save(file_path, v2_version=3)

            msg = f"MP3 ID3v2.3 タグに POPM={popm_val}, RATING={rating_100}, Rating WMP={rating_5} (★{rating}) を書き込みました ({file_path})"
            _safe_log(f"✅ [Tagger] {msg}")
            return True, msg

        # 3. M4A / MP4 / AAC / ALAC (MP4 Atoms)
        elif ext in (".m4a", ".mp4", ".m4p", ".aac", ".alac"):
            audio = MP4(file_path)
            itunes_rate = rating * 20
            audio["----:com.apple.iTunes:RATING"] = [str(itunes_rate).encode("utf-8")]
            audio["----:com.apple.iTunes:Rating WMP"] = [rating_5.encode("utf-8")]
            audio["----:com.apple.iTunes:WM/SharedUserRating"] = [wm_val.encode("utf-8")]
            try:
                audio["rate"] = [itunes_rate]
            except Exception:
                pass
            audio.save()
            msg = f"MP4 タグに RATING={itunes_rate}, Rating WMP={rating_5} (★{rating}) を書き込みました ({file_path})"
            _safe_log(f"✅ [Tagger] {msg}")
            return True, msg

        # 4. Ogg Vorbis / Opus
        elif ext in (".ogg", ".oga", ".opus"):
            if ext == ".opus":
                audio = OggOpus(file_path)
            else:
                audio = OggVorbis(file_path)
            audio["RATING"] = [rating_100]
            audio["RATING WMP"] = [rating_5]
            audio["WM/SharedUserRating"] = [wm_val]
            audio["RATING:no@email"] = [rating_100]
            audio["RATING_5"] = [rating_5]
            audio.save()
            msg = f"Ogg/Opus タグに RATING={rating_100}, Rating WMP={rating_5} (★{rating}) を書き込みました ({file_path})"
            _safe_log(f"✅ [Tagger] {msg}")
            return True, msg

        # 5. その他 (WAV, AIFF, DSF 等)
        else:
            audio = MutagenFile(file_path)
            if audio is not None and hasattr(audio, "tags") and audio.tags is not None:
                if hasattr(audio.tags, "setall") and POPM is not None:
                    popm_map = {1: 1, 2: 64, 3: 128, 4: 196, 5: 255}
                    popm_val = popm_map.get(rating, 128)
                    audio.tags.setall("POPM", [POPM(email="Windows Media Player 9 Series", rating=popm_val, count=0)])
                    audio.tags.setall("TXXX:RATING", [TXXX(desc="RATING", text=[rating_100])])
                    audio.tags.setall("TXXX:Rating WMP", [TXXX(desc="Rating WMP", text=[rating_5])])
                elif hasattr(audio.tags, "__setitem__"):
                    audio.tags["RATING"] = [rating_100]
                    audio.tags["RATING WMP"] = [rating_5]
                audio.save()
                msg = f"音源タグに RATING={rating_100} (★{rating}) を書き込みました ({file_path})"
                _safe_log(f"✅ [Tagger] {msg}")
                return True, msg
            else:
                msg = f"タグ構造に対応していないフォーマットです: {ext}"
                _safe_log(f"⚠️ [Tagger] {msg}")
                return False, msg

    except PermissionError as pe:
        msg = f"NASファイルへの書き込み権限がありません (Permission denied): {pe}"
        _safe_log(f"❌ [Tagger] {msg}")
        return False, msg
    except Exception as e:
        msg = f"ファイルタグ書き込み例外エラー ({file_path}): {e}"
        _safe_log(f"❌ [Tagger] {msg}")
        traceback.print_exc()
        return False, msg


def read_rating_from_file(file_path: str) -> Optional[int]:
    """音楽ファイル本体のメタデータタグからレーティング（★1〜5）を読み出す。

    Returns:
        1〜5 の整数（未設定または取得不能時は None）
    """
    if not os.path.isfile(file_path) or MutagenFile is None:
        return None

    ext = os.path.splitext(file_path)[1].lower()
    try:
        # 1. FLAC / OGG / Opus
        if ext in (".flac", ".ogg", ".oga", ".opus"):
            audio = MutagenFile(file_path)
            if audio and hasattr(audio, "tags") and audio.tags:
                for k in ["RATING WMP", "rating wmp", "RATING", "rating", "Rating", "WM/SharedUserRating", "RATING:no@email", "RATING_PERCENT", "RATING_5"]:
                    if k in audio.tags:
                        val = str(audio.tags[k][0]).strip()
                        if val.isdigit():
                            num = int(val)
                            if 1 <= num <= 5:
                                return num
                            elif num in (20, 40, 60, 80, 100):
                                return num // 20
                            elif num in (1, 25, 50, 75, 99):
                                # WM/SharedUserRating 変換
                                wm_rev = {1: 1, 25: 2, 50: 3, 75: 4, 99: 5}
                                return wm_rev.get(num, max(1, min(5, round(num / 20))))
                            elif num > 0:
                                return max(1, min(5, round(num / 20)))

        # 2. MP3 / WAV / AIFF
        elif ext in (".mp3", ".wav", ".aif", ".aiff"):
            tags_obj = None
            if ID3 is not None:
                try:
                    tags_obj = ID3(file_path)
                except Exception:
                    tags_obj = None

            if tags_obj is None:
                audio = MutagenFile(file_path)
                tags_obj = audio.tags if (audio and hasattr(audio, "tags")) else None

            if tags_obj:
                popm_frames = []
                if hasattr(tags_obj, "getall"):
                    popm_frames.extend(tags_obj.getall("POPM"))
                if hasattr(tags_obj, "values"):
                    for v in tags_obj.values():
                        if isinstance(v, POPM) and v not in popm_frames:
                            popm_frames.append(v)

                for p in popm_frames:
                    if hasattr(p, "rating"):
                        r = p.rating
                        if r >= 224:
                            return 5
                        elif r >= 160:
                            return 4
                        elif r >= 96:
                            return 3
                        elif r >= 32:
                            return 2
                        elif r >= 1:
                            return 1

                txxx_frames = []
                if hasattr(tags_obj, "getall"):
                    txxx_frames.extend(tags_obj.getall("TXXX:Rating WMP"))
                    txxx_frames.extend(tags_obj.getall("TXXX:RATING"))
                    txxx_frames.extend(tags_obj.getall("TXXX:WM/SharedUserRating"))
                if hasattr(tags_obj, "values"):
                    for v in tags_obj.values():
                        if isinstance(v, TXXX) and getattr(v, "desc", "") in ("RATING", "Rating WMP", "WM/SharedUserRating") and v not in txxx_frames:
                            txxx_frames.append(v)

                for t in txxx_frames:
                    if hasattr(t, "text") and t.text:
                        val = str(t.text[0]).strip()
                        if val.isdigit():
                            num = int(val)
                            if 1 <= num <= 5:
                                return num
                            elif num in (20, 40, 60, 80, 100):
                                return num // 20
                            elif num in (1, 25, 50, 75, 99):
                                wm_rev = {1: 1, 25: 2, 50: 3, 75: 4, 99: 5}
                                return wm_rev.get(num, max(1, min(5, round(num / 20))))

        # 3. MP4 / M4A
        elif ext in (".m4a", ".mp4", ".aac", ".alac"):
            audio = MP4(file_path)
            for atom_key in ["----:com.apple.iTunes:Rating WMP", "----:com.apple.iTunes:RATING", "----:com.apple.iTunes:WM/SharedUserRating"]:
                if atom_key in audio:
                    val = audio[atom_key][0]
                    if isinstance(val, bytes):
                        val = val.decode("utf-8", errors="ignore")
                    val_str = str(val).strip()
                    if val_str.isdigit():
                        num = int(val_str)
                        if 1 <= num <= 5:
                            return num
                        elif num in (20, 40, 60, 80, 100):
                            return num // 20
                        elif num in (1, 25, 50, 75, 99):
                            wm_rev = {1: 1, 25: 2, 50: 3, 75: 4, 99: 5}
                            return wm_rev.get(num, max(1, min(5, round(num / 20))))
            if "rate" in audio:
                r = audio["rate"][0]
                if isinstance(r, int):
                    if 1 <= r <= 5:
                        return r
                    elif r in (20, 40, 60, 80, 100):
                        return num // 20

    except Exception:
        pass

    return None
