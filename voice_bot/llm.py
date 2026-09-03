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

    system_prompt = """あなたは音楽再生AIアシスタントです。ユーザーの要望（日本語・英語）を解釈し、moOde audioの操作コマンドと自然な返答を生成してください。
出力は必ず、説明文やMarkdownを含まない1つのJSONオブジェクトだけにしてください。

【出力形式】
{"action":"play_search"|"play"|"pause"|"stop"|"next"|"previous"|"rate_good"|"rate_bad"|"unknown","query":"検索語","reply":"返答"}

【ルール】
- 曲やジャンル、アーティストの指定・再生要求 ➔ action: "play_search", query: "検索対象(例: ジャズ, ロック, Diana Krall, ビートルズ, ハイレゾ, 静かな曲, Ambientなど)", reply: "〜を再生します。/ Playing jazz now."
  例:
  - "play jazz" / "jazz" / "ジャズかけて" ➔ {"action":"play_search","query":"ジャズ","reply":"ジャズを再生します。"}
  - "play Beatles" / "ビートルズ流して" ➔ {"action":"play_search","query":"Beatles","reply":"ビートルズを再生します。"}
  - "play something calm" / "静かな曲" ➔ {"action":"play_search","query":"静か","reply":"落ち着いた曲を再生します。"}
  - "play music" / "何か音楽かけて" ➔ {"action":"play_search","query":"","reply":"おすすめの音楽を再生します。"}
- 再生中楽曲の評価（グッド・いいね・高評価） ➔ action: "rate_good", query: "", reply: "この曲を高評価しました。"
- 再生中楽曲の評価（バッド・低評価・いまいち） ➔ action: "rate_bad", query: "", reply: "この曲に低評価をつけました。"
- 単なる再生再開（曲指定なしの「再生」「再開」「play」） ➔ action: "play", query: "", reply: "音楽の再生を再開します。"
- 「止めて」「一時停止」「ストップ」「pause」「stop」 ➔ action: "pause", query: "", reply: "音楽を一時停止します。"
- 「次の曲」「スキップ」「next」「skip」 ➔ action: "next", query: "", reply: "次の曲を再生します。"
- 「前の曲」「戻って」「previous」「prev」「back」 ➔ action: "previous", query: "", reply: "前の曲に戻ります。"
- 雑談や一般的な質問 ➔ action: "unknown", query: "", reply: "内容に応じた親切で自然な返答"
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
                    # 正規化・補正: "play" で query が入っている場合は "play_search" に補正
                    act = command.get("action", "unknown")
                    q = str(command.get("query", "")).strip()
                    if act == "play" and q:
                        command["action"] = "play_search"
                    elif act == "play_search":
                        # query から余計な "play" プレフィックスを除去
                        cleaned_q = re.sub(r"^(?:play\s+(?:some\s+|me\s+)?|put\s+on\s+|listen\s+to\s+)", "", q, flags=re.IGNORECASE).strip()
                        command["query"] = cleaned_q or q
                    return command
            except json.JSONDecodeError:
                continue

    except Exception as e:
        print(f"⚠️ [LLM] llama.cpp接続エラー/フォールバック: {e}")

    # フォールバック（キーワードベースの日英両対応判定）
    lower_text = user_text.lower().strip()

    # 1. 停止・一時停止
    if any(k in lower_text for k in ["止め", "ストップ", "停止", "stop", "pause", "halt", "quiet", "shut up"]):
        return {"action": "pause", "query": "", "reply": "音楽を停止します。"}

    # 2. スキップ・次の曲
    if any(k in lower_text for k in ["次", "スキップ", "next", "skip"]):
        return {"action": "next", "query": "", "reply": "次の曲を再生します。"}

    # 3. 前の曲・戻る
    if any(k in lower_text for k in ["前", "戻っ", "previous", "prev", "back"]):
        return {"action": "previous", "query": "", "reply": "前の曲に戻ります。"}

    # 4. 楽曲評価（グッド / バッド）
    if any(k in lower_text for k in ["いいね", "グッド", "高評価", "すき", "好き", "good", "like", "thumbs up", "great song"]):
        return {"action": "rate_good", "query": "", "reply": "この曲を高評価しました。"}
    if any(k in lower_text for k in ["バッド", "低評価", "いまいち", "微妙", "嫌い", "bad", "dislike", "thumbs down", "poor"]):
        return {"action": "rate_bad", "query": "", "reply": "この曲に低評価をつけました。"}

    # 5. 単なる再生再開（play / 再生 / スタート）
    if lower_text in ["play", "resume", "start", "再生", "再開", "スタート"]:
        return {"action": "play", "query": "", "reply": "音楽の再生を再開します。"}

    # 6. 選曲・再生リクエスト（日英対応）
    play_triggers = [
        "かけて", "流して", "再生", "聴きたい", "聴かせて", "play", "put on", "listen to",
        "ジャズ", "jazz", "ロック", "rock", "クラシック", "classic", "ポップ", "pop",
        "ブルース", "blues", "ハイレゾ", "hires", "hi-res", "静か", "落ち着", "calm",
        "リラックス", "relax", "cafe", "カフェ", "喫茶", "癒し", "バラード", "upbeat", "ノリ"
    ]
    if any(k in lower_text for k in play_triggers):
        query = user_text
        # 日本語助詞の除去
        for sw in ["をかけて", "を流して", "を再生して", "かけて", "流して", "再生して", "聴かせて", "聴きたい"]:
            query = query.replace(sw, "")
        # 英語プレフィックス・サフィックスの除去
        query = re.sub(r"^(?:play\s+(?:some\s+|me\s+)?|put\s+on\s+|listen\s+to\s+)", "", query, flags=re.IGNORECASE)
        query = re.sub(r"\s+(?:please|music|song|songs|track|tracks)$", "", query, flags=re.IGNORECASE)
        query = query.strip()

        display_q = query or "おすすめの曲"
        return {"action": "play_search", "query": query, "reply": f"{display_q} を再生します。"}

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

    # 楽曲評価アクションの場合はメッセージを最適化
    if cmd.get("action") in ("rate_good", "rate_bad") and control_res.get("rating_result"):
        rate_info = control_res["rating_result"]
        if rate_info.get("success"):
            new_r = rate_info.get("rating")
            t_name = rate_info.get("title", "この曲")
            if config.ANNOUNCE_LANGUAGE == "en":
                reply_text = f"Rated '{t_name}' with {new_r} stars."
            else:
                reply_text = f"『{t_name}』を ★{new_r} に評価しました。"

    # 3. 再生・スキップ時、曲紹介文を構築
    if cmd.get("action") in ("play_search", "next", "previous") and control_res.get("success"):
        selected_tracks = control_res.get("selected_tracks_meta") or []
        is_play_search = (cmd.get("action") == "play_search")

        if config.ANNOUNCE_LANGUAGE == "en":
            if is_play_search and selected_tracks:
                reply_text = tts.build_playlist_overview_announcement(
                    selected_tracks=selected_tracks,
                    query=cmd.get("query", ""),
                    first_track=track_info,
                    language="en",
                )
            else:
                is_skip = cmd.get("action") in ("next", "previous")
                reply_text = tts.build_english_track_announcement(track_info, is_next=False, is_skip=is_skip)
            print(f"🎙️ [English DJ ナレーション] {reply_text}", flush=True)
        else:
            if is_play_search and selected_tracks:
                reply_text = tts.build_playlist_overview_announcement(
                    selected_tracks=selected_tracks,
                    query=cmd.get("query", ""),
                    first_track=track_info,
                    language="ja",
                )
            else:
                prefix = "次の曲、" if cmd.get("action") == "next" else ("前の曲、" if cmd.get("action") == "previous" else "")
                reply_text = tts.build_japanese_track_announcement(track_info, description=description, prefix=prefix)
            print(f"📖 [音声案内テキスト] {reply_text}", flush=True)

    # 4. 音声読み上げと moOde 音楽再生の順序制御（先に曲をセット ➔ 曲紹介アナウンス ➔ 発話完了後に再生開始）
    needs_playback = control_res.get("needs_playback", False)
    select_start_time = time.time()

    def start_moode_playback():
        """moOde の音楽再生を開始 (mpd_client.safe_start_playback)"""
        mpd_cli = mpd_client.get_mpd_client()
        if mpd_cli:
            try:
                broadcast_process_status("playing", "▶️ moOde 音楽再生をスタートしました", auto_idle_sec=3.5)
                mpd_client.safe_start_playback(mpd_cli)
                mpd_cli.close()
                mpd_cli.disconnect()
                print("▶️ [moOde] 音楽再生を開始しました (ReplayGain 適用済み)。", flush=True)
                broadcast_status()
            except Exception as e:
                print(f"⚠️ [moOde] 再生開始エラー: {e}")

    def trigger_playback_start(select_time: float):
        """音声発話なし時の ReplayGain 反映待ち（PLAY_DELAY_SEC秒）を確保して moOde の音楽再生を開始"""
        elapsed = time.time() - select_time
        remaining_delay = max(0.0, config.PLAY_DELAY_SEC - elapsed)
        if remaining_delay > 0.1:
            broadcast_process_status("playing", f"⏳ ReplayGain 適用待機中 ({remaining_delay:.1f}秒)...")
            print(f"⏳ [moOde] ReplayGain 反映待機中: {remaining_delay:.1f}秒 スリープ...", flush=True)
            time.sleep(remaining_delay)
        start_moode_playback()

    if speak_voice:
        def speak_and_play_flow():
            try:
                if config.ANNOUNCE_LANGUAGE == "en" and cmd.get("action") in ("play_search", "next", "previous"):
                    broadcast_process_status("tts", "🎙️ DJ英語曲紹介音声を生成・再生中 (edge-tts)...")
                    tts.speak_english(reply_text)
                else:
                    broadcast_process_status("tts", "🎙️ 曲紹介音声を合成・再生中 (VOICEVOX)...")
                    tts.speak(reply_text)
            except Exception as tts_err:
                print(f"⚠️ [TTS] 発話処理エラー (再生は継続): {tts_err}")
            finally:
                if needs_playback:
                    # 曲紹介アナウンスが完全に終了した直後に音楽再生をスタート！
                    print("🎙️ [TTS] 曲紹介アナウンス完了 ➔ 音楽再生を開始します。", flush=True)
                    start_moode_playback()
                else:
                    broadcast_process_status("idle", "🎙️ 音声待機中 (「ヘイ、マスター」)")

        threading.Thread(target=speak_and_play_flow, daemon=True).start()
    else:
        # 音声読み上げなしの場合（Webチャット等）は非同期スレッドで3秒待機後に再生開始
        if needs_playback:
            threading.Thread(target=trigger_playback_start, args=(select_start_time,), daemon=True).start()
        else:
            broadcast_process_status("idle", "🎙️ 音声待機中 (「ヘイ、マスター」)")

    # 5. 履歴に追加
    msg_record = state.create_chat_message(
        sender="assistant",
        text=reply_text,
        source=source,
        action=cmd.get("action"),
        query=cmd.get("query"),
        track_info=track_info,
        description=description,
        tracks_added=control_res.get("tracks_added", []),
    )
    state.append_chat_message(msg_record)

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
        "message": msg_record,
    }
