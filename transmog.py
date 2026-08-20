#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ежедневная подборка трансмогов из r/Transmogrification в Discord.

Вебхук читается ТОЛЬКО из переменной окружения TRANSMOG_WEBHOOK.

Каждый день — НОВОЕ сообщение, а не редактирование: в канале должна
получаться лента, которую можно листать по дням.

Медиа не скачиваем и не перезаливаем: в сообщение идёт голая ссылка на пост,
а превью разворачивает сам Discord. Поэтому ссылки нельзя оформлять
маркдауном [текст](url) — на замаскированные ссылки превью не строится.

Про Reddit: один запрос в сутки, честный User-Agent, при 429 не долбим,
а спокойно выходим и пробуем завтра.
"""

import json
import logging
import os
import sys
import time
from pathlib import Path

import feedparser
import requests

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "transmog_config.json"
POSTED_PATH = ROOT / "transmog_posted.json"

DEFAULTS = {
    "feed_url": "https://www.reddit.com/r/Transmogrification/top.rss?t=day",
    "user_agent": "discord-transmog-bot/1.0",
    "count": 5,
    "history_size": 500,
    "header": "**Трансмоги дня · r/Transmogrification**",
    # single — все ссылки одним сообщением, separate — шапка и посты по одному.
    # Discord строит превью не более чем на 5 ссылок в сообщении, так что
    # при count > 5 разумно переключиться на separate.
    "message_mode": "single",
    "request_timeout": 30,
    "delay_between_posts": 1.0,
    # Сколько ждать, пока Discord подтянет превью, прежде чем их пересчитать.
    "embed_check_delay": 4.0,
    "verify_embeds": True,
}

FATAL_WEBHOOK_CODES = (401, 403, 404)


class TransmogError(Exception):
    """Публиковать нечего или некуда — прогон должен покраснеть."""


class RedditBusy(Exception):
    """Reddit просит не беспокоить. Это не ошибка — просто придём завтра."""


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
            logging.warning("transmog_config.json не читается (%s), беру умолчания", exc)
    return cfg


def load_posted():
    if not POSTED_PATH.exists():
        return [], True
    try:
        with POSTED_PATH.open(encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        logging.warning("transmog_posted.json повреждён (%s) — считаю его пустым", exc)
        return [], False
    if isinstance(data, dict):
        data = data.get("posted", [])
    if not isinstance(data, list):
        return [], False
    return [str(x) for x in data], False


def save_posted(ids, history_size):
    payload = {"posted": ids[-history_size:]}
    tmp = POSTED_PATH.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    tmp.replace(POSTED_PATH)


def fetch_feed(cfg):
    """Один запрос в сутки. 429 — это RedditBusy, всё остальное плохое — TransmogError."""
    try:
        resp = requests.get(
            cfg["feed_url"],
            headers={"User-Agent": cfg["user_agent"], "Accept": "application/atom+xml, */*"},
            timeout=cfg["request_timeout"],
        )
    except requests.RequestException as exc:
        raise TransmogError("не достучался до Reddit: %s" % exc) from exc

    if resp.status_code == 429:
        retry = resp.headers.get("Retry-After", "не указан")
        raise RedditBusy("Reddit ответил 429 (Retry-After: %s)" % retry)

    if not (200 <= resp.status_code < 300):
        raise TransmogError(
            "Reddit ответил %s. Если это 403 или капча — значит, он начал резать "
            "раннеры GitHub; обходить блокировку бот не будет. Тело: %s"
            % (resp.status_code, resp.text[:200].replace("\n", " "))
        )

    ctype = resp.headers.get("Content-Type", "")
    if "xml" not in ctype.lower():
        raise TransmogError(
            "Reddit вернул не XML, а «%s» — похоже, вместо ленты отдана заглушка "
            "или капча. Начало тела: %s"
            % (ctype, resp.text[:200].replace("\n", " "))
        )

    parsed = feedparser.parse(resp.content)
    if parsed.bozo and not parsed.entries:
        raise TransmogError("лента не распарсилась: %s" % parsed.bozo_exception)
    if not parsed.entries:
        raise TransmogError("Reddit отдал ленту без единого поста — публиковать нечего")
    return parsed


def pick_posts(parsed, posted, cfg):
    known = set(posted)
    fresh = []
    for entry in parsed.entries:
        post_id = entry.get("id")
        link = entry.get("link")
        if not post_id or not link:
            continue
        if post_id in known:
            continue
        fresh.append(
            {
                "id": post_id,
                "link": link,
                "title": (entry.get("title") or "без заголовка").strip(),
                "author": (entry.get("author") or "").strip(),
            }
        )
        if len(fresh) >= cfg["count"]:
            break
    return fresh


def escape_md(text):
    """Гасим маркдаун в тексте от пользователей.

    Без этого заголовок вида «...seen some sh**» съезжает: его звёздочки
    закрывают наш жирный шрифт раньше времени. Ссылку экранировать нельзя —
    она должна остаться голой, иначе Discord не построит превью.
    """
    for ch in ("\\", "*", "_", "~", "`", "|"):
        text = text.replace(ch, "\\" + ch)
    return text


def format_post(index, post):
    author = (" — %s" % escape_md(post["author"])) if post["author"] else ""
    # Ссылка обязательно голая, отдельной строкой: только так Discord строит превью.
    return "**%d. %s**%s\n%s" % (index, escape_md(post["title"]), author, post["link"])


def build_messages(posts, cfg):
    """Список текстов сообщений: одно общее или шапка плюс по одному на пост."""
    if cfg["message_mode"] == "separate":
        chunks = [cfg["header"]]
        chunks += [format_post(i, p) for i, p in enumerate(posts, 1)]
        return chunks
    body = "\n\n".join(format_post(i, p) for i, p in enumerate(posts, 1))
    return ["%s\n\n%s" % (cfg["header"], body)]


def _check_fatal(resp, what):
    if resp.status_code in FATAL_WEBHOOK_CODES:
        raise TransmogError(
            "Discord ответил %s при %s — вебхук недействителен. Пересоздайте его "
            "в настройках канала и обновите секрет TRANSMOG_WEBHOOK. Ответ: %s"
            % (resp.status_code, what, resp.text[:200])
        )


def send_message(webhook, content, cfg):
    """Отправляет одно сообщение, возвращает его id."""
    for attempt in range(1, 4):
        resp = requests.post(
            webhook + "?wait=true",
            json={"content": content, "allowed_mentions": {"parse": []}},
            timeout=cfg["request_timeout"],
        )
        _check_fatal(resp, "отправке сообщения")

        if resp.status_code == 429:
            try:
                wait = float(resp.json().get("retry_after", 5))
            except (ValueError, TypeError, AttributeError):
                wait = 5.0
            if wait > 300:
                wait /= 1000.0
            wait = min(wait, 60) + 0.5
            logging.warning("429 от Discord, жду %.1f с (попытка %s)", wait, attempt)
            time.sleep(wait)
            continue

        if 200 <= resp.status_code < 300:
            return str(resp.json().get("id", ""))

        if resp.status_code >= 500:
            logging.warning("Discord ответил %s (попытка %s), повторю", resp.status_code, attempt)
            time.sleep(2 * attempt)
            continue

        raise TransmogError(
            "Discord отказал: %s %s" % (resp.status_code, resp.text[:300])
        )

    raise TransmogError("не удалось отправить сообщение после трёх попыток")


def count_embeds(webhook, message_id, cfg):
    """Сколько превью Discord успел построить. Диагностика, не приговор."""
    try:
        resp = requests.get(
            "%s/messages/%s" % (webhook, message_id), timeout=cfg["request_timeout"]
        )
        if not (200 <= resp.status_code < 300):
            return None
        embeds = resp.json().get("embeds") or []
        kinds = {}
        for emb in embeds:
            kind = emb.get("type", "?")
            if emb.get("video"):
                kind += "+видео"
            elif emb.get("image") or emb.get("thumbnail"):
                kind += "+картинка"
            kinds[kind] = kinds.get(kind, 0) + 1
        return len(embeds), kinds
    except (requests.RequestException, ValueError):
        return None


def run():
    cfg = load_config()
    dry_run = os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes")
    webhook = (os.environ.get("TRANSMOG_WEBHOOK") or "").strip().rstrip("/")
    if not webhook and not dry_run:
        raise TransmogError("нет переменной окружения TRANSMOG_WEBHOOK — отправлять некуда")

    try:
        parsed = fetch_feed(cfg)
    except RedditBusy as exc:
        logging.info("%s — не настаиваю, вернусь завтра", exc)
        return 0

    logging.info("в ленте постов: %s", len(parsed.entries))

    posted, first_run = load_posted()
    if first_run:
        # Архивом это не грозит: за прогон уходит не больше cfg["count"] постов.
        logging.info("первый запуск: истории нет, беру сегодняшний топ-%s", cfg["count"])

    posts = pick_posts(parsed, posted, cfg)
    logging.info("новых постов к отправке: %s", len(posts))
    for post in posts:
        logging.info("  %s  %s — %s", post["id"], post["title"][:58], post["author"])

    if not posts:
        logging.info("всё из сегодняшнего топа уже отправляли — сообщений не будет")
        return 0

    messages = build_messages(posts, cfg)
    logging.info("режим «%s»: сообщений к отправке %s", cfg["message_mode"], len(messages))

    if dry_run:
        for text in messages:
            logging.info("DRY-RUN, не отправляю:\n%s", text)
        return 0

    sent_ids = []
    for index, text in enumerate(messages):
        message_id = send_message(webhook, text, cfg)
        sent_ids.append(message_id)
        logging.info("отправлено сообщение %s (%s символов)", message_id, len(text))
        if index < len(messages) - 1:
            time.sleep(cfg["delay_between_posts"])

    # Состояние пишем только после успешной отправки.
    posted.extend(p["id"] for p in posts)
    save_posted(posted, cfg["history_size"])
    logging.info("в истории теперь %s постов", len(posted[-cfg["history_size"]:]))

    if cfg["verify_embeds"] and sent_ids:
        time.sleep(cfg["embed_check_delay"])
        for message_id in sent_ids:
            result = count_embeds(webhook, message_id, cfg)
            if result is None:
                logging.warning("не смог перечитать сообщение %s для проверки превью", message_id)
                continue
            total, kinds = result
            logging.info(
                "превью в сообщении %s: %s (%s)",
                message_id, total,
                ", ".join("%s×%s" % (v, k) for k, v in sorted(kinds.items())) or "нет",
            )
            if total == 0:
                logging.warning(
                    "Discord не построил ни одного превью — проверьте, что ссылки идут "
                    "голыми, без маркдауна, и что у вебхука не отключены превью"
                )
    return 0


def main():
    setup_logging()
    try:
        return run()
    except TransmogError as exc:
        logging.error("%s", exc)
        return 1
    except Exception:
        logging.exception("непредвиденная ошибка")
        return 1


if __name__ == "__main__":
    sys.exit(main())
