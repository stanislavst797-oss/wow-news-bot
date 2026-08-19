#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Автопостинг новостей из RSS noob-club.ru в Discord.

Вебхук читается ТОЛЬКО из переменной окружения DISCORD_WEBHOOK.
В файлах репозитория его быть не должно.
"""

import html
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

import feedparser
import requests

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
POSTED_PATH = ROOT / "posted.json"

DEFAULTS = {
    "feed_url": "https://www.noob-club.ru/rss2.xml",
    "stop_words": [],
    "max_per_run": 5,
    "history_size": 500,
    "description_limit": 300,
    "embed_color": 10038562,
    "footer_text": "noob-club.ru",
    "delay_between_posts": 1.0,
    "request_timeout": 30,
    "user_agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
}

# Хвост, который сайт клеит в конец каждого анонса.
READ_MORE_RE = re.compile(r"\s*(читать\s+далее|подробнее)\s*[.…]*\s*$", re.IGNORECASE)
IMG_SRC_RE = re.compile(r"<img[^>]+src\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE)
TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")

# Ответы Discord, после которых повторять бессмысленно: вебхук удалён,
# перевыпущен или запрещён. Такой отказ должен красить прогон в красный,
# иначе постинг умрёт молча — в канале тишина, а в Actions «успешно».
FATAL_WEBHOOK_CODES = (401, 403, 404)


class WebhookBroken(Exception):
    """Вебхук нерабочий — чинится только заменой секрета DISCORD_WEBHOOK."""


def setup_logging():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )


def load_config():
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            with CONFIG_PATH.open(encoding="utf-8") as fh:
                cfg.update(json.load(fh))
        except (OSError, ValueError) as exc:
            logging.warning("config.json не читается (%s), беру значения по умолчанию", exc)
    return cfg


def load_posted():
    """Возвращает (список_ссылок, это_первый_запуск)."""
    if not POSTED_PATH.exists():
        return [], True
    try:
        with POSTED_PATH.open(encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        logging.warning("posted.json повреждён (%s) — считаю его пустым", exc)
        return [], False
    if isinstance(data, dict):
        data = data.get("posted", [])
    if not isinstance(data, list):
        return [], False
    return [str(x) for x in data], False


def save_posted(links, history_size):
    payload = {"posted": links[-history_size:]}
    tmp = POSTED_PATH.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    tmp.replace(POSTED_PATH)


def fetch_feed(cfg):
    """Скачивает и парсит ленту. Кидает исключение при любой проблеме."""
    resp = requests.get(
        cfg["feed_url"],
        headers={
            "User-Agent": cfg["user_agent"],
            "Accept": "application/rss+xml, application/xml, text/xml, */*",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        },
        timeout=cfg["request_timeout"],
    )
    resp.raise_for_status()
    parsed = feedparser.parse(resp.content)
    if parsed.bozo and not parsed.entries:
        raise ValueError("лента не распарсилась: %s" % parsed.bozo_exception)
    return parsed


def is_blocked(title, stop_words):
    low = (title or "").casefold()
    for word in stop_words:
        if word and word.casefold() in low:
            return word
    return None


def entry_html(entry):
    if entry.get("content"):
        return entry["content"][0].get("value", "") or ""
    return entry.get("summary", "") or ""


def extract_image(entry):
    for enc in entry.get("enclosures") or []:
        if str(enc.get("type", "")).startswith("image/") and enc.get("href"):
            return enc["href"]
    for key in ("media_content", "media_thumbnail"):
        for media in entry.get(key) or []:
            if media.get("url"):
                return media["url"]
    # В этой ленте картинки живут только внутри HTML анонса.
    match = IMG_SRC_RE.search(entry_html(entry))
    if match:
        return html.unescape(match.group(1))
    return None


def make_description(entry, limit):
    text = entry_html(entry)
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<br\s*/?>|</p>|</div>", " ", text, flags=re.I)
    text = TAG_RE.sub("", text)
    text = html.unescape(text)
    text = WS_RE.sub(" ", text).strip()
    text = READ_MORE_RE.sub("", text).strip()
    if len(text) <= limit:
        return text
    cut = text[: limit - 1].rstrip()
    if " " in cut:
        cut = cut[: cut.rfind(" ")].rstrip()
    return cut.rstrip(" ,.;:—-") + "…"


def make_timestamp(entry):
    stamp = entry.get("published_parsed") or entry.get("updated_parsed")
    if not stamp:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", stamp)


def build_embed(entry, cfg):
    embed = {
        "title": (entry.get("title") or "Без заголовка")[:256],
        "url": entry.get("link"),
        "color": cfg["embed_color"],
        "footer": {"text": cfg["footer_text"]},
    }
    description = make_description(entry, cfg["description_limit"])
    if description:
        embed["description"] = description
    timestamp = make_timestamp(entry)
    if timestamp:
        embed["timestamp"] = timestamp
    image = extract_image(entry)
    if image:
        embed["image"] = {"url": image}
    return embed


def post_embed(webhook, embed, cfg, dry_run=False):
    """Отправляет один эмбед. True — успех, False — не получилось."""
    if dry_run:
        logging.info("DRY-RUN, не отправляю: %s", embed["title"])
        return True

    for attempt in range(1, 6):
        try:
            resp = requests.post(
                webhook,
                json={"embeds": [embed], "allowed_mentions": {"parse": []}},
                headers={"Content-Type": "application/json"},
                timeout=cfg["request_timeout"],
            )
        except requests.RequestException as exc:
            logging.error("сеть при отправке (попытка %s): %s", attempt, exc)
            time.sleep(2 * attempt)
            continue

        if resp.status_code == 429:
            retry_after = None
            try:
                retry_after = float(resp.json().get("retry_after"))
            except (ValueError, TypeError, AttributeError):
                pass
            if retry_after is None:
                try:
                    retry_after = float(resp.headers.get("Retry-After", 5))
                except (ValueError, TypeError):
                    retry_after = 5.0
            # Discord отдаёт секунды, но у глобального лимита бывают миллисекунды.
            if retry_after > 300:
                retry_after /= 1000.0
            wait = min(retry_after, 60) + 0.5
            logging.warning("429 от Discord, жду %.1f с (попытка %s)", wait, attempt)
            time.sleep(wait)
            continue

        if 200 <= resp.status_code < 300:
            return True

        if resp.status_code >= 500:
            logging.warning("Discord ответил %s (попытка %s), повторю", resp.status_code, attempt)
            time.sleep(2 * attempt)
            continue

        if resp.status_code in FATAL_WEBHOOK_CODES:
            raise WebhookBroken(
                "Discord ответил %s — вебхук недействителен. "
                "Пересоздайте его в настройках канала и обновите секрет DISCORD_WEBHOOK. Ответ: %s"
                % (resp.status_code, resp.text[:200])
            )

        logging.error("Discord отказал: %s %s", resp.status_code, resp.text[:300])
        return False

    logging.error("не удалось отправить после всех попыток: %s", embed["title"])
    return False


def run():
    cfg = load_config()
    dry_run = os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes")

    webhook = (os.environ.get("DISCORD_WEBHOOK") or "").strip()
    if not webhook and not dry_run:
        logging.error("нет переменной окружения DISCORD_WEBHOOK — отправлять некуда")
        return 1

    try:
        parsed = fetch_feed(cfg)
    except Exception as exc:  # сеть, 403, таймаут, битый XML
        logging.error("лента недоступна (%s) — выхожу с кодом 0, повторю в следующий прогон", exc)
        return 0

    entries = [e for e in parsed.entries if e.get("link")]
    logging.info("записей в ленте: %s", len(entries))
    if not entries:
        return 0

    posted, first_run = load_posted()

    if first_run:
        links = [e["link"] for e in entries]
        save_posted(links, cfg["history_size"])
        logging.info(
            "первый запуск: пометил %s текущих ссылок отправленными, архивом не спамлю",
            len(links),
        )
        return 0

    known = set(posted)
    fresh = [e for e in entries if e["link"] not in known]
    logging.info("новых записей: %s", len(fresh))

    to_send = []
    for entry in reversed(fresh):  # от старых к новым, чтобы в канале был хронологический порядок
        word = is_blocked(entry.get("title", ""), cfg["stop_words"])
        if word:
            logging.info("стоп-слово «%s» отсекло: %s", word, (entry.get("title") or "")[:90])
            posted.append(entry["link"])  # больше её не рассматриваем
            continue
        to_send.append(entry)

    limit = cfg["max_per_run"]
    if len(to_send) > limit:
        logging.info("к отправке %s, за прогон шлю %s, остальное уйдёт позже", len(to_send), limit)
        to_send = to_send[:limit]

    sent = 0
    for index, entry in enumerate(to_send):
        embed = build_embed(entry, cfg)
        try:
            ok = post_embed(webhook, embed, cfg, dry_run)
        except WebhookBroken as exc:
            # Молчать нельзя: пусть прогон будет красным и GitHub пришлёт уведомление.
            logging.error("%s", exc)
            save_posted(posted, cfg["history_size"])
            logging.error("отправлено до сбоя: %s", sent)
            return 1
        if not ok:
            logging.error("прерываюсь, остаток уйдёт в следующий прогон")
            break
        sent += 1
        posted.append(entry["link"])
        save_posted(posted, cfg["history_size"])  # состояние переживёт падение на середине
        logging.info("отправлено: %s", (entry.get("title") or "")[:90])
        if index < len(to_send) - 1:
            time.sleep(cfg["delay_between_posts"])

    save_posted(posted, cfg["history_size"])
    logging.info("итог: отправлено %s, в истории %s ссылок", sent, len(posted[-cfg["history_size"]:]))
    return 0


def main():
    setup_logging()
    try:
        return run()
    except Exception:
        logging.exception("непредвиденная ошибка — job не роняю")
        return 0


if __name__ == "__main__":
    sys.exit(main())
