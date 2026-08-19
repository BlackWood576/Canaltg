#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram performance-marketing editor bot.

Что делает при каждом запуске:
  1. Читает свежие записи из RSS официальных блогов рекламных платформ и профильных СМИ.
  2. Отфильтровывает то, что уже публиковалось (анти-повтор по истории).
  3. Просит Gemini выбрать ОДНУ самую ценную тему и написать пост RU+EN по заданному стилю.
  4. Присылает готовый черновик ТЕБЕ в личку с кнопкой "Опубликовать в канал".
  5. Ты жмёшь кнопку -> следующий запуск бота публикует одобренный пост в канал.

Ничего не публикуется в канал без твоего одобрения.
"""

import os
import json
import time
import html
import hashlib
import datetime as dt
from urllib.parse import quote

import requests
import feedparser

# ---------------------------------------------------------------------------
# КОНФИГ (значения берутся из переменных окружения / GitHub Secrets)
# ---------------------------------------------------------------------------
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
TG_BOT_TOKEN   = os.environ["TG_BOT_TOKEN"]
TG_ADMIN_ID    = os.environ["TG_ADMIN_ID"]      # твой личный chat_id (куда шлём черновики)
TG_CHANNEL     = os.environ["TG_CHANNEL"]       # @имя_канала или -100... id

# Модель Gemini. flash — быстрый и с щедрым бесплатным лимитом.
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")

HISTORY_FILE = "history.json"      # список уже использованных тем (хранится в репозитории)
PENDING_FILE = "pending.json"      # одобренный, но ещё не опубликованный пост

# RSS-источники. Можно свободно добавлять/убирать.
RSS_FEEDS = [
    "https://blog.google/products/ads-commerce/rss/",
    "https://www.tiktok.com/business/en/blog/rss.xml",
    "https://searchengineland.com/feed",
    "https://www.searchenginejournal.com/feed/",
    "https://www.ppchero.com/feed/",
    "https://martech.org/feed/",
]

# Сколько последних тем помнить (чтобы не повторяться)
HISTORY_LIMIT = 300

TELEGRAM_API = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"


# ---------------------------------------------------------------------------
# СИСТЕМНЫЙ ПРОМТ ДЛЯ МОДЕЛИ (твой промт, оформленный как инструкция редактора)
# ---------------------------------------------------------------------------
EDITOR_PROMPT = """Ты — AI-редактор Telegram-канала о digital marketing и performance marketing.
Тебе дают список свежих заголовков и ссылок из открытых источников.
Выбери ОДНУ самую ценную тему для специалиста по performance marketing и напиши по ней пост.

ПРИОРИТЕТ ОТБОРА (сверху вниз):
1. Изменения рекламных платформ. 2. Изменения алгоритмов. 3. Новые функции.
4. Изменения рекламных правил. 5. Новые инструменты. 6. Изменения стоимости рекламы.
7. Интересные GEO и рынки. 8. Исследования. 9. Кейсы. 10. Практические советы.
Если ни одна тема не даёт практической ценности — верни поле "skip": true.

ФОРМАТ ПОСТА (обе языковые версии):
🔥 Заголовок
Коротко: что произошло.
Почему это важно.
Практический вывод.
Источник (ссылка).

СТИЛЬ: коротко, конкретно, без воды, без журналистского пафоса, без длинных вступлений.
Пиши как практикующий performance marketer. Больше фактов, цифр и дат, если они есть.
Не начинай со слов вроде "Сегодня мы хотим рассказать", "Уважаемые подписчики", "В современном мире".
Emoji — умеренно. Для выделения используй <b>жирный</b> (HTML, а не markdown).

ЯЗЫКИ: сделай RU и EN версии. EN — адаптация под международное performance-сообщество, НЕ дословный перевод.

ФОРМАТ ОТВЕТА — строго JSON, без markdown-ограждений и без текста вокруг:
{
  "skip": false,
  "format": "NEWS|UPDATE|TOOL|DATA|CASE|TREND|GUIDE|ANALYSIS",
  "topic_id": "короткий стабильный идентификатор темы на латинице",
  "ru": "полный текст RU-версии с HTML-тегами <b>...</b> и ссылкой-источником",
  "en": "полный текст EN-версии с HTML-тегами <b>...</b> и ссылкой-источником"
}
Если публиковать нечего: {"skip": true}
"""


# ---------------------------------------------------------------------------
# РАБОТА С ИСТОРИЕЙ (анти-повтор)
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# СБОР ТЕМ ИЗ RSS
# ---------------------------------------------------------------------------
def collect_candidates(seen_keys):
    """Возвращает список свежих кандидатов, которых ещё не было в истории."""
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
            # фильтр по свежести, если дата есть
            published = entry.get("published_parsed") or entry.get("updated_parsed")
            if published:
                pub_dt = dt.datetime.fromtimestamp(time.mktime(published))
                if pub_dt < cutoff:
                    continue
            summary = entry.get("summary", "")[:400]
            candidates.append({"key": key, "title": title, "link": link, "summary": summary})
    print(f"[rss] собрано свежих кандидатов: {len(candidates)}")
    return candidates


# ---------------------------------------------------------------------------
# ГЕНЕРАЦИЯ ПОСТА ЧЕРЕЗ GEMINI
# ---------------------------------------------------------------------------
def generate_post(candidates):
    lines = []
    for i, c in enumerate(candidates[:25], 1):
        lines.append(f'{i}. "{c["title"]}" — {c["link"]}\n   {c["summary"]}')
    candidates_block = "\n".join(lines)

    user_content = (
        "Свежие кандидаты (заголовок — ссылка — краткое описание):\n\n"
        + candidates_block
        + "\n\nВыбери одну лучшую тему и верни JSON по заданному формату."
    )

    endpoint = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    )
    payload = {
        "system_instruction": {"parts": [{"text": EDITOR_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": user_content}]}],
        "generationConfig": {"temperature": 0.7, "response_mime_type": "application/json"},
    }

    r = requests.post(endpoint, json=payload, timeout=90)
    r.raise_for_status()
    data = r.json()
    text = data["candidates"][0]["content"]["parts"][0]["text"]
    # на случай, если модель обернула в ```json
    text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return json.loads(text)


# ---------------------------------------------------------------------------
# TELEGRAM
# ---------------------------------------------------------------------------
def tg_send(chat_id, text, reply_markup=None):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    r = requests.post(f"{TELEGRAM_API}/sendMessage", json=payload, timeout=30)
    if not r.ok:
        print(f"[tg] ошибка отправки: {r.text}")
    return r.ok


def draft_to_admin(post):
    """Шлём готовый черновик тебе в личку с кнопкой одобрения."""
    header = f"📝 <b>Черновик [{post.get('format','POST')}]</b>\nОдобри — и он уйдёт в канал.\n"
    body = (
        header
        + "\n———  🇷🇺 RU  ———\n" + post["ru"]
        + "\n\n———  🇬🇧 EN  ———\n" + post["en"]
    )
    # callback_data ограничен 64 байтами, поэтому передаём только topic_id
    markup = {
        "inline_keyboard": [[
            {"text": "✅ Опубликовать в канал", "callback_data": f"pub:{post['topic_id'][:50]}"},
            {"text": "🗑 Отклонить", "callback_data": "skip"},
        ]]
    }
    tg_send(TG_ADMIN_ID, body, reply_markup=markup)


def publish_to_channel(post):
    """Публикуем обе версии в канал (RU и EN отдельными сообщениями)."""
    ok_ru = tg_send(TG_CHANNEL, post["ru"])
    ok_en = tg_send(TG_CHANNEL, post["en"])
    return ok_ru and ok_en


# ---------------------------------------------------------------------------
# ОБРАБОТКА ОДОБРЕНИЙ (читаем нажатия кнопок через getUpdates)
# ---------------------------------------------------------------------------
def process_approvals(history):
    """
    Проверяем, не нажал ли ты кнопку на присланных ранее черновиках.
    Одобренные посты публикуем в канал.
    Возвращаем True, если что-то опубликовали.
    """
    offset_data = load_json("tg_offset.json", {"offset": 0})
    r = requests.get(
        f"{TELEGRAM_API}/getUpdates",
        params={"offset": offset_data["offset"], "timeout": 0},
        timeout=30,
    )
    if not r.ok:
        print(f"[tg] getUpdates fail: {r.text}")
        return False

    updates = r.json().get("result", [])
    pending = load_json(PENDING_FILE, {})   # topic_id -> post
    published_any = False
    max_update_id = offset_data["offset"]

    for upd in updates:
        max_update_id = max(max_update_id, upd["update_id"] + 1)
        cq = upd.get("callback_query")
        if not cq:
            continue
        data = cq.get("data", "")
        # отвечаем на callback, чтобы Telegram убрал "часики"
        requests.post(f"{TELEGRAM_API}/answerCallbackQuery",
                      json={"callback_query_id": cq["id"]}, timeout=15)

        if data.startswith("pub:"):
            tid = data.split("pub:", 1)[1]
            post = pending.get(tid)
            if post:
                if publish_to_channel(post):
                    tg_send(TG_ADMIN_ID, "✅ Опубликовано в канал.")
                    history.append(post["topic_id"])
                    pending.pop(tid, None)
                    published_any = True
                else:
                    tg_send(TG_ADMIN_ID, "⚠️ Не удалось опубликовать. Проверь права бота в канале.")
        elif data == "skip":
            tg_send(TG_ADMIN_ID, "🗑 Черновик отклонён.")

    save_json("tg_offset.json", {"offset": max_update_id})
    save_json(PENDING_FILE, pending)
    return published_any


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def main():
    history = load_json(HISTORY_FILE, [])
    seen = set(history)

    # 1) сперва разбираем твои одобрения предыдущих черновиков
    process_approvals(history)

    # 2) собираем свежие темы
    candidates = collect_candidates(seen)
    if not candidates:
        print("Свежих тем нет — ничего не генерируем (качество > количество).")
        save_json(HISTORY_FILE, history[-HISTORY_LIMIT:])
        return

    # 3) генерируем пост
    try:
        post = generate_post(candidates)
    except Exception as e:
        print(f"[gemini] ошибка генерации: {e}")
        return

    if post.get("skip"):
        print("Модель решила, что публиковать нечего.")
        save_json(HISTORY_FILE, history[-HISTORY_LIMIT:])
        return

    # 4) кладём в pending и шлём тебе черновик на одобрение
    pending = load_json(PENDING_FILE, {})
    pending[post["topic_id"][:50]] = post
    save_json(PENDING_FILE, pending)

    # тему сразу отмечаем как показанную, чтобы не предлагать повторно
    for c in candidates:
        if c["title"].lower() in (post["ru"] + post["en"]).lower():
            history.append(c["key"])
    draft_to_admin(post)
    print("Черновик отправлен на одобрение.")

    save_json(HISTORY_FILE, history[-HISTORY_LIMIT:])


if __name__ == "__main__":
    main()
