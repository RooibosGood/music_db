"""moOde 音声ボット共有状態モジュール。"""

import time
from typing import Any, Dict, List, Optional
from fastapi import WebSocket


# ==================== チャット・WebSocket 状態 ====================
chat_history: List[Dict[str, Any]] = []
active_websockets: List[WebSocket] = []

voice_state: Dict[str, Any] = {
    "is_listening": False,
    "state": "idle",
    "last_text": "",
    "error": None,
}

current_processing_state: Dict[str, Any] = {
    "step": "idle",
    "detail": "音声待機中 (「ヘイ、マスター」)",
    "timestamp": time.time(),
}


def is_same_track(
    file_a: Optional[str],
    file_b: Optional[str],
    id_a: Optional[str] = None,
    id_b: Optional[str] = None,
) -> bool:
    """2つのトラック情報が同一曲かどうかを判定（MPD ID、フルパス、相対パス、ファイル名で比較）"""
    if id_a and id_b and str(id_a).strip() == str(id_b).strip() and str(id_a).strip() != "":
        return True
    if not file_a or not file_b:
        return False
    norm_a = file_a.replace("\\", "/").rstrip("/")
    norm_b = file_b.replace("\\", "/").rstrip("/")
    if norm_a == norm_b:
        return True
    if norm_a.endswith(norm_b) or norm_b.endswith(norm_a):
        return True
    base_a = norm_a.split("/")[-1]
    base_b = norm_b.split("/")[-1]
    return base_a == base_b and base_a != ""
