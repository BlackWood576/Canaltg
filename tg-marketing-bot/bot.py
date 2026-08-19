#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram performance-marketing editor bot (Groq edition).
Собирает свежие темы из RSS, просит Groq выбрать одну и написать пост RU+EN,
присылает готовый черновик тебе в личку с кнопкой публикации.
"""

import os
import json
import time
import hashlib
import datetime as dt

import requests
import feedparser

# --- КОНФИГ ---
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
TG_BOT_TOKEN = os.environ["TG_BOT_TOKEN"]
TG_ADMIN_ID  = os.environ["TG_ADMIN_ID"]
TG_CHANNEL   = os.environ["TG_CHANNEL"]

GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-20b")

HISTORY_FILE = "history.json"
PENDING_FILE = "pending.json"

RSS_FEEDS = [
    "https://blog.google/products/ads-commerce/rss/",
    "https://www.tiktok.com/business/en/blog/rss.xml",
    "https://searchengineland.com/feed",
    "https://www.searchenginejournal.com/feed/",
    "https://www.ppchero.com/feed/",
    "https://martech.org/feed/",
]

HISTORY_LIMIT = 300
TELEGRAM_API = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"

EDITOR_PROMPT = """Ты — AI-редактор Telegram-канала о digital marketing и performance marketing.
Тебе дают список свежих заголовков и ссылок. Выбери ОДНУ самую ценную тему для специалиста
по performance marketing и напиши по ней пост. Верни ответ строго как JSON.

ПРИОРИТЕТ: изменения платформ, алгоритмов, новые функции, изменения правил, новые инструменты,
стоимость рекламы, GEO/рынки, исследования, кейсы, советы.
Если ничего ценного нет — верни {"skip": true}.

ФОРМАТ ПОСТА (обе версии):
🔥 Заголовок
Коротко: что произошло.
Почему это важно.
Практический вывод.
Источник (ссылка).

СТИЛЬ: коротко, конкретно, без воды и пафоса, как практикующий маркетолог. Больше фактов, цифр, дат.
Для выделения используй <b>жирный</b> (HTML). Emoji умеренно.
Языки: RU и EN. EN — адаптация, не дословный перевод.

Верни JSON вида:
{"skip": false, "format": "NEWS|UPDATE|TOOL|DATA|CASE|TREND|GUIDE|ANALYSIS",
 "topic_id": "короткий id на латинице", "ru": "текст RU с <b> и ссылкой", "en": "текст EN с <b> и ссылкой"}
"""


def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default
    return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def topic_key(title, link):
    raw = (title or "") + "|" + (link or "")
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def collect_candidates(seen_keys):
    cutoff = dt.datetime.utcnow() - dt.timedelta(days=4)
    candidates = []
    for url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
        except Exception as e:
            print(f"[rss] fail {url}: {e}")
            continue
        for entry in feed.entries[:10]:
            title = entry.get("title", "").strip()
            link = entry.get("link", "").strip()
            if not title or not link:
                continue
            key = topic_key(title, link)
            if key in seen_keys:
                continue
            published = entry.get("published_parsed") or entry.get("updated_parsed")
            if published:
                pub_dt = dt.datetime.fromtimestamp(time.mktime(published))
                if pub_dt < cutoff:
                    continue
            summary = entry.get("summary", "")[:400]
            candidates.append({"key": key, "title": title, "link": link, "summary": summary})
    print(f"[rss] собрано свежих кандидатов: {len(candidates)}")
    return candidates


def generate_post(candidates):
    lines = []
    for i, c in enumerate(candidates[:25], 1):
        lines.append(f'{i}. "{c["title"]}" — {c["link"]}\n   {c["summary"]}')
    user_content = (
        "Свежие кандидаты (заголовок — ссылка — описание):\n\n"
        + "\n".join(lines)
        + "\n\nВыбери одну лучшую тему и верни JSON."
    )

    endpoint = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": EDITOR_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.7,
        "response_format": {"type": "json_object"},
    }
    r = requests.post(endpoint, headers=headers, json=payload, timeout=90)
    r.raise_for_status()
    text = r.json()["choices"][0]["message"]["content"]
    text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return json.loads(text)


def tg_send(chat_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
               "disable_web_page_preview": False}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    r = requests.post(f"{TELEGRAM_API}/sendMessage", json=payload, timeout=30)
    if not r.ok:
        print(f"[tg] ошибка отправки: {r.text}")
    return r.ok


def draft_to_admin(post):
    header = f"📝 <b>Черновик [{post.get('format','POST')}]</b>\nОдобри — и он уйдёт в канал.\n"
    body = header + "\n———  🇷🇺 RU  ———\n" + post["ru"] + "\n\n———  🇬🇧 EN  ———\n" + post["en"]
    markup = {"inline_keyboard": [[
        {"text": "✅ Опубликовать в канал", "callback_data": f"pub:{post['topic_id'][:50]}"},
        {"text": "🗑 Отклонить", "callback_data": "skip"},
    ]]}
    tg_send(TG_ADMIN_ID, body, reply_markup=markup)


def publish_to_channel(post):
    return tg_send(TG_CHANNEL, post["ru"]) and tg_send(TG_CHANNEL, post["en"])


def process_approvals(history):
    offset_data = load_json("tg_offset.json", {"offset": 0})
    r = requests.get(f"{TELEGRAM_API}/getUpdates",
                     params={"offset": offset_data["offset"], "timeout": 0}, timeout=30)
    if not r.ok:
        print(f"[tg] getUpdates fail: {r.text}")
        return
    updates = r.json().get("result", [])
    pending = load_json(PENDING_FILE, {})
    max_update_id = offset_data["offset"]
    for upd in updates:
        max_update_id = max(max_update_id, upd["update_id"] + 1)
        cq = upd.get("callback_query")
        if not cq:
            continue
        data = cq.get("data", "")
        requests.post(f"{TELEGRAM_API}/answerCallbackQuery",
                      json={"callback_query_id": cq["id"]}, timeout=15)
        if data.startswith("pub:"):
            post = pending.get(data.split("pub:", 1)[1])
            if post and publish_to_channel(post):
                tg_send(TG_ADMIN_ID, "✅ Опубликовано в канал.")
                history.append(post["topic_id"])
                pending.pop(data.split("pub:", 1)[1], None)
        elif data == "skip":
            tg_send(TG_ADMIN_ID, "🗑 Черновик отклонён.")
    save_json("tg_offset.json", {"offset": max_update_id})
    save_json(PENDING_FILE, pending)


def main():
    history = load_json(HISTORY_FILE, [])
    seen = set(history)
    process_approvals(history)

    candidates = collect_candidates(seen)
    if not candidates:
        print("Свежих тем нет.")
        save_json(HISTORY_FILE, history[-HISTORY_LIMIT:])
        return

    try:
        post = generate_post(candidates)
    except Exception as e:
        print(f"[groq] ошибка генерации: {e}")
        return

    if post.get("skip"):
        print("Модель решила: публиковать нечего.")
        save_json(HISTORY_FILE, history[-HISTORY_LIMIT:])
        return

    pending = load_json(PENDING_FILE, {})
    pending[post["topic_id"][:50]] = post
    save_json(PENDING_FILE, pending)
    for c in candidates:
        if c["title"].lower() in (post["ru"] + post["en"]).lower():
            history.append(c["key"])
    draft_to_admin(post)
    print("Черновик отправлен на одобрение.")
    save_json(HISTORY_FILE, history[-HISTORY_LIMIT:])


if __name__ == "__main__":
    main()
