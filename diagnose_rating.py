#!/usr/bin/env python3
"""Jetson Orin Nano / Linux 環境用 Rating タグ書き込み診断スクリプト。

music_meta.db のパスと /mnt/music 配下の実ファイルのマッピング、
および音源ファイル（FLAC/MP3等）へのメタデータタグ書き込み権限を診断します。

使用方法:
    python diagnose_rating.py
"""
import os
import sqlite3
import sys

# Windows cp932 対策
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from voice_bot import config, tagger

print("=" * 60)
print("🔍 [Rating 診断] Jetson 音源ファイル Rating 書き換え診断を開始します")
print("=" * 60)

# 1. /mnt/music の確認
print("\n[ステップ 1] マウントポイントの確認:")
mnt_music = "/mnt/music"
if os.path.exists(mnt_music):
    print(f"  ✅ {mnt_music} が存在します。")
    try:
        items = os.listdir(mnt_music)
        print(f"  📁 {mnt_music} 内のディレクトリ/ファイル数: {len(items)} 件")
        print(f"     先頭サンプル: {items[:5]}")
    except Exception as e:
        print(f"  ❌ {mnt_music} の読み取りに失敗しました: {e}")
else:
    print(f"  ⚠️ {mnt_music} が存在しません。")
    print("     Jetson 上で NAS が別の場所にマウントされているか確認してください。")

# 2. Linux /proc/mounts の確認
if os.path.exists("/proc/mounts"):
    print("\n[ステップ 2] システムマウント情報 (/proc/mounts):")
    nas_mounts = tagger.get_system_nas_mount_points()
    print(f"  検出された NAS マウントポイント: {nas_mounts}")

# 3. music_meta.db の確認
print("\n[ステップ 3] データベース (music_meta.db) の確認:")
db_path = "music_meta.db"
if not os.path.exists(db_path):
    print(f"  ❌ {db_path} がカレントディレクトリに見つかりません。")
    sys.exit(1)

conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row
cur = conn.cursor()
cur.execute("SELECT id, title, artist, file_path, relative_path, rating FROM tracks LIMIT 5;")
sample_tracks = cur.fetchall()
conn.close()

print(f"  ✅ データベースから {len(sample_tracks)} 件のサンプルを取得しました。")

# 4. パス解決およびタグ書き込みテスト
print("\n[ステップ 4] 実ファイルパス解決およびタグ書き込みテスト:")
all_ok = True
for idx, tr in enumerate(sample_tracks, 1):
    t_id = tr["id"]
    title = tr["title"]
    fp = tr["file_path"]
    rel = tr["relative_path"]
    cur_rating = tr["rating"]

    print(f"\n--- [Track {idx}] 『{title}』 (ID: {t_id}) ---")
    print(f"  DB file_path: {fp}")
    print(f"  DB relative_path: {rel}")

    resolved = tagger.resolve_audio_file_path(fp, rel)
    if resolved:
        print(f"  ✅ 解決された実パス: {resolved}")
        # パーミッション確認
        is_writable = os.access(resolved, os.W_OK)
        print(f"  📝 書き込み権限 (W_OK): {'⭕ 可能' if is_writable else '❌ 不可 (Permission Denied)'}")

        # タグ読み取りテスト
        read_r = tagger.read_rating_from_file(resolved)
        print(f"  🏷️ 現在のファイル内 Rating: {read_r} (DB値: {cur_rating})")

        # テスト書き込み（現在の評価値または ★3）
        test_val = cur_rating if cur_rating else 3
        success, msg = tagger.write_rating_to_file(resolved, test_val)
        if success:
            print(f"  🎉 タグ書き込みテスト: 成功！ ({msg})")
        else:
            print(f"  ❌ タグ書き込みテスト: 失敗 ({msg})")
            all_ok = False
            if "Permission" in msg or not is_writable:
                print("     💡 原因: NAS マウントが読み取り専用 (ro) または書き込み権限がありません。")
                print("     💡 対処法: Jetson 上で以下のように rw,file_mode=0777,dir_mode=0777 で再マウントしてください:")
                print("        sudo mount -o remount,rw /mnt/music")
    else:
        print(f"  ❌ 実ファイルが見つかりません。")
        print(f"     探索候補: /mnt/music/{rel}")
        all_ok = False

print("\n" + "=" * 60)
if all_ok:
    print("🎉 【診断結果】すべての音源ファイルへの Rating 書き換えが正常に動作可能です！")
else:
    print("⚠️ 【診断結果】一部またはすべてのファイルでパス解決またはタグ書き込みに失敗しました。")
    print("   上記のエラー内容と対処法をご確認ください。")
print("=" * 60)
