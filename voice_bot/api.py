"""moOde 音声ボット FastAPI Web アプリケーション & エンドポイントモジュール。"""

import json
import os
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel

from . import config
from . import coverart
from . import mpd_client
from . import state
from .broadcaster import broadcast_status
from .llm import process_user_message


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield


app = FastAPI(title="moOde AI Master", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ChatRequest(BaseModel):
    message: str
    speak: Optional[bool] = True


class ControlRequest(BaseModel):
    action: str
    value: Optional[Any] = None


@app.get("/")
async def get_index():
    project_root = os.path.dirname(os.path.dirname(__file__))
    index_path = os.path.join(project_root, "web", "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return JSONResponse({"message": "moOde AI Master Backend is running."})


@app.post("/api/chat")
async def api_chat(req: ChatRequest):
    """Web Chat からのメッセージ受付"""
    user_text = req.message.strip()
    if not user_text:
        return JSONResponse({"error": "Empty message"}, status_code=400)

    res = process_user_message(user_text, source="chat", speak_voice=bool(req.speak))
    return JSONResponse(res)


@app.get("/api/status")
async def api_status():
    """現在の moOde 再生情報 & システム状態を取得"""
    player_status = mpd_client.get_moode_status()
    return JSONResponse({
        "player_status": player_status,
        "voice_status": state.voice_state,
        "moode_ip": f"{config.MOODE_IP}:{config.MOODE_PORT}",
        "language": config.ANNOUNCE_LANGUAGE,
        "llm_model": config.LLM_MODEL,
    })


@app.post("/api/player/control")
async def api_player_control(req: ControlRequest):
    """プレイヤーの直接操作 (play, pause, next, previous, stop, volume)"""
    cmd = {"action": req.action, "value": req.value}
    res = mpd_client.control_moode(cmd)
    broadcast_status()
    return JSONResponse({"result": res, "status": mpd_client.get_moode_status()})


@app.get("/api/player/cover")
async def api_player_cover(
    file: Optional[str] = None,
    artist: Optional[str] = None,
    album: Optional[str] = None,
    title: Optional[str] = None,
):
    """現在再生中楽曲または指定楽曲のアルバムジャケット画像（Cover Art）を取得"""
    if not file and not artist and not album and not title:
        status = mpd_client.get_moode_status()
        song = status.get("song", {})
        file = song.get("file", "")
        artist = song.get("artist", "")
        album = song.get("album", "")
        title = song.get("title", "")

    img_bytes, media_type = coverart.get_album_cover_bytes(
        song_file=file or "",
        artist=artist or "",
        album=album or "",
        title=title or "",
    )
    return Response(
        content=img_bytes,
        media_type=media_type,
        headers={"Cache-Control": "public, max-age=3600"},
    )


@app.get("/api/history")
async def api_history():
    """チャット履歴を取得"""
    return JSONResponse({"history": state.chat_history})


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    state.active_websockets.append(websocket)
    try:
        await websocket.send_text(
            json.dumps(
                {
                    "type": "status_update",
                    "player_status": mpd_client.get_moode_status(),
                    "voice_status": state.voice_state,
                },
                ensure_ascii=False,
            )
        )
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        if websocket in state.active_websockets:
            state.active_websockets.remove(websocket)
    except Exception:
        if websocket in state.active_websockets:
            state.active_websockets.remove(websocket)
