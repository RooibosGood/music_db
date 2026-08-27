#!/usr/bin/env bash
set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LLAMA_BIN_DIR="${LLAMA_BIN_DIR:-$HOME/llama.cpp/build/bin}"

# 1. バイナリの解決
LLAMA_SERVER_BIN="$LLAMA_BIN_DIR/llama-server"
if [ ! -x "$LLAMA_SERVER_BIN" ]; then
  if command -v llama-server >/dev/null 2>&1; then
    LLAMA_SERVER_BIN="$(command -v llama-server)"
  fi
fi

# 2. モデルファイル候補の探索リスト
TARGET_MODELS=(
  "Qwen_Qwen3-4B-Q4_K_M.gguf"
  "qwen_qwen3-4b-q4_k_m.gguf"
  "qwen2.5-1.5b-instruct-q4_k_m.gguf"
  "qwen2.5-1.5b-instruct.gguf"
  "Qwen2.5-1.5B-Instruct-Q4_K_M.gguf"
  "gemma-2-2b-it-Q4_K_M.gguf"
)

CANDIDATE_PATHS=()

# コマンドライン引数 ($1) が指定された場合を最優先
if [ $# -ge 1 ] && [ -n "$1" ]; then
  CANDIDATE_PATHS+=("$1")
fi

# 環境変数 MODEL_PATH があれば追加
if [ -n "${MODEL_PATH:-}" ]; then
  CANDIDATE_PATHS+=("$MODEL_PATH")
fi

# 代表的な配置場所と対象モデルの組み合わせを探索
SEARCH_DIRS=("$HOME/LLM/model" "$HOME/LLM/models" "$HOME/model" "$HOME/models" "$HOME/Audio_SQL/model" "$SCRIPT_DIR/../model" "$SCRIPT_DIR/../../model" "$SCRIPT_DIR/model")

for d in "${SEARCH_DIRS[@]}"; do
  for m in "${TARGET_MODELS[@]}"; do
    CANDIDATE_PATHS+=("$d/$m")
  done
done

# 候補から実在するファイルを検索
CHOSEN_MODEL=""
for p in "${CANDIDATE_PATHS[@]}"; do
  if [ -f "$p" ]; then
    CHOSEN_MODEL="$(realpath "$p")"
    break
  fi
done

# もし特定モデルが見つからなければ、ディレクトリ内の任意の .gguf を自動探索
if [ -z "$CHOSEN_MODEL" ]; then
  for d in "${SEARCH_DIRS[@]}"; do
    if [ -d "$d" ]; then
      FOUND_GGUF=$(find "$d" -maxdepth 2 -name "*.gguf" 2>/dev/null | head -n 1 || true)
      if [ -n "$FOUND_GGUF" ] && [ -f "$FOUND_GGUF" ]; then
        CHOSEN_MODEL="$(realpath "$FOUND_GGUF")"
        echo "💡 GGUF モデルを自動検出しました: $CHOSEN_MODEL"
        break
      fi
    fi
  done
fi

echo "=========================================="
echo " 🚀 llama-server 起動スクリプト"
echo " 📂 モデルパス : ${CHOSEN_MODEL:-未検出}"
echo " 📂 バイナリ   : $LLAMA_SERVER_BIN"
echo " 📡 エンドポイント: http://0.0.0.0:8080/v1"
echo "=========================================="

if [ -z "$CHOSEN_MODEL" ] || [ ! -f "$CHOSEN_MODEL" ]; then
  echo "❌ [エラー] モデルファイル (.gguf) が見つかりませんでした。"
  echo ""
  echo "以下のいずれかの方法でモデルを指定してください:"
  echo " 1. 引数で直接指定: bash start_llama_server.sh /path/to/model.gguf"
  echo " 2. 環境変数で指定: MODEL_PATH=/path/to/model.gguf bash start_llama_server.sh"
  echo " 3. ~/model/ ディレクトリに .gguf ファイルを配置"
  echo ""
  echo "🔍 探索したパス:"
  for p in "${CANDIDATE_PATHS[@]}"; do
    echo " - $p"
  done
  exit 1
fi

if [ ! -x "$LLAMA_SERVER_BIN" ]; then
  echo "❌ [エラー] llama-server バイナリが見つかりません: $LLAMA_SERVER_BIN"
  echo "   llama.cpp をビルドするか、LLAMA_BIN_DIR環境変数を指定してください。"
  exit 1
fi

# 3. GPU オフロード層数とコンテキスト長（OOM防止のための最適化設定）
N_GPU_LAYERS="${N_GPU_LAYERS:-28}"
CTX_SIZE="${CTX_SIZE:-1024}"

echo " ⚙️ GPU オフロード: $N_GPU_LAYERS 層"
echo " ⚙️ コンテキスト長: $CTX_SIZE"

exec "$LLAMA_SERVER_BIN" \
  -m "$CHOSEN_MODEL" \
  --host 0.0.0.0 \
  --port 8080 \
  -ngl "$N_GPU_LAYERS" \
  -c "$CTX_SIZE" \
  -b 512 \
  -ub 256 \
  --flash-attn \
  --threads 4


