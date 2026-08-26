"""moOde 音声ボット デイリーインフォメーション（日付・天気・今日のエピソード）モジュール。

Open-Meteo API による天気取得、Wikipedia / Web検索による今日のエピソード取得、
および Ollama LLM を用いた自然なラジオDJ/アシスタント風ナレーション生成を提供します。
"""

import datetime
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional

from . import config
from .broadcaster import broadcast_process_status


# ==================== WMO 天気コード マッピング ====================
WMO_WEATHER_CODES: Dict[int, Dict[str, str]] = {
    0: {"en": "clear skies", "ja": "快晴"},
    1: {"en": "mainly clear", "ja": "概ね晴れ"},
    2: {"en": "partly cloudy", "ja": "晴れ時々曇り"},
    3: {"en": "overcast", "ja": "曇り"},
    45: {"en": "foggy", "ja": "霧"},
    48: {"en": "depositing rime fog", "ja": "濃霧"},
    51: {"en": "light drizzle", "ja": "弱い霧雨"},
    53: {"en": "moderate drizzle", "ja": "霧雨"},
    55: {"en": "dense drizzle", "ja": "濃い霧雨"},
    56: {"en": "light freezing drizzle", "ja": "着氷性の霧雨"},
    57: {"en": "dense freezing drizzle", "ja": "濃い着氷性の霧雨"},
    61: {"en": "slight rain", "ja": "小雨"},
    63: {"en": "moderate rain", "ja": "雨"},
    65: {"en": "heavy rain", "ja": "強い雨"},
    66: {"en": "light freezing rain", "ja": "着氷性の雨"},
    67: {"en": "heavy freezing rain", "ja": "激しい着氷性の雨"},
    71: {"en": "slight snow fall", "ja": "小雪"},
    73: {"en": "moderate snow fall", "ja": "雪"},
    75: {"en": "heavy snow fall", "ja": "大雪"},
    77: {"en": "snow grains", "ja": "霧雪"},
    80: {"en": "slight rain showers", "ja": "にわか雨"},
    81: {"en": "moderate rain showers", "ja": "通り雨"},
    82: {"en": "violent rain showers", "ja": "激しいにわか雨"},
    85: {"en": "slight snow showers", "ja": "にわか雪"},
    86: {"en": "heavy snow showers", "ja": "激しいにわか雪"},
    95: {"en": "thunderstorms", "ja": "雷雨"},
    96: {"en": "thunderstorms with slight hail", "ja": "雹を伴う雷雨"},
    99: {"en": "thunderstorms with heavy hail", "ja": "激しい雹を伴う雷雨"},
}


def get_ordinal_suffix(day: int) -> str:
    """日付けの序数サフィックス (1st, 2nd, 3rd, 4th...) を返す"""
    if 11 <= (day % 100) <= 13:
        return f"{day}th"
    suffix = {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return f"{day}{suffix}"


def format_current_date(language: str = "en") -> str:
    """今日の日付文字列を生成 (英語 / 日本語)"""
    now = datetime.datetime.now()
    if language == "ja":
        weekdays_ja = ["月", "火", "水", "木", "金", "土", "日"]
        weekday_str = weekdays_ja[now.weekday()]
        return f"{now.year}年{now.month}月{now.day}日 {weekday_str}曜日"
    else:
        weekday_str = now.strftime("%A")
        month_str = now.strftime("%B")
        day_str = get_ordinal_suffix(now.day)
        return f"{weekday_str}, {month_str} {day_str}, {now.year}"


def fetch_weather_forecast(
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    city_en: Optional[str] = None,
    city_ja: Optional[str] = None,
    timeout: float = 6.0,
) -> Dict[str, Any]:
    """Open-Meteo API から現在の天気と今日の予想気温を取得"""
    lat = latitude if latitude is not None else config.WEATHER_LATITUDE
    lon = longitude if longitude is not None else config.WEATHER_LONGITUDE
    city_name_en = city_en or config.WEATHER_CITY
    city_name_ja = city_ja or config.WEATHER_CITY_JA

    url = (
        f"https://api.open-meteo.com/v1/forecast?"
        f"latitude={lat}&longitude={lon}&"
        f"current=temperature_2m,weather_code&"
        f"daily=weather_code,temperature_2m_max,temperature_2m_min&"
        f"timezone={urllib.parse.quote(config.WEATHER_TIMEZONE)}"
    )

    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "moOde-AI-Master/1.0"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        current = data.get("current", {})
        daily = data.get("daily", {})

        current_temp = round(current.get("temperature_2m", 0.0))
        current_code = current.get("weather_code", 0)

        max_temps = daily.get("temperature_2m_max", [])
        min_temps = daily.get("temperature_2m_min", [])
        max_temp = round(max_temps[0]) if max_temps else current_temp
        min_temp = round(min_temps[0]) if min_temps else current_temp

        weather_desc = WMO_WEATHER_CODES.get(
            current_code, {"en": "fair weather", "ja": "晴れ"}
        )
        condition_en = weather_desc["en"]
        condition_ja = weather_desc["ja"]

        summary_en = (
            f"In {city_name_en}, today's weather is {condition_en} with a high of {max_temp}°C "
            f"and a low of {min_temp}°C (currently {current_temp}°C)."
        )
        summary_ja = (
            f"{city_name_ja}の現在の天気は{condition_ja}、気温は{current_temp}度です。"
            f"今日の予想最高気温は{max_temp}度、最低気温は{min_temp}度です。"
        )

        return {
            "success": True,
            "city_en": city_name_en,
            "city_ja": city_name_ja,
            "current_temp": current_temp,
            "max_temp": max_temp,
            "min_temp": min_temp,
            "condition_en": condition_en,
            "condition_ja": condition_ja,
            "summary_en": summary_en,
            "summary_ja": summary_ja,
        }

    except Exception as e:
        print(f"⚠️ [daily_info] 天気情報の取得に失敗しました: {e}", flush=True)
        return {
            "success": False,
            "city_en": city_name_en,
            "city_ja": city_name_ja,
            "summary_en": f"The weather in {city_name_en} today looks pleasant.",
            "summary_ja": f"本日の{city_name_ja}の天気は穏やかな一日となりそうです。",
        }


def fetch_today_episode(language: str = "en", timeout: float = 6.0) -> Dict[str, Any]:
    """今日（月日）にちなんだ歴史的出来事・音楽エピソード・記念日をWeb/Wikipediaから取得"""
    now = datetime.datetime.now()
    month = now.month
    day = now.day

    if language == "en":
        # Wikipedia On This Day API
        url = f"https://en.wikipedia.org/api/rest_v1/feed/onthisday/all/{month:02d}/{day:02d}"
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "moOde-AI-Master/1.0 (https://github.com/moode-ai)"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            # 音楽やアート、カルチャー関連のイベントを優先的に検索
            music_keywords = [
                "music", "song", "album", "band", "singer", "beatles", "jazz",
                "rock", "symphony", "composer", "concert", "orchestra", "grammy",
                "guitar", "piano", "recorded", "released"
            ]

            all_events = data.get("selected", []) + data.get("events", [])
            chosen_event = None

            # 1. 音楽関連イベントを探す
            for ev in all_events:
                ev_text = ev.get("text", "")
                if any(kw in ev_text.lower() for kw in music_keywords):
                    year = ev.get("year", "")
                    year_str = f"in {year}" if year else ""
                    chosen_event = f"On this day {year_str}, {ev_text}"
                    break

            # 2. 音楽関連がなければ注目の歴史イベント (selected または events)
            if not chosen_event and all_events:
                ev = all_events[0]
                year = ev.get("year", "")
                year_str = f"in {year}" if year else ""
                chosen_event = f"On this day {year_str}, {ev.get('text', '')}"

            # 3. 記念日 (holidays) のチェック
            holidays = data.get("holidays", [])
            holiday_text = ""
            if holidays:
                h_name = holidays[0].get("text", "")
                if h_name:
                    holiday_text = f"Today is also celebrated as {h_name}."

            episode_summary = chosen_event or holiday_text or "Today is a wonderful day to enjoy your favorite music."
            return {
                "success": True,
                "episode": episode_summary,
                "holiday": holiday_text,
            }

        except Exception as e:
            print(f"⚠️ [daily_info] 英語エピソードの取得に失敗しました: {e}", flush=True)
            return {
                "success": False,
                "episode": "Today is a great day filled with timeless melodies and good vibes.",
                "holiday": "",
            }

    else:
        # 日本語: Wikipedia API (X月X日の概要・記念日)
        title_str = f"{month}月{day}日"
        encoded_title = urllib.parse.quote(title_str)
        url = (
            f"https://ja.wikipedia.org/w/api.php?action=query&prop=extracts&explaintext=1&"
            f"exintro=1&titles={encoded_title}&format=json"
        )
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "moOde-AI-Master/1.0"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            pages = data.get("query", {}).get("pages", {})
            extract = ""
            for _, page_info in pages.items():
                extract = page_info.get("extract", "")
                if extract:
                    break

            episode_text = ""
            if extract:
                # 概要文を整形（不要な行や導入部を整理）
                lines = [line.strip() for line in extract.splitlines() if line.strip()]
                # 記念日や出来事に関わる行を探す
                for line in lines:
                    if any(k in line for k in ["記念日", "日", "制定", "出来事", "周年", "誕生"]):
                        if len(line) > 10 and not line.startswith("=="):
                            episode_text = line
                            break
                if not episode_text and lines:
                    episode_text = lines[0]

            if not episode_text:
                episode_text = f"{month}月{day}日は、新しい音楽の発見にぴったりの素敵な一日です。"

            return {
                "success": True,
                "episode": episode_text,
            }

        except Exception as e:
            print(f"⚠️ [daily_info] 日本語エピソードの取得に失敗しました: {e}", flush=True)
            return {
                "success": False,
                "episode": f"{month}月{day}日は、素晴らしい音楽とともに過ごす最高の一日です。",
            }


def generate_daily_intro(language: str = "en") -> str:
    """日付・天気・今日のエピソードを統合し、LLM またはフォールバックで自然なナレーション文を生成"""
    broadcast_process_status("info", "🌐 今日の天気・日付・Webエピソードを取得中...")

    date_str = format_current_date(language)
    weather_info = fetch_weather_forecast()
    episode_info = fetch_today_episode(language)

    weather_summary = (
        weather_info.get("summary_en", "")
        if language == "en"
        else weather_info.get("summary_ja", "")
    )
    episode_text = episode_info.get("episode", "")

    # Ollama LLM を使って自然で洗練されたナレーションを生成
    broadcast_process_status("llm", "🤖 起動アナウンスを生成中 (Ollama)...")

    if language == "en":
        system_prompt = (
            "You are a charismatic, smooth, and friendly FM radio DJ on an AI music station. "
            "Given today's date, weather summary, and an interesting episode or anniversary, "
            "write a concise, engaging 2-3 sentence introductory talk that flows smoothly. "
            "Do NOT include greetings like 'Hello' or 'Hi' since they have already been spoken. "
            "Directly state the date and weather in a warm tone, mention the trivia/episode, "
            "and build anticipation for good music. Keep it under 50 words."
        )
        user_prompt = (
            f"Date: {date_str}\n"
            f"Weather: {weather_summary}\n"
            f"Episode/Trivia: {episode_text}\n\n"
            "Please generate the smooth radio DJ announcement talk."
        )
    else:
        system_prompt = (
            "あなたは親しみやすく落ち着いた声の moOde AI 音楽アシスタントです。"
            "今日の日付、天気情報、および今日にちなんだ出来事や記念日のエピソードをもとに、"
            "最初の挨拶に自然に続く、聞き取りやすく心地よい2〜3文のオープニングトークを作成してください。"
            "「こんにちは」などの冒頭の挨拶は既に発話済みのため含めず、"
            "日付と天気、そして今日のエピソードを紹介し、音楽を楽しむ気持ちを盛り上げる言葉で結んでください。"
            "100文字〜150文字程度で簡潔に出力してください。"
        )
        user_prompt = (
            f"日付: {date_str}\n"
            f"天気: {weather_summary}\n"
            f"今日のエピソード: {episode_text}\n\n"
            "自然な続きのナレーション文を生成してください。"
        )

    payload = {
        "model": config.LLM_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "stream": False,
        "think": False,
        "keep_alive": "5m",
        "options": {
            "num_ctx": 2048,
            "temperature": 0.5,
            "num_predict": 180,
        },
    }

    try:
        json_bytes = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            config.OLLAMA_CHAT_URL,
            data=json_bytes,
            headers={"Content-Type": "application/json", "User-Agent": "moOde-AI-Master/1.0"},
        )
        with urllib.request.urlopen(req, timeout=12.0) as response:
            resp_data = json.loads(response.read().decode("utf-8"))
            content = resp_data.get("message", {}).get("content", "").strip()

            # タグやMarkdownのクリーンアップ
            cleaned = re.sub(r"<think>[\s\S]*?</think>", "", content).strip()
            cleaned = cleaned.replace("```", "").replace("\n", " ").strip()

            if cleaned and len(cleaned) >= 15:
                print(f"🎙️ [daily_info] LLM生成ナレーション: {cleaned}", flush=True)
                return cleaned

    except Exception as e:
        print(f"⚠️ [daily_info] Ollama ナレーション生成スキップ/フォールバック: {e}", flush=True)

    # フォールバックテンプレート合成（Ollamaがオフラインまたはタイムアウト時）
    if language == "en":
        return f"Today is {date_str}. {weather_summary} By the way, {episode_text}"
    else:
        return f"本日は{date_str}です。{weather_summary} ちなみに、{episode_text}"
