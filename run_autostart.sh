#!/bin/bash

# 作業ディレクトリへ移動
cd /home/mtakai/music_db/music_db

# 1. llama-server の起動 (バックグラウンド実行)
echo "[AutoStart] Starting llama-server..."
bash /home/mtakai/music_db/music_db/start_llama_server.sh &
LLAMA_PID=$!

# 2. llama-server の立ち上がり完了を待機 (ポート8080の応答確認)
echo "[AutoStart] Waiting for llama-server to become ready..."
until curl -s http://localhost:8080/health > /dev/null 2>&1 || curl -s http://localhost:8080/v1/models > /dev/null 2>&1; do
    sleep 1
done
echo "[AutoStart] llama-server is ready."

# 3. voice_bot の起動 (フォアグラウンド実行)
echo "[AutoStart] Starting voice_bot..."
export PYTHONUNBUFFERED=1
python3 -m voice_bot
