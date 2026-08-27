"""moOde 音声ボット LLM 意図解析・メッセージ処理モジュール。"""

import json
import re
import threading
import time
import urllib.request
from typing import Any, Dict, Optional

from . import config
from . import mpd_client
from . import state
from . import tts
from .broadcaster import (
    broadcast_event,
    broadcast_process_status,
    broadcast_status,
)


def http_post_json(url: str, data: Dict[str, Any], timeout: float = 30.0) -> Dict[str, Any]:
    """JSONペイロードをPOSTしてJSONレスポンスを返す"""
    json_bytes = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=json_bytes,
        headers={"Content-Type": "application/json", "User-Agent": "moOde-AI-Master/1.0"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        resp_data = response.read().decode("utf-8")
        return json.loads(resp_data)


def parse_intent_with_llm(user_text: str) -> Dict[str, Any]:
    """テキスト ➔ 意図抽出 (LLM - JSON構造化)"""
    started_at = time.monotonic()
    print(f"🤖 [LLM] 解析要求: '{user_text}'", flush=True)
    broadcast_process_status("llm", f"🤖 AIが選曲・意図を解釈中 ({config.LLM_MODEL}): 「{user_text}」")

    system_prompt = """あなたは音楽再生AIアシスタントです。ユーザーの要望を解釈し、moOde audioの操作コマンドと自然な日本語の返答を生成してください。
出力は必ず、説明文やMarkdownを含まない1つのJSONオブジェクトだけにしてください。

【出力形式】
{"action":"play_search"|"pause"|"stop"|"next"|"previous"|"unknown","query":"検索語","reply":"日本語返答"}

【ルール】
- 「〜をかけて」「〜を流して」「Jazz」「静かな曲」「ハイレゾ」 ➔ action: "play_search", query: "検索語(ジャズ/ロック/アーティスト名/Ambientなど)", reply: "〜を再生します。"
- 「止めて」「一時停止」「ストップ」 ➔ action: "pause", query: "", reply: "音楽を一時停止します。"
- 「次の曲」「スキップ」 ➔ action: "next", query: "", reply: "次の曲を再生します。"
- 「前の曲」「戻って」 ➔ action: "previous", query: "", reply: "前の曲に戻ります。"
- 雑談や一般的な質問 ➔ action: "unknown", query: "", reply: "内容に応じた親切で自然な日本語返答"
"""

    payload = {
        "model": config.LLM_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ],
        "stream": False,
        "temperature": 0,
        "max_tokens": 192,
        "response_format": {"type": "json_object"},
    }

    try:
        try:
            response_json = http_post_json(config.LLAMA_CPP_CHAT_URL, payload, timeout=60)
        except Exception as post_err:
            # response_format や system ロール非対応環境向けの再試行
            payload_fallback = {
                "model": config.LLM_MODEL,
                "messages": [
                    {"role": "user", "content": f"{system_prompt}\n\nユーザーリクエスト: {user_text}"},
                ],
                "stream": False,
                "temperature": 0,
                "max_tokens": 192,
            }
            response_json = http_post_json(config.LLAMA_CPP_CHAT_URL, payload_fallback, timeout=60)

        message = response_json.get("choices", [{}])[0].get("message", {})
        response_text = message.get("content", "").strip()
        print(f"🤖 [LLM] 応答受信（{time.monotonic() - started_at:.1f}秒）: {response_text}", flush=True)

        cleaned_text = response_text.replace("```json", "").replace("```", "")
        cleaned_text = re.sub(r"<think>[\s\S]*?</think>", "", cleaned_text).strip()
        decoder = json.JSONDecoder()
        for start, character in enumerate(cleaned_text):
            if character != "{":
                continue
            try:
                command, _ = decoder.raw_decode(cleaned_text[start:])
                if isinstance(command, dict):
                    return command
            except json.JSONDecodeError:
                continue

    except Exception as e:
        print(f"⚠️ [LLM] llama.cpp接続エラー/フォールバック: {e}")

    # フォールバック（キーワードベースの簡易判定）
    if any(k in user_text for k in ["止め", "ストップ", "停止"]):
        return {"action": "pause", "query": "", "reply": "音楽を停止します。"}
    elif any(k in user_text for k in ["次", "スキップ"]):
        return {"action": "next", "query": "", "reply": "次の曲を再生します。"}
    elif any(k in user_text for k in ["前", "戻っ"]):
        return {"action": "previous", "query": "", "reply": "前の曲に戻ります。"}
    elif any(k in user_text for k in ["かけて", "流して", "再生", "聴きたい", "ジャズ", "jazz", "ロック", "クラシック", "ポップ"]):
        query = (
            user_text.replace("をかけて", "")
            .replace("を流して", "")
            .replace("を再生して", "")
            .replace("かけて", "")
            .replace("流して", "")
            .replace("再生して", "")
            .strip()
        )
        return {"action": "play_search", "query": query or user_text, "reply": f"{query or user_text} を再生します。"}

    return {
        "action": "unknown",
        "query": "",
        "reply": "ご用件を承りました。音楽のリクエストやご質問をどうぞ。",
    }


def process_user_message(
    user_text: str,
    source: str = "chat",
    speak_voice: bool = True,
) -> Dict[str, Any]:
    """音声・Web Chat両方からのメッセージを処理するコア関数 (description読み上げ対応)"""
    print(f"\n📩 [Request] 処理開始 (from {source}): '{user_text}'", flush=True)
    broadcast_process_status("llm", f"🤖 リクエスト処理開始: 「{user_text}」")

    # 1. LLMによる意図抽出
    cmd = parse_intent_with_llm(user_text)

    # 2. moOde (MPD) 操作 & DB解説取得
    control_res = mpd_client.control_moode(cmd)

    reply_text = cmd.get("reply", "承知いたしました。")
    description = control_res.get("description", "")
    track_info = control_res.get("track_info") or {}

    # 3. 再生・スキップ時、曲紹介文を構築
    if cmd.get("action") in ("play_search", "next", "previous") and control_res.get("success"):
        if config.ANNOUNCE_LANGUAGE == "en":
            is_skip = cmd.get("action") in ("next", "previous")
            reply_text = tts.build_english_track_announcement(track_info, is_next=False, is_skip=is_skip)
            print(f"🎙️ [English DJ ナレーション] {reply_text}", flush=True)
        else:
            prefix = "次の曲、" if cmd.get("action") == "next" else ("前の曲、" if cmd.get("action") == "previous" else "")
            reply_text = tts.build_japanese_track_announcement(track_info, description=description, prefix=prefix)
            print(f"📖 [音声案内テキスト] {reply_text}", flush=True)

    # 4. 音声読み上げと moOde 音楽再生の順序制御（解説文を話し終えてから再生）
    needs_playback = control_res.get("needs_playback", False)

    def trigger_playback_start():
        """発話完了後に moOde の音楽再生を開始"""
        mpd_cli = mpd_client.get_mpd_client()
        if mpd_cli:
            try:
                broadcast_process_status("playing", "▶️ moOde 音楽再生をスタートしました", auto_idle_sec=3.5)
                mpd_cli.play()
                mpd_cli.close()
                mpd_cli.disconnect()
                print("▶️ [moOde] 音声案内完了後に音楽再生を開始しました。", flush=True)
                broadcast_status()
            except Exception as e:
                print(f"⚠️ [moOde] 再生開始エラー: {e}")

    if speak_voice:
        def speak_and_play_flow():
            if config.ANNOUNCE_LANGUAGE == "en" and cmd.get("action") in ("play_search", "next", "previous"):
                broadcast_process_status("tts", "🎙️ DJ英語曲紹介音声を生成・再生中 (edge-tts)...")
                tts.speak_english(reply_text)
            else:
                broadcast_process_status("tts", "🎙️ 曲紹介音声を合成・再生中 (VOICEVOX)...")
                tts.speak(reply_text)
            if needs_playback:
                trigger_playback_start()
            else:
                broadcast_process_status("idle", "🎙️ 音声待機中 (「ヘイ、マスター」)")

        threading.Thread(target=speak_and_play_flow, daemon=True).start()
    else:
        # 音声読み上げなしの場合は即座に再生
        if needs_playback:
            trigger_playback_start()
        else:
            broadcast_process_status("idle", "🎙️ 音声待機中 (「ヘイ、マスター」)")

    # 5. 履歴に追加
    msg_record = {
        "sender": "assistant",
        "text": reply_text,
        "source": source,
        "action": cmd.get("action"),
        "query": cmd.get("query"),
        "track_info": track_info,
        "description": description,
        "tracks_added": control_res.get("tracks_added", []),
        "timestamp": time.strftime("%H:%M:%S"),
    }
    state.chat_history.append(msg_record)

    # 6. 全 WebSocket クライアントにブロードキャスト
    broadcast_event({
        "type": "chat_message",
        "message": msg_record,
    })

    # 最新ステータスもプッシュ
    broadcast_status()

    return {
        "action": cmd.get("action"),
        "query": cmd.get("query"),
        "reply": reply_text,
        "description": description,
        "track_info": track_info,
        "tracks_added": control_res.get("tracks_added", []),
        "control_success": control_res.get("success", False),
    }
