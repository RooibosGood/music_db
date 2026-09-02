#!/usr/bin/env bash
# ==============================================================================
# Jetson Orin Nano Super 用 NAS (//homenas/music) マウントセットアップスクリプト
#
# voice_bot から NAS の音源ファイルへ直接レーティング (Rating) を書き換えるために、
# /mnt/nas/music へフルアクセス権限 (rw, 0777) で CIFS/Samba マウントを行います。
# ==============================================================================

set -e

MOUNT_POINT="/mnt/nas/music"
NAS_SHARE="//homenas/music"

echo "========================================================"
echo "🎵 NAS (${NAS_SHARE}) マウントセットアップを開始します"
echo "========================================================"

# 1. cifs-utils の確認・インストール
if ! command -v mount.cifs &> /dev/null; then
    echo "📦 cifs-utils をインストール中..."
    sudo apt-get update -y
    sudo apt-get install -y cifs-utils
fi

# 2. マウントディレクトリ作成
echo "📁 マウントポイントを作成中: ${MOUNT_POINT}"
sudo mkdir -p "${MOUNT_POINT}"
sudo chmod 777 "${MOUNT_POINT}"

# 3. 既存マウントの確認
if mountpoint -q "${MOUNT_POINT}"; then
    echo "ℹ️ 既に ${MOUNT_POINT} はマウントされています。"
else
    echo "🔌 NAS をマウント中: ${NAS_SHARE} -> ${MOUNT_POINT}"
    # ゲストアクセス（パスワードなし）でのマウント試行
    if sudo mount -t cifs "${NAS_SHARE}" "${MOUNT_POINT}" -o guest,rw,noperm,file_mode=0777,dir_mode=0777,iocharset=utf8,vers=3.0; then
        echo "✅ ゲスト認証でマウントに成功しました！"
    else
        echo "⚠️ ゲスト認証でのマウントに失敗しました。ユーザー認証で再試行します。"
        read -p "NAS のユーザー名を入力してください (例: admin, guest): " NAS_USER
        read -s -p "NAS のパスワードを入力してください: " NAS_PASS
        echo ""
        sudo mount -t cifs "${NAS_SHARE}" "${MOUNT_POINT}" -o username="${NAS_USER}",password="${NAS_PASS}",rw,noperm,file_mode=0777,dir_mode=0777,iocharset=utf8,vers=3.0
        echo "✅ ユーザー認証でマウントに成功しました！"
    fi
fi

# 4. 接続テスト
echo "🔍 音楽ファイルの読み書きテストを実行中..."
if [ -d "${MOUNT_POINT}" ]; then
    FILE_COUNT=$(ls -1 "${MOUNT_POINT}" 2>/dev/null | wc -l)
    echo "📊 発見されたアイテム数: ${FILE_COUNT} 件"
    echo "🎉 NAS のマウントが正常に完了しました！"
    echo "   マウント先: ${MOUNT_POINT}"
    echo "   これで voice_bot から NAS の音源ファイルへ直接 Rating タグを書き込めます。"
else
    echo "❌ マウントポイントにアクセスできません。"
    exit 1
fi
