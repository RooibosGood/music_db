#!/usr/bin/env bash
set -eu

LLAMA_BIN_DIR="$HOME/llama.cpp/build/bin"
MODEL_PATH="../model/google_gemma-4-E2B-it-Q4_K_M.gguf"

cd "$LLAMA_BIN_DIR"
exec ./llama-server -m "$MODEL_PATH" --port 8080
