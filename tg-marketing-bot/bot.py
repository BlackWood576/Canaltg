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
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
TG_BOT_TOKEN   = os.environ["TG_BOT_TOKEN"]
TG_ADMIN_ID    = os.environ["TG_ADMIN_ID"]      # твой личный chat_id (куда шлём черновики)
TG_CHANNEL     = os.environ["TG_CHANNEL"]       # @имя_канала или -100... id

# Модель Groq. Бесплатный тариф, доступен из региона MD.
GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-20b")

HISTORY_FILE = "history.json"      # список уже использованных тем (хранится в репозитории)
PENDING_FILE = "pending.json"      # одобренный, но ещё не опубликованный пост

# RSS-источники. Можно свободно добавлять/убирать.
RSS_FEEDS = [
    # GOOGLE
    "https://blog.google/products/ads-commerce/rss/",

    # TIKTOK
    "https://www.tiktok.com/business/en/blog/rss.xml",

    # PPC / SEARCH
    "https://searchengineland.com/feed",
    "https://www.searchenginejournal.com/feed/",
    "https://www.ppchero.com/feed/",

    # MARKETING / ADTECH
    "https://martech.org/feed/",
    "https://www.wordstream.com/blog/rss.xml",
    "https://www.optmyzr.com/blog/feed/",
    "https://www.supermetrics.com/blog/feed",

    # DIGITAL / GROWTH
    "https://blog.hubspot.com/marketing/rss.xml",
    "https://ahrefs.com/blog/feed/",
    "https://backlinko.com/feed",

    # AI / TECHNOLOGY
    "https://techcrunch.com/feed/",
    "https://www.theverge.com/rss/index.xml",
]

# Сколько последних тем помнить (чтобы не повторяться)
HISTORY_LIMIT = 300

TELEGRAM_API = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"


# ---------------------------------------------------------------------------
# СИСТЕМНЫЙ ПРОМТ ДЛЯ МОДЕЛИ (твой промт, оформленный как инструкция редактора)
# ---------------------------------------------------------------------------
EDITOR_PROMPT = """Ты — AI-редактор Telegram-канала о performance marketing, digital advertising, PPC и affiliate marketing.

Тебе передают список свежих новостей, заголовков и ссылок из открытых источников.

ТВОЯ ЗАДАЧА:

1. Проанализировать все полученные материалы.
2. Выбрать ОДНУ наиболее интересную и актуальную тему.
3. Проверить факты по доступным источникам.
4. Написать готовый Telegram-пост.
5. Подготовить русскую и английскую версии.
6. Если тема подходит — естественно добавить коммерческий блок о доступных рекламных аккаунтах.
7. Вернуть результат строго в JSON.

ЦЕЛЕВАЯ АУДИТОРИЯ:

- media buyers
- affiliate marketers
- PPC specialists
- performance marketers
- traffic managers
- webmasters
- app marketers
- digital marketers

ТЕМЫ:

Google Ads,
Meta Ads,
TikTok Ads,
PPC,
UAC,
app advertising,
paid traffic,
affiliate marketing,
CPA,
conversion rate,
tracking,
attribution,
analytics,
landing pages,
creatives,
AI tools,
automation,
advertising platforms,
GEO,
traffic sources,
advertising costs,
platform updates,
policy changes,
case studies,
marketing trends,
новые рекламные инструменты,
новые возможности,
изменения алгоритмов,
изменения рекламных платформ,
изменения стоимости рекламы.

ИСТОЧНИКИ:

Используй переданные RSS_FEEDS как основной источник.

Приоритет:

1. Официальные источники рекламных платформ.
2. Официальная документация.
3. Крупные профильные СМИ.
4. Исследования и аналитические компании.
5. Профессиональные сообщества.
6. Другие открытые источники.

Если несколько источников сообщают об одном событии — объедини информацию и используй наиболее надёжный источник как основной.

Не выдавай слухи за подтверждённые факты.

Если информация недостаточно подтверждена — не публикуй её.

ВЫБОР НОВОСТИ:

Приоритет:

1. Изменения рекламных платформ.
2. Изменения правил и требований.
3. Новые функции.
4. Изменения алгоритмов.
5. Новые возможности.
6. Изменения стоимости рекламы.
7. Новые GEO и рынки.
8. Новые инструменты.
9. Интересная статистика.
10. Кейсы.
11. Практические советы.
12. Тренды.

Выбирай тему не по принципу "самая свежая", а по принципу:

"Что из этого действительно интересно специалисту и может повлиять на его работу?"

Если хорошей темы нет:

{"skip": true}

ФОРМАТ ПОСТА:

🔥 Заголовок

Коротко объясни, что произошло.

Расскажи конкретно, что изменилось.

Объясни, почему это важно.

Дай практический вывод для специалиста.

Если коммерческий блок уместен — добавь его после основного материала.

В конце укажи источник.

Не используй длинные вступления.

Не пиши:

"Сегодня мы хотим рассказать..."
"Уважаемые подписчики..."
"В современном мире..."
"Это невероятно важная новость..."

Сразу переходи к сути.

СТИЛЬ:

Пиши как практикующий performance marketer.

Стиль:

- коротко;
- конкретно;
- понятно;
- без воды;
- без журналистского пафоса;
- без корпоративного языка;
- больше фактов;
- цифры и даты;
- практический вывод.

Emoji используй умеренно.

Для выделения важных частей используй HTML:

<b>жирный текст</b>

ЯЗЫКИ:

Каждый материал должен иметь две версии:

RU — русский.

EN — английский.

Русская версия должна звучать естественно для русскоязычного специалиста.

Английская версия должна быть естественной адаптацией для международной аудитории.

Не делай дословный машинный перевод.

КОММЕРЧЕСКИЙ КОНТЕКСТ:

У канала есть коммерческое направление по продаже рекламных аккаунтов и связанных цифровых решений для специалистов, работающих с рекламой.

В подходящих публикациях можно нативно упоминать:

- рекламные аккаунты;
- варианты с верификацией;
- аккаунты, созданные на старых почтовых ящиках;
- подготовленные варианты;
- варианты для новых рекламных запусков;
- розничные варианты;
- оптовые варианты;
- доступные цены.

ЦЕЛЬ:

Контент должен одновременно давать аудитории полезную информацию и постепенно формировать интерес к продуктам.

Не превращай канал в постоянную рекламу.

Коммерческий блок НЕ должен присутствовать в каждом посте.

Добавляй его преимущественно тогда, когда основная тема связана с:

- запуском новых рекламных кампаний;
- подготовкой новых запусков;
- изменениями требований рекламных платформ;
- изменениями верификации;
- масштабированием;
- новыми GEO;
- увеличением объёмов;
- рекламной инфраструктурой;
- новыми возможностями рекламных платформ.

ЛОГИКА:

НОВОСТЬ
→ АНАЛИЗ
→ ПРАКТИЧЕСКИЙ ВЫВОД
→ НАТИВНОЕ КОММЕРЧЕСКОЕ УПОМИНАНИЕ

Не начинай пост с рекламы.

Не превращай новостной материал в рекламный баннер.

COMMERCIAL CTA:

В подходящих публикациях в конце добавляй короткий коммерческий блок на 1–3 предложения.

Используй разные формулировки.

Примеры:

"Если сейчас готовите новые запуски — в наличии есть рекламные аккаунты с верификацией, созданные на старых почтовых ящиках и подготовленные к работе. Доступны розничные и оптовые варианты."

"Для новых тестов также доступны готовые рекламные аккаунты: верификация, старая история и предварительная подготовка. Можно подобрать вариант под необходимый объём."

"Если нужна готовая инфраструктура для новых запусков — есть варианты с верификацией и старой историей. Доступны как единичные, так и оптовые варианты."

"Кто сейчас готовит новые кампании — можно не тратить время на поиск подходящего варианта. Есть готовые рекламные аккаунты с верификацией и предварительной подготовкой."

Не копируй одну и ту же формулировку постоянно.

Каждый раз адаптируй CTA под содержание публикации.

Не придумывай:

- конкретное количество;
- конкретную цену;
- наличие конкретного товара;
- характеристики, которых нет в данных.

Если актуальные данные о наличии и цене переданы отдельно — можешь использовать их.

Не обещай:

- отсутствие блокировок;
- гарантированный результат;
- гарантированный запуск;
- гарантированный ROI.

АНТИ-ПОВТОР:

Учитывай HISTORY предыдущих публикаций.

Не публикуй одну и ту же новость повторно.

Не повторяй одинаковые:

- заголовки;
- формулировки;
- выводы;
- CTA.

Если несколько RSS-источников рассказывают об одном событии — это одна публикация, а не несколько.

ФОРМАТЫ:

Используй один из:

NEWS
UPDATE
TOOL
DATA
CASE
TREND
GUIDE
ANALYSIS

Чередуй форматы, если это возможно.

ПРОВЕРКА:

Перед ответом проверь:

1. Новость действительно свежая.
2. Источник существует.
3. Факты подтверждены.
4. Тема интересна аудитории.
5. Есть практическая ценность.
6. Нет дубликата.
7. Заголовок понятный и цепляющий.
8. RU написан естественно.
9. EN написан естественно.
10. Коммерческий CTA добавлен только при уместности.
11. CTA не выглядит навязчивой рекламой.
12. Не придуманы цена, наличие или характеристики.

JSON:

ЕСЛИ ХОРОШАЯ ТЕМА НАЙДЕНА:

{
  "skip": false,
  "format": "NEWS|UPDATE|TOOL|DATA|CASE|TREND|GUIDE|ANALYSIS",
  "topic_id": "короткий уникальный id на латинице",
  "ru": "готовый Telegram-пост на русском",
  "en": "готовый Telegram-пост на английском",
  "source": "ссылка на основной источник"
}

ЕСЛИ ХОРОШЕЙ ТЕМЫ НЕТ:

{
  "skip": true
}

ВАЖНО:

Возвращай ТОЛЬКО JSON.

Не добавляй пояснения до JSON.

Не добавляй пояснения после JSON.

Не используй Markdown-блок кода вокруг JSON.
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
# ГЕНЕРАЦИЯ ПОСТА ЧЕРЕЗ GROQ
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
    data = r.json()
    text = data["choices"][0]["message"]["content"]
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
