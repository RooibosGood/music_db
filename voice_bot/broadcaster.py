"""moOde 音声ボット WebSocket リアルタイム配信モジュール。"""

import asyncio
import json
import threading
import time
from typing import Any, Dict, Optional

from . import config
from . import mpd_client
from . import state


def broadcast_event(data: Dict[str, Any]):
    """接続中の全WebSocketにイベントを非同期送信"""
    if not state.active_websockets:
        return
    loop = None
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        pass

    if loop and loop.is_running():
        asyncio.run_coroutine_threadsafe(_async_broadcast(data), loop)
    else:
        threading.Thread(target=lambda: asyncio.run(_async_broadcast(data)), daemon=True).start()


async def _async_broadcast(data: Dict[str, Any]):
    msg_str = json.dumps(data, ensure_ascii=False)
    disconnected = []
    for ws in state.active_websockets:
        try:
            await ws.send_text(msg_str)
        except Exception:
            disconnected.append(ws)
    for ws in disconnected:
        if ws in state.active_websockets:
            state.active_websockets.remove(ws)


def broadcast_process_status(step: str, detail: str, auto_idle_sec: Optional[float] = None):
    """処理進行状況をリアルタイムで WebSocket クライアントに通知 (Web画面でのステータス表示用)"""
    state.current_processing_state = {
        "step": step,
        "detail": detail,
        "timestamp": time.time(),
    }
    print(f"⚡ [Process Status] [{step.upper()}] {detail}", flush=True)
    broadcast_event({
        "type": "process_status",
        "step": step,
        "detail": detail,
        "timestamp": time.time(),
    })

    if auto_idle_sec:
        def _reset_to_idle():
            time.sleep(auto_idle_sec)
            if state.current_processing_state.get("step") == step:
                broadcast_process_status("idle", "音声待機中 (「ヘイ、マスター」)")
        threading.Thread(target=_reset_to_idle, daemon=True).start()


def broadcast_status():
    """現在の moOde 再生状態、音声ステータス、処理ステータス、および言語モードをプッシュ"""
    player_status = mpd_client.get_moode_status()
    broadcast_event({
        "type": "status_update",
        "player_status": player_status,
        "voice_status": state.voice_state,
        "process_status": state.current_processing_state,
        "language": config.ANNOUNCE_LANGUAGE,
    })
