"""moOde 音声ボット デイリーインフォメーション（日付・天気・今日の音楽トピックス）モジュール。

Open-Meteo API による天気取得、Wikipedia による今日にちなんだ音楽トピックス（アーティスト生誕・名盤リリース・音楽史の出来事）取得、
および llama-server (llama.cpp) を用いた自然なラジオDJ/アシスタント風音楽オープニングトーク生成を提供します。
"""

import datetime
import json
import random
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

from . import config
from .broadcaster import broadcast_process_status


# ==================== 音楽トピックス抽出用キーワード・正規表現 ====================
MUSIC_KEYWORDS_JA: List[str] = [
    "音楽", "楽曲", "名曲", "ヒット曲", "アルバム",
    "歌手", "ミュージシャン", "シンガー", "ボーカル", "作曲家", "作詞家",
    "指揮者", "ピアニスト", "ギタリスト", "ベーシスト", "ドラマー", "バイオリニスト",
    "チェリスト", "サックス奏者", "トランペット奏者", "オーケストラ", "交響楽団",
    "ロックバンド", "ジャズ", "クラシック音楽", "ポップス",
    "コンサート", "ライブツアー", "音楽祭", "音楽賞", "グラミー賞", "日本レコード大賞",
    "オリコン", "ビルボード", "ビートルズ", "モーツァルト", "ベートーヴェン", "ショパン",
    "バッハ", "ブラームス", "チャイコフスキー", "エルヴィス", "クイーン"
]

MUSIC_PHRASES_JA: List[str] = [
    r"シングル(曲|盤|CD|発売|リリース)",
    r"レコード(大賞|会社|レーベル|盤|発売|デビュー|録音|プレイヤー|コンサート)",
    r"バンド(活動|結成|解散|演奏|リーダー)",
    r"ライブ(ハウス|ツアー|公演|イベント|ステージ)"
]

EXCLUDE_PATTERNS_JA: List[str] = [
    r"シングルス", r"ロックスプリングス", r"ロッククライミング", r"ロックダウン",
    r"字光式", r"ピル", r"ロックアイス", r"ロックアウト", r"ロックフェラー",
    r"世界記録", r"日本記録", r"最高記録", r"大会記録"
]

MUSIC_REGEX_JA = re.compile(
    "|".join([re.escape(k) for k in MUSIC_KEYWORDS_JA] + MUSIC_PHRASES_JA)
)
EXCLUDE_REGEX_JA = re.compile("|".join(EXCLUDE_PATTERNS_JA))


MUSIC_KEYWORDS_EN: List[str] = [
    "music", "musical", "musician", "song", "songs", "songwriter", "album", "albums",
    "band", "bands", "singer", "singers", "singing", "vocalist", "vocal",
    "composer", "composed", "conductor", "pianist", "piano", "guitarist", "guitar",
    "drummer", "drums", "bassist", "bass guitar", "violinist", "violin", "cello", "cellist",
    "saxophonist", "saxophone", "trumpet", "orchestra", "orchestral", "symphony", "symphonic",
    "concert", "concerts", "grammy", "billboard", "discography",
    "beatles", "jazz", "rock and roll", "rock band", "rock music", "classical music",
    "opera", "operas", "operatic", "hip hop", "r&b", "pop music", "woodstock",
    "synthesizer", "motown", "reggae", "blues", "heavy metal", "funk", "disco"
]

MUSIC_PHRASES_EN: List[str] = [
    r"\b(hit|debut|lead|new)\s+single\b",
    r"\breleased\s+(a|the|their|his|her)\s+(single|album|song|record)\b",
    r"\brecorded\s+(a|the|their|his|her)\s+(single|album|song|track)\b",
    r"\b(record\s+label|record\s+album|vinyl\s+record|gold\s+record|platinum\s+record)\b",
    r"\b(live\s+album|studio\s+album|debut\s+album)\b",
    r"\b(music\s+festival|music\s+award|music\s+hall\s+of\s+fame)\b"
]

MUSIC_REGEX_EN = re.compile(
    "|".join([r"\b(" + "|".join(re.escape(k) for k in MUSIC_KEYWORDS_EN) + r")\b"] + MUSIC_PHRASES_EN),
    re.IGNORECASE,
)


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


def fetch_today_music_episode(language: str = "en", timeout: float = 6.0) -> Dict[str, Any]:
    """今日（月日）にちなんだ音楽トピックス（アーティスト生誕・名盤リリース・音楽史の出来事）を取得"""
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

            music_events: List[str] = []

            # 1. events, selected, births, deaths から音楽関連を収集
            categories = [
                ("events", data.get("selected", []) + data.get("events", [])),
                ("births", data.get("births", [])),
                ("deaths", data.get("deaths", [])),
            ]

            for cat_name, items in categories:
                for item in items:
                    text = item.get("text", "")
                    # 単語境界正規表現でマッチング
                    if MUSIC_REGEX_EN.search(text):
                        year = item.get("year", "")
                        year_str = f"in {year}" if year else ""
                        if cat_name == "births":
                            music_events.append(f"Born on this day {year_str}: {text}")
                        elif cat_name == "deaths":
                            music_events.append(f"Remembering on this day {year_str}: {text}")
                        else:
                            music_events.append(f"On this day {year_str}, {text}")

            chosen_event = ""
            if music_events:
                # 音楽トピックの中からランダム選定
                chosen_event = random.choice(music_events[:6])
            else:
                chosen_event = f"On this day ({month}/{day}) in music history, legendary artists and timeless tracks continue to resonate."

            return {
                "success": True,
                "episode": chosen_event,
            }

        except Exception as e:
            print(f"⚠️ [daily_info] 英語音楽トピックスの取得に失敗しました: {e}", flush=True)
            return {
                "success": False,
                "episode": f"Today in music history is celebrated with timeless melodies and inspiring tracks.",
            }

    else:
        # 日本語: Wikipedia API (X月X日の全文から音楽トピックを抽出)
        title_str = f"{month}月{day}日"
        encoded_title = urllib.parse.quote(title_str)
        url = (
            f"https://ja.wikipedia.org/w/api.php?action=query&prop=extracts&explaintext=1&"
            f"titles={encoded_title}&format=json"
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

            matched_lines: List[str] = []
            if extract:
                lines = [line.strip() for line in extract.splitlines() if line.strip()]
                for line in lines:
                    if line.startswith("==") or len(line) < 8:
                        continue
                    if EXCLUDE_REGEX_JA.search(line):
                        continue
                    if MUSIC_REGEX_JA.search(line):
                        # 先頭の記号などをクリーンアップ
                        cleaned_line = line.lstrip("*-・• ").strip()
                        matched_lines.append(cleaned_line)

            chosen_topic = ""
            if matched_lines:
                # 年号付きのトピックを優先（上位候補からランダム選定）
                candidates = matched_lines[:6]
                chosen_topic = random.choice(candidates)
            else:
                chosen_topic = f"{month}月{day}日は、数々の名曲や素晴らしいアーティストの歴史に彩られた一日です。"

            return {
                "success": True,
                "episode": chosen_topic,
            }

        except Exception as e:
            print(f"⚠️ [daily_info] 日本語音楽トピックスの取得に失敗しました: {e}", flush=True)
            return {
                "success": False,
                "episode": f"{month}月{day}日は、素晴らしい音楽の歴史とともに過ごす最高の一日です。",
            }


# 互換性のためのエイリアス
fetch_today_episode = fetch_today_music_episode


def generate_daily_intro(language: str = "en") -> str:
    """日付・天気・今日の音楽トピックスを統合し、LLM またはフォールバックで自然な音楽オープニングナレーション文を生成"""
    broadcast_process_status("info", "🌐 今日の天気・日付・音楽トピックスを取得中...")

    date_str = format_current_date(language)
    weather_info = fetch_weather_forecast()
    episode_info = fetch_today_music_episode(language)

    weather_summary = (
        weather_info.get("summary_en", "")
        if language == "en"
        else weather_info.get("summary_ja", "")
    )
    episode_text = episode_info.get("episode", "")

    # llama.cpp を使って自然で洗練された音楽オープニングトークを生成
    broadcast_process_status("llm", "🤖 起動アナウンス（音楽トピックス）を生成中 (llama.cpp)...")

    if language == "en":
        system_prompt = (
            "You are a charismatic, smooth, and friendly FM radio DJ on an AI music station. "
            "Given today's date, weather summary, and a music-related topic on this day (such as an iconic album release, artist birthday, or music history milestone), "
            "write a concise, engaging 2-3 sentence radio station opening talk that flows smoothly.\n"
            "Rules:\n"
            "- Do NOT include greetings like 'Hello' or 'Hi' as they have already been spoken.\n"
            "- Smoothly mention the date, weather, and then highlight today's music topic/trivia.\n"
            "- Conclude with a warm, uplifting sentence building anticipation for enjoying good music.\n"
            "- Keep it under 55 words."
        )
        user_prompt = (
            f"Date: {date_str}\n"
            f"Weather: {weather_summary}\n"
            f"Music Topic on this day: {episode_text}\n\n"
            "Please generate the smooth radio DJ opening talk focusing on today's music topic."
        )
    else:
        system_prompt = (
            "あなたは親しみやすく落ち着いた声の moOde AI 音楽アシスタント（ラジオDJ風）です。"
            "今日の日付、天気情報、そして「今日にちなんだ音楽トピックス（名曲・名盤の発売、有名ミュージシャンの生誕、歴史的出来事など）」をもとに、"
            "最初の挨拶に自然に続く、心地よい2〜3文のオープニングトークを作成してください。\n"
            "【ルール】\n"
            "・「こんにちは」等の冒頭挨拶は既に発話済みのため含めないこと。\n"
            "・日付と天気を簡潔に伝えた後、今日にちなんだ音楽のエピソードを紹介すること。\n"
            "・最後は音楽を聴く時間を楽しみにできるような心地よい言葉で結ぶこと。\n"
            "・100文字〜150文字程度で簡潔に出力すること。"
        )
        user_prompt = (
            f"日付: {date_str}\n"
            f"天気: {weather_summary}\n"
            f"今日の音楽トピックス: {episode_text}\n\n"
            "上記の情報をもとに、音楽ファンが楽しめる自然なオープニングナレーション文を生成してください。"
        )

    payload = {
        "model": config.LLM_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "stream": False,
        "temperature": 0.5,
        "max_tokens": 180,
    }

    try:
        json_bytes = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            config.LLAMA_CPP_CHAT_URL,
            data=json_bytes,
            headers={"Content-Type": "application/json", "User-Agent": "moOde-AI-Master/1.0"},
        )
        with urllib.request.urlopen(req, timeout=12.0) as response:
            resp_data = json.loads(response.read().decode("utf-8"))
            content = resp_data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()

            # タグやMarkdownのクリーンアップ
            cleaned = re.sub(r"<think>[\s\S]*?</think>", "", content).strip()
            cleaned = cleaned.replace("```", "").replace("\n", " ").strip()

            if cleaned and len(cleaned) >= 15:
                print(f"🎙️ [daily_info] LLM生成音楽ナレーション: {cleaned}", flush=True)
                return cleaned

    except Exception as e:
        print(f"⚠️ [daily_info] llama.cpp ナレーション生成スキップ/フォールバック: {e}", flush=True)

    # フォールバックテンプレート合成（llama-server がオフラインまたはタイムアウト時）
    if language == "en":
        return f"Today is {date_str}. {weather_summary} In music history today, {episode_text}"
    else:
        return f"本日は{date_str}です。{weather_summary} 音楽の歴史を振り返ると、{episode_text}"
