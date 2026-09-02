"""音楽ファイル本体へのメタデータタグ（Rating等）読み書き・パス解決モジュール。

mutagen を用いて、FLAC / MP3 / M4A / AAC / OGG / WAV / AIFF / DSF 等の各種音源ファイルの
メタデータ欄（Vorbis Comment, ID3 POPM/TXXX, MP4 atoms）にレーティング（★1〜5）を直接読み書きします。
music_meta.db に記録されている NAS パス（\\homenas\\music\\...）や相対パスを自動解決し、
Windows / Linux (Jetson) / moOde の環境差を吸収して直接 NAS 音源ファイルを書き換えます。
"""
import os
import re
import traceback
from typing import Any, Dict, List, Optional

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
    # Linux / Jetson / moOde マウント先
    "/mnt/nas/music",
    "/mnt/nas_music",
    "/mnt/nas",
    "/mnt/music",
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
    r"\\homenas\homenas_music",
    r"//homenas/music",
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
                        # cifs, smbfs, nfs, sshfs, または homenas / music を含むマウント
                        is_nas_fs = fstype in ("cifs", "smbfs", "nfs", "nfs4", "fuse", "fuse.rclone")
                        is_nas_path = "homenas" in src.lower() or "music" in target.lower() or "nas" in target.lower()
                        if is_nas_fs or is_nas_path:
                            if target not in mounts:
                                mounts.append(target)
                            # サブディレクトリに music がある場合
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

    music_meta.db の file_path (\\\\homenas\\music\\...) や relative_path (Artist/Album/01.flac)
    から、Windows UNC パスや Linux/Jetson 上のマウントパスを網羅的に探索して返します。

    Args:
        file_path: 登録されている絶対パスまたはURL
        relative_path: 音楽ライブラリのルートからの相対パス

    Returns:
        実在が確認できたファイルパス（存在しない場合は None）
    """
    # 候補リストを生成
    base_dirs = []
    # 1. config.MUSIC_DIR（設定ファイル・環境変数）
    custom_dir = getattr(config, "MUSIC_DIR", None)
    if custom_dir:
        base_dirs.append(custom_dir)

    # 2. Linux /proc/mounts からの自動検出マウントポイント
    base_dirs.extend(get_system_nas_mount_points())

    # 3. 定義済み検索候補
    base_dirs.extend(STATIC_SEARCH_BASE_DIRS)

    # パス候補のバリエーションを収集
    raw_candidates = []
    if file_path:
        raw_candidates.append(file_path)
    if relative_path:
        raw_candidates.append(relative_path)

    # 1. 直接実在チェック（Windows UNC パス \\homenas\music\... やローカル絶対パス）
    for cand in raw_candidates:
        if cand and os.path.isfile(cand):
            return os.path.abspath(cand)
        if cand:
            norm = os.path.normpath(cand)
            if os.path.isfile(norm):
                return os.path.abspath(norm)

    # 2. 相対パスの正規化（NAS/ や USB/ などのプレフィックス除去、Windows UNC プレフィックス除去）
    rel_variants = set()
    for cand in raw_candidates:
        if not cand:
            continue
        c_clean = cand.replace("\\", "/").lstrip("/")
        rel_variants.add(c_clean)

        # moOde MPD プレフィックス除去 (例: "NAS/Artist/Album/01.flac" -> "Artist/Album/01.flac")
        for pfx in ("NAS/", "USB/", "SDCARD/", "nas/", "usb/", "sdcard/"):
            if c_clean.startswith(pfx):
                rel_variants.add(c_clean[len(pfx):].lstrip("/"))

        # Windows UNC プレフィックス除去 (例: "homenas/music/Artist/..." -> "Artist/...")
        for uncpfx in ("homenas/music/", "homenas/homenas_music/", "music/", "//homenas/music/"):
            idx = c_clean.lower().find(uncpfx)
            if idx != -1:
                rel_variants.add(c_clean[idx + len(uncpfx):].lstrip("/"))

    # 3. base_dirs × rel_variants の組み合わせ探索
    for base in base_dirs:
        if not base:
            continue
        for rel in rel_variants:
            if not rel:
                continue
            full = os.path.normpath(os.path.join(base, rel.replace("/", os.sep)))
            if os.path.isfile(full):
                return os.path.abspath(full)

    # 4. カレントディレクトリ基準
    for rel in rel_variants:
        if rel and os.path.isfile(rel):
            return os.path.abspath(rel)

    # 5. ファイル名（basename）単体での探索（カレントまたは base_dirs 直下）
    for cand in raw_candidates:
        if not cand:
            continue
        fname = os.path.basename(cand.replace("\\", "/"))
        if not fname:
            continue
        if os.path.isfile(fname):
            return os.path.abspath(fname)
        for base in base_dirs:
            if base and os.path.isdir(base):
                direct_file = os.path.join(base, fname)
                if os.path.isfile(direct_file):
                    return os.path.abspath(direct_file)

    return None


def write_rating_to_file(file_path: str, rating: int) -> bool:
    """NAS上の音楽ファイル自体のメタデータタグにレーティング（★1〜5）を直接書き込む。

    【フォーマット別タグ仕様・Windows / moOde / foobar2000 最大互換】
    - FLAC / OGG / OPUS:
      - Vorbis Comment `RATING`: "20", "40", "60", "80", "100" (★1〜5: Windows/foobar2000標準)
      - `RATING:no@email` / `RATING_PERCENT`: "20"〜"100"
      - `RATING_5`: "1"〜"5"
    - MP3 / WAV / AIFF:
      - ID3v2 `POPM` (Windows Media Player 9 Series): 1, 64, 128, 196, 255
      - ID3v2 `POPM` (no@email): 1, 64, 128, 196, 255
      - `TXXX:RATING`: "60", "80", "100" / `TXXX:Rating WMP`: "3", "4", "5"
    - M4A / AAC / ALAC:
      - MP4 atom `----:com.apple.iTunes:RATING` / `rate`: 20, 40, 60, 80, 100

    Args:
        file_path: 対象の音楽ファイルパス (ローカルまたはUNCパス)
        rating: 1〜5 の整数

    Returns:
        書き込み成功時 True、失敗時 False
    """
    if not os.path.isfile(file_path):
        _safe_log(f"❌ [Tagger] 音源ファイルが存在しません: {file_path}")
        return False

    if MutagenFile is None:
        _safe_log(f"⚠️ [Tagger] mutagen が未インストールのためタグ書き込みをスキップ: {file_path}")
        return False

    # 1〜5 にクランプ
    rating = max(1, min(5, int(rating)))
    rating_100 = str(rating * 20)  # 20, 40, 60, 80, 100
    rating_5 = str(rating)         # 1, 2, 3, 4, 5
    ext = os.path.splitext(file_path)[1].lower()

    # ファイルのパーミッション確認・書き込み権限の付与試行
    try:
        if not os.access(file_path, os.W_OK):
            os.chmod(file_path, 0o666)
    except Exception:
        pass

    try:
        # 1. FLAC
        if ext == ".flac":
            audio = FLAC(file_path)
            # Windows エクスプローラー・foobar2000・moOde 互換: RATING = 20, 40, 60, 80, 100
            audio["RATING"] = [rating_100]
            audio["RATING:no@email"] = [rating_100]
            audio["RATING_PERCENT"] = [rating_100]
            audio["RATING_5"] = [rating_5]
            audio.save()
            _safe_log(f"✅ [Tagger] NAS音源タグ書き込み完了 (FLAC RATING={rating_100}, ★{rating}): {file_path}")
            return True

        # 2. MP3
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
                TXXX(desc="POPM", text=[str(popm_val)]),
            ]

            try:
                audio = MP3(file_path, ID3=ID3)
                if audio.tags is None:
                    audio.add_tags()
                audio.tags.setall("POPM", popm_frames)
                for t in txxx_frames:
                    audio.tags.setall(f"TXXX:{t.desc}", [t])
                audio.save()
            except Exception:
                # MP3 フレーム破損時の ID3 直接フォールバック
                try:
                    id3_tags = ID3(file_path)
                except ID3NoHeaderError:
                    id3_tags = ID3()
                id3_tags.setall("POPM", popm_frames)
                for t in txxx_frames:
                    id3_tags.setall(f"TXXX:{t.desc}", [t])
                id3_tags.save(file_path)

            _safe_log(f"✅ [Tagger] NAS音源タグ書き込み完了 (MP3 POPM={popm_val}, RATING={rating_100}, ★{rating}): {file_path}")
            return True

        # 3. M4A / MP4 / AAC / ALAC
        elif ext in (".m4a", ".mp4", ".m4p", ".aac", ".alac"):
            audio = MP4(file_path)
            itunes_rate = rating * 20
            audio["----:com.apple.iTunes:RATING"] = [str(itunes_rate).encode("utf-8")]
            try:
                audio["rate"] = [itunes_rate]
            except Exception:
                pass
            audio.save()
            _safe_log(f"✅ [Tagger] NAS音源タグ書き込み完了 (MP4 RATING={itunes_rate}, ★{rating}): {file_path}")
            return True

        # 4. Ogg Vorbis / Opus
        elif ext in (".ogg", ".oga", ".opus"):
            if ext == ".opus":
                audio = OggOpus(file_path)
            else:
                audio = OggVorbis(file_path)
            audio["RATING"] = [rating_100]
            audio["RATING:no@email"] = [rating_100]
            audio["RATING_5"] = [rating_5]
            audio.save()
            _safe_log(f"✅ [Tagger] NAS音源タグ書き込み完了 (Ogg/Opus RATING={rating_100}, ★{rating}): {file_path}")
            return True

        # 5. その他 (WAV, AIFF, DSF 等、汎用 MutagenFile)
        else:
            audio = MutagenFile(file_path)
            if audio is not None and hasattr(audio, "tags") and audio.tags is not None:
                if hasattr(audio.tags, "setall") and POPM is not None:
                    popm_map = {1: 1, 2: 64, 3: 128, 4: 196, 5: 255}
                    popm_val = popm_map.get(rating, 128)
                    audio.tags.setall("POPM", [POPM(email="Windows Media Player 9 Series", rating=popm_val, count=0)])
                    audio.tags.setall("TXXX:RATING", [TXXX(desc="RATING", text=[rating_100])])
                elif hasattr(audio.tags, "__setitem__"):
                    audio.tags["RATING"] = [rating_100]
                audio.save()
                _safe_log(f"✅ [Tagger] NAS音源タグ書き込み完了 (Generic RATING={rating_100}, ★{rating}): {file_path}")
                return True

    except Exception as e:
        _safe_log(f"❌ [Tagger] NAS音源タグ書き込みエラー ({file_path}): {e}")
        traceback.print_exc()

    return False


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
                for k in ["RATING", "rating", "Rating", "RATING:no@email", "RATING_PERCENT", "RATING_5"]:
                    if k in audio.tags:
                        val = str(audio.tags[k][0]).strip()
                        if val.isdigit():
                            num = int(val)
                            if 1 <= num <= 5:
                                return num
                            elif num in (20, 40, 60, 80, 100):
                                return num // 20
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
                # POPM フレームの確認
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

                # TXXX:RATING の確認
                txxx_frames = []
                if hasattr(tags_obj, "getall"):
                    txxx_frames.extend(tags_obj.getall("TXXX:RATING"))
                    txxx_frames.extend(tags_obj.getall("TXXX:Rating WMP"))
                if hasattr(tags_obj, "values"):
                    for v in tags_obj.values():
                        if isinstance(v, TXXX) and getattr(v, "desc", "") in ("RATING", "Rating WMP") and v not in txxx_frames:
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

        # 3. MP4 / M4A
        elif ext in (".m4a", ".mp4", ".aac", ".alac"):
            audio = MP4(file_path)
            if "----:com.apple.iTunes:RATING" in audio:
                val = audio["----:com.apple.iTunes:RATING"][0]
                if isinstance(val, bytes):
                    val = val.decode("utf-8", errors="ignore")
                val_str = str(val).strip()
                if val_str.isdigit():
                    num = int(val_str)
                    if 1 <= num <= 5:
                        return num
                    elif num in (20, 40, 60, 80, 100):
                        return num // 20
            if "rate" in audio:
                r = audio["rate"][0]
                if isinstance(r, int):
                    if 1 <= r <= 5:
                        return r
                    elif r in (20, 40, 60, 80, 100):
                        return r // 20

    except Exception:
        pass

    return None
