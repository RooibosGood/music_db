"""音楽ファイル本体へのメタデータタグ（Rating等）読み書き・パス解決モジュール。

mutagen を用いて、FLAC / MP3 / M4A / AAC / OGG / WAV / AIFF / DSF 等の各種音源ファイルの
メタデータ欄（Vorbis Comment, ID3 POPM/TXXX, MP4 atoms）にレーティング（★1〜5）を直接読み書きします。
"""
import os
import re
from typing import Any, Dict, List, Optional

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
SEARCH_BASE_DIRS: List[str] = [
    # Windows
    r"\\homenas\music",
    r"\\homenas\homenas_music",
    r"D:\music",
    r"E:\music",
    # Linux / Jetson / moOde
    "/mnt/nas/music",
    "/mnt/music",
    "/var/lib/mpd/music/NAS",
    "/var/lib/mpd/music",
    "/mnt/homenas_music",
    "/media/music",
    os.path.expanduser("~/music"),
]


def resolve_audio_file_path(
    file_path: Optional[str] = None,
    relative_path: Optional[str] = None,
) -> Optional[str]:
    """実在する音楽ファイルへのローカル絶対パスを探索・解決する。

    Args:
        file_path: 登録されている絶対パスまたはURL
        relative_path: 音楽ライブラリのルートからの相対パス

    Returns:
        実在が確認できたファイルパス（存在しない場合は None）
    """
    # 1. file_path そのまま
    if file_path and os.path.isfile(file_path):
        return os.path.abspath(file_path)

    # 2. file_path のスラッシュ/バックスラッシュ正規化
    if file_path:
        norm = os.path.normpath(file_path)
        if os.path.isfile(norm):
            return os.path.abspath(norm)

    # 3. relative_path からの探索
    rel = (relative_path or "").replace("\\", "/").lstrip("/")
    if rel:
        # 3-1. カレントディレクトリ基準
        if os.path.isfile(rel):
            return os.path.abspath(rel)

        # 3-2. SEARCH_BASE_DIRS 配下の探索
        for base in SEARCH_BASE_DIRS:
            candidate = os.path.normpath(os.path.join(base, rel.replace("/", os.sep)))
            if os.path.isfile(candidate):
                return os.path.abspath(candidate)

    # 4. file_path からファイル名（basename）を取り出して探索
    if file_path:
        fname = os.path.basename(file_path.replace("\\", "/"))
        if fname:
            # カレントディレクトリ
            if os.path.isfile(fname):
                return os.path.abspath(fname)

    return None


def _safe_log(msg: str):
    """Windows cp932 等でもクラッシュしない安全な標準出力ログ"""
    try:
        print(msg, flush=True)
    except Exception:
        try:
            print(msg.encode("ascii", "replace").decode("ascii"), flush=True)
        except Exception:
            pass


def write_rating_to_file(file_path: str, rating: int) -> bool:
    """音楽ファイル自体のメタデータタグにレーティング（★1〜5）を書き込む。

    【フォーマット別仕様】
    - FLAC / OGG / OPUS: Vorbis Comment `RATING` に "1"〜"5"
    - MP3 / WAV / AIFF: ID3v2 `POPM` (1, 64, 128, 196, 255) および `TXXX:RATING` ("1"〜"5")
    - M4A / AAC / ALAC: MP4 atom `----:com.apple.iTunes:RATING` (20, 40, 60, 80, 100) / `rate`

    Args:
        file_path: 対象の音楽ファイルパス
        rating: 1〜5 の整数

    Returns:
        書き込み成功時 True、失敗時 False
    """
    if not os.path.isfile(file_path):
        return False

    if MutagenFile is None:
        _safe_log(f"[Tagger] mutagen がインストールされていないためファイルタグ書き込みをスキップ: {file_path}")
        return False

    # 1〜5 にクランプ
    rating = max(1, min(5, int(rating)))
    ext = os.path.splitext(file_path)[1].lower()

    try:
        # 1. FLAC
        if ext == ".flac":
            audio = FLAC(file_path)
            # 汎用 Vorbis comment: RATING = "1".."5"
            audio["RATING"] = [str(rating)]
            audio.save()
            _safe_log(f"🏷️ [Tagger] FLAC タグに Rating=★{rating} を書き込みました: {file_path}")
            return True

        # 2. MP3
        elif ext == ".mp3":
            popm_map = {1: 1, 2: 64, 3: 128, 4: 196, 5: 255}
            popm_val = popm_map.get(rating, 128)
            popm_frames = [
                POPM(email="Windows Media Player 9 Series", rating=popm_val, count=0),
                POPM(email="no@email", rating=popm_val, count=0),
            ]
            txxx_frame = [TXXX(desc="RATING", text=[str(rating)])]

            try:
                audio = MP3(file_path, ID3=ID3)
                if audio.tags is None:
                    audio.add_tags()
                audio.tags.setall("POPM", popm_frames)
                audio.tags.setall("TXXX:RATING", txxx_frame)
                audio.save()
            except Exception:
                # MP3 オーディオフレーム解析失敗時の ID3 直接フォールバック
                try:
                    id3_tags = ID3(file_path)
                except ID3NoHeaderError:
                    id3_tags = ID3()
                id3_tags.setall("POPM", popm_frames)
                id3_tags.setall("TXXX:RATING", txxx_frame)
                id3_tags.save(file_path)

            _safe_log(f"🏷️ [Tagger] MP3 ID3 (POPM={popm_val}, RATING={rating}) を書き込みました: {file_path}")
            return True

        # 3. M4A / MP4 / AAC / ALAC
        elif ext in (".m4a", ".mp4", ".m4p", ".aac", ".alac"):
            audio = MP4(file_path)
            # iTunes RATING (20, 40, 60, 80, 100)
            itunes_rate = rating * 20
            audio["----:com.apple.iTunes:RATING"] = [str(itunes_rate).encode("utf-8")]
            try:
                audio["rate"] = [itunes_rate]
            except Exception:
                pass
            audio.save()
            _safe_log(f"🏷️ [Tagger] MP4 タグに Rating={itunes_rate} (★{rating}) を書き込みました: {file_path}")
            return True

        # 4. Ogg Vorbis / Opus
        elif ext in (".ogg", ".oga"):
            audio = OggVorbis(file_path)
            audio["RATING"] = [str(rating)]
            audio.save()
            _safe_log(f"🏷️ [Tagger] Ogg タグに Rating=★{rating} を書き込みました: {file_path}")
            return True
        elif ext == ".opus":
            audio = OggOpus(file_path)
            audio["RATING"] = [str(rating)]
            audio.save()
            _safe_log(f"🏷️ [Tagger] Opus タグに Rating=★{rating} を書き込みました: {file_path}")
            return True

        # 5. その他 (WAV, AIFF, DSF 等、汎用 MutagenFile)
        else:
            audio = MutagenFile(file_path)
            if audio is not None and hasattr(audio, "tags") and audio.tags is not None:
                # ID3 タグがある場合
                if hasattr(audio.tags, "setall") and POPM is not None:
                    popm_map = {1: 1, 2: 64, 3: 128, 4: 196, 5: 255}
                    popm_val = popm_map.get(rating, 128)
                    audio.tags.setall("POPM", [POPM(email="Windows Media Player 9 Series", rating=popm_val, count=0)])
                    audio.tags.setall("TXXX:RATING", [TXXX(desc="RATING", text=[str(rating)])])
                elif hasattr(audio.tags, "__setitem__"):
                    audio.tags["RATING"] = [str(rating)]
                audio.save()
                _safe_log(f"🏷️ [Tagger] 音源タグに Rating=★{rating} を書き込みました: {file_path}")
                return True

    except Exception as e:
        _safe_log(f"⚠️ [Tagger] ファイルタグ書き込みエラー ({file_path}): {e}")

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
                for k in ["RATING", "rating", "Rating"]:
                    if k in audio.tags:
                        val = str(audio.tags[k][0]).strip()
                        if val.isdigit():
                            num = int(val)
                            if 1 <= num <= 5:
                                return num
                            elif num in (20, 40, 60, 80, 100):
                                return num // 20

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
                # POPM フレームの確認 (getall または直接探索)
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
                if hasattr(tags_obj, "values"):
                    for v in tags_obj.values():
                        if isinstance(v, TXXX) and getattr(v, "desc", "") == "RATING" and v not in txxx_frames:
                            txxx_frames.append(v)

                for t in txxx_frames:
                    if hasattr(t, "text") and t.text:
                        val = str(t.text[0]).strip()
                        if val.isdigit() and 1 <= int(val) <= 5:
                            return int(val)

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
