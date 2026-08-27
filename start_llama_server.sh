#!/usr/bin/env bash
set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LLAMA_BIN_DIR="${LLAMA_BIN_DIR:-$HOME/llama.cpp/build/bin}"
MODEL_PATH="${MODEL_PATH:-$SCRIPT_DIR/../model/google_gemma-4-E2B-it-Q4_K_M.gguf}"

# 相対パスの場合はスクリプトディレクトリ基準に解決
if [[ ! "$MODEL_PATH" = /* ]]; then
  MODEL_PATH="$SCRIPT_DIR/$MODEL_PATH"
fi

echo "=========================================="
echo " 🚀 llama-server 起動スクリプト"
echo " 📂 モデルパス : $MODEL_PATH"
echo " 📂 バイナリ   : $LLAMA_BIN_DIR/llama-server"
echo " 📡 エンドポイント: http://0.0.0.0:8080/v1"
echo "=========================================="

if [ ! -f "$MODEL_PATH" ]; then
  echo "⚠️ [警告] モデルファイルが見つかりません: $MODEL_PATH"
  echo "   パスを確認するか、MODEL_PATH環境変数で正しいパスを指定してください。"
fi

exec "$LLAMA_BIN_DIR/llama-server" \
  -m "$MODEL_PATH" \
  --host 0.0.0.0 \
  --port 8080 \
  -ngl 99 \
  -c 2048 \
  --threads 4

