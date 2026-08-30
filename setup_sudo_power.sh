#!/bin/bash
# ==============================================================================
# Jetson Orin Nano Super: Webからのシャットダウン・再起動 権限設定スクリプト
# ==============================================================================
# このスクリプトは、Web UI (voice_bot) からパスワード入力なしで
# shutdown / reboot / systemctl poweroff を実行できるように sudoers を設定します。
# 
# 使い方 (Jetson Orin Nano Super 上で1回だけ実行):
#   bash setup_sudo_power.sh
# ==============================================================================

set -e

echo "⚡ [Setup] Jetson 電源制御用 sudoers 設定を開始します..."

CURRENT_USER=$(whoami)
SUDOERS_FILE="/etc/sudoers.d/jetson_power_control"

# 設定内容の生成
CONFIG_LINE="${CURRENT_USER} ALL=(ALL) NOPASSWD: /sbin/shutdown, /sbin/reboot, /sbin/poweroff, /bin/systemctl poweroff, /bin/systemctl reboot, /usr/bin/systemctl poweroff, /usr/bin/systemctl reboot, /usr/sbin/shutdown, /usr/sbin/reboot, /usr/sbin/poweroff"

echo "📝 [Setup] 作成ファイル: ${SUDOERS_FILE} (ユーザー: ${CURRENT_USER})"

# sudo でファイルを書き込み、パーミッションを 0440 に設定
echo "${CONFIG_LINE}" | sudo tee "${SUDOERS_FILE}" > /dev/null
sudo chmod 0440 "${SUDOERS_FILE}"

echo "✅ [Setup] 設定が完了しました！"
echo "これで Web UI から Jetson Orin Nano Super のシャットダウンや再起動が正常に動作します。"
