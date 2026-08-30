import json
import os
import subprocess
import threading
import time
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
from .broadcaster import broadcast_event, broadcast_process_status, broadcast_status
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


class SystemPowerRequest(BaseModel):
    action: str  # "shutdown" | "reboot"


def _execute_system_power(action: str):
    """Jetson Orin Nano Super のシャットダウン / 再起動を非同期実行 (多段フォールバック対応)"""
    time.sleep(1.2)  # クライアントへのレスポンス送信完了待ち

    # moOde 音楽再生の安全停止
    try:
        mpd_cli = mpd_client.get_mpd_client()
        if mpd_cli:
            mpd_cli.stop()
            mpd_cli.close()
            mpd_cli.disconnect()
            print("⏹️ [System Power] MPD 音楽再生を停止しました。", flush=True)
    except Exception as mpd_err:
        print(f"ℹ️ [System Power] MPD停止スキップ: {mpd_err}", flush=True)

    if os.name == "nt":
        print(f"🖥️ [System Power] Windows (開発環境) のためシミュレーション実行: {action}", flush=True)
        return

    success = False

    if action in ("shutdown", "poweroff"):
        print("⚡ [System Power] Jetson Orin Nano Super をシャットダウンします...", flush=True)
        commands = [
            # 1. systemd-logind D-Bus経由 (パスワードなしで実行可能な場合あり)
            ["dbus-send", "--system", "--print-reply", "--dest=org.freedesktop.login1", "/org/freedesktop/login1", "org.freedesktop.login1.Manager.PowerOff", "boolean:true"],
            # 2. systemctl non-interactive
            ["systemctl", "poweroff", "-i"],
            ["systemctl", "poweroff"],
            # 3. sudo non-interactive (NOPASSWD設定がある場合)
            ["sudo", "-n", "shutdown", "-h", "now"],
            ["sudo", "-n", "systemctl", "poweroff"],
            ["sudo", "-n", "poweroff"],
            # 4. 通常の sudo (tty/環境によるフォールバック)
            ["sudo", "shutdown", "-h", "now"],
            ["sudo", "poweroff"],
            ["sudo", "init", "0"],
        ]
    elif action == "reboot":
        print("🔄 [System Power] Jetson Orin Nano Super を再起動します...", flush=True)
        commands = [
            # 1. systemd-logind D-Bus経由 (パスワードなしで実行可能な場合あり)
            ["dbus-send", "--system", "--print-reply", "--dest=org.freedesktop.login1", "/org/freedesktop/login1", "org.freedesktop.login1.Manager.Reboot", "boolean:true"],
            # 2. systemctl non-interactive
            ["systemctl", "reboot", "-i"],
            ["systemctl", "reboot"],
            # 3. sudo non-interactive (NOPASSWD設定がある場合)
            ["sudo", "-n", "reboot"],
            ["sudo", "-n", "systemctl", "reboot"],
            # 4. 通常の sudo (tty/環境によるフォールバック)
            ["sudo", "reboot"],
            ["sudo", "systemctl", "reboot"],
            ["sudo", "init", "6"],
        ]
    else:
        print(f"⚠️ [System Power] 未知のアクション: {action}", flush=True)
        return

    for cmd in commands:
        cmd_str = " ".join(cmd)
        try:
            print(f"⚡ [System Power] 実行試行: '{cmd_str}'", flush=True)
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            if res.returncode == 0:
                print(f"✅ [System Power] コマンド成功: '{cmd_str}'", flush=True)
                success = True
                break
            else:
                err_msg = res.stderr.strip() or res.stdout.strip()
                print(f"ℹ️ [System Power] コマンドスキップ ({cmd_str}, code {res.returncode}): {err_msg}", flush=True)
        except Exception as ex:
            print(f"ℹ️ [System Power] コマンド実行エラー ({cmd_str}): {ex}", flush=True)

    if not success:
        print("❌ [System Power] すべての電源制御コマンドの実行に失敗しました。", flush=True)
        print("💡 Jetson 端末上で一度だけ以下のセットアップスクリプトを実行してください:", flush=True)
        print("   bash setup_sudo_power.sh", flush=True)


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
        "process_status": state.current_processing_state,
        "moode_ip": f"{config.MOODE_IP}:{config.MOODE_PORT}",
        "language": config.ANNOUNCE_LANGUAGE,
        "llm_model": config.LLM_MODEL,
        "enable_voice_listener": config.ENABLE_VOICE_LISTENER,
    })


@app.post("/api/player/control")
async def api_player_control(req: ControlRequest):
    """プレイヤーの直接操作 (play, pause, next, previous, stop, volume)"""
    cmd = {"action": req.action, "value": req.value}
    res = mpd_client.control_moode(cmd)
    broadcast_status()
    return JSONResponse({"result": res, "status": mpd_client.get_moode_status()})


@app.post("/api/system/power")
async def api_system_power(req: SystemPowerRequest):
    """Jetson Orin Nano Super のシャットダウン / 再起動を実行"""
    action = req.action.lower().strip()
    if action not in ("shutdown", "reboot", "poweroff"):
        return JSONResponse(
            {"success": False, "error": "Invalid action. Use 'shutdown' or 'reboot'."},
            status_code=400,
        )

    action_label = "シャットダウン (電源OFF)" if action in ("shutdown", "poweroff") else "再起動 (Reboot)"
    msg = f"⚡ Jetson Orin Nano Super を{action_label}します..."

    # チャット履歴とWebSocketに通知
    msg_record = {
        "sender": "assistant",
        "text": f"⚠️ システムコマンドを受信しました。{action_label}を実行します。",
        "source": "system",
        "action": action,
        "timestamp": time.strftime("%H:%M:%S"),
    }
    state.chat_history.append(msg_record)
    broadcast_event({
        "type": "system_power",
        "action": action,
        "message": msg,
        "chat_message": msg_record,
    })
    broadcast_process_status("idle", f"⚡ システム{action_label}中...")

    # バックグラウンドスレッドでシャットダウン/再起動を実行
    threading.Thread(target=_execute_system_power, args=(action,), daemon=True).start()

    return JSONResponse({
        "success": True,
        "action": action,
        "message": msg,
    })


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
                    "process_status": state.current_processing_state,
                    "language": config.ANNOUNCE_LANGUAGE,
                    "enable_voice_listener": config.ENABLE_VOICE_LISTENER,
                    "history": state.chat_history,
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
