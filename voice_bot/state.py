"""moOde 音声ボット共有状態モジュール。"""

import time
import uuid
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


def create_chat_message(
    sender: str,
    text: str,
    source: str = "chat",
    action: Optional[str] = None,
    query: Optional[str] = None,
    track_info: Optional[Dict[str, Any]] = None,
    description: Optional[str] = None,
    tracks_added: Optional[List[Any]] = None,
    msg_id: Optional[str] = None,
) -> Dict[str, Any]:
    """一意なIDを持つチャットメッセージレコードを生成"""
    return {
        "id": msg_id or f"msg_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}",
        "sender": sender,
        "text": text,
        "source": source,
        "action": action,
        "query": query,
        "track_info": track_info or {},
        "description": description or "",
        "tracks_added": tracks_added or [],
        "timestamp": time.strftime("%H:%M:%S"),
    }


def append_chat_message(msg_record: Dict[str, Any]) -> bool:
    """重複を排除してチャット履歴に追加"""
    msg_id = msg_record.get("id")
    if msg_id:
        for m in chat_history[-30:]:
            if m.get("id") == msg_id:
                return False

    # 直近のメッセージと送信者・テキストが完全一致する場合は重複とみなしてスキップ
    if chat_history:
        last = chat_history[-1]
        if last.get("sender") == msg_record.get("sender") and last.get("text") == msg_record.get("text"):
            return False

    chat_history.append(msg_record)
    # 履歴上限の維持（最大200件）
    if len(chat_history) > 200:
        chat_history.pop(0)
    return True


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

