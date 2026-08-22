#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ежедневная подборка трансмогов из r/Transmogrification в Discord.

Вебхук читается ТОЛЬКО из переменной окружения TRANSMOG_WEBHOOK.

Формат намеренно минимальный: короткая шапка, дальше каждый пост
отдельным сообщением. В карточке только название и само изображение —
ни текста поста, ни автора, ни счётчиков голосов.

Картинки и галереи уходят СВОЕЙ карточкой: заголовок плюс прямая ссылка
на исходник i.redd.it. Автоматическое превью Discord тут не годится —
оно тянет ещё og:description, то есть весь текст поста (у трансмогов это
обычно длинный список шмота), и карточка раздувается.

Видео уходит голой ссылкой на зеркало (link_host в конфиге): плеер умеет
строить только автоматическое превью Discord, в собственный эмбед бота
видео вставить нельзя. Ценой этого в карточке видео остаётся текст
от зеркала.

Прямые ссылки на reddit.com не годятся ни для чего: Discord берёт у них
share.redd.it/preview/post/<id> — сгенерированный баннер, где работа
втиснута в полосу по центру, а вокруг впечатаны логотип, название
сабреддита и счётчики голосов.

К зеркалу ходит только сам Discord, когда разворачивает ссылку на видео.
Бот его не дёргает: единственный сетевой запрос бота за прогон — RSS Reddit.

Про Reddit: один запрос в сутки, честный User-Agent, при 429 не долбим,
а спокойно выходим и пробуем завтра.
"""

import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse, urlunparse

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
    # Зеркало для всех типов постов. Оно отдаёт Discord ссылку на исходный
    # файл с i.redd.it — то есть саму работу автора: картинка чистая,
    # гифка анимируется, видео разворачивается плеером.
    #
    # Прямые ссылки на reddit.com сюда не годятся: Discord берёт у них
    # share.redd.it/preview/post/<id> — сгенерированный баннер 1120x584,
    # где работа втиснута в полосу по центру, а вокруг впечатаны название
    # сабреддита, логотип, счётчики голосов и кнопка плея поверх видео.
    #
    # Ляжет vxreddit — поставьте сюда rxddit.com, код трогать не нужно.
    "link_host": "vxreddit.com",
    "header": "Трансмоги дня",
    "request_timeout": 30,
    "delay_between_posts": 1.0,
    # Discord подтягивает превью асинхронно, ему нужно дать время.
    "embed_check_delay": 6.0,
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


def entry_html(entry):
    if entry.get("content"):
        return entry["content"][0].get("value", "") or ""
    return entry.get("summary", "") or ""


def detect_kind(entry):
    """Видео, галерея или картинка — по ссылке на медиа внутри записи.

    Нужно только для лога и для проверки эмбедов: у видеопоста мы ждём
    в карточке блок video, у остальных — картинку.
    """
    html = entry_html(entry)
    if "v.redd.it" in html:
        return "видео"
    if "/gallery/" in html:
        return "галерея"
    return "картинка"


# Прямая ссылка на исходник в ленте есть только у одиночных картинок.
DIRECT_RE = re.compile(r"href=[\"'](https://i\.redd\.it/[^\"'?]+)")
# У галерей её нет, но превью лежит под тем же именем файла,
# поэтому preview.redd.it/<файл> превращается в i.redd.it/<файл>.
# external-preview сюда намеренно не подходит: там имя закодировано,
# и это всегда видеопост, который идёт другим путём.
PREVIEW_RE = re.compile(r"https://preview\.redd\.it/([^\"'?&]+)")


def direct_image(entry):
    """Ссылка на исходный файл работы — без баннеров и подписей."""
    html = entry_html(entry)
    match = DIRECT_RE.search(html)
    if match:
        return match.group(1)
    for thumb in entry.get("media_thumbnail") or []:
        match = PREVIEW_RE.search(thumb.get("url") or "")
        if match:
            return "https://i.redd.it/" + match.group(1)
    match = PREVIEW_RE.search(html)
    if match:
        return "https://i.redd.it/" + match.group(1)
    return ""


def mirror_link(link, host):
    """Тот же путь, только домен другой."""
    parts = urlparse(link)
    return urlunparse(parts._replace(netloc=host))


def pick_posts(parsed, posted, cfg):
    known = set(posted)
    chosen = []
    for entry in parsed.entries:
        if len(chosen) >= cfg["count"]:
            break
        post_id = entry.get("id")
        link = entry.get("link")
        if not post_id or not link or post_id in known:
            continue
        kind = detect_kind(entry)
        chosen.append(
            {
                "id": post_id,
                "title": (entry.get("title") or "без заголовка").strip(),
                "kind": kind,
                # url — через зеркало (для Discord), source_url — оригинал,
                # на него ведёт заголовок в нашей карточке.
                "url": mirror_link(link, cfg["link_host"]),
                "source_url": link,
                "image": direct_image(entry),
            }
        )
    return chosen


def _check_fatal(resp, what):
    if resp.status_code in FATAL_WEBHOOK_CODES:
        raise TransmogError(
            "Discord ответил %s при %s — вебхук недействителен. Пересоздайте его "
            "в настройках канала и обновите секрет TRANSMOG_WEBHOOK. Ответ: %s"
            % (resp.status_code, what, resp.text[:200])
        )


def send_message(webhook, payload, cfg):
    """Отправляет одно сообщение (payload как есть), возвращает его id."""
    body = dict(payload)
    body.setdefault("allowed_mentions", {"parse": []})
    for attempt in range(1, 4):
        resp = requests.post(
            webhook + "?wait=true",
            json=body,
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

        raise TransmogError("Discord отказал: %s %s" % (resp.status_code, resp.text[:300]))

    raise TransmogError("не удалось отправить сообщение после трёх попыток")


def read_embeds(webhook, message_id, cfg):
    """Что Discord реально построил. Список эмбедов или None, если не прочиталось."""
    try:
        resp = requests.get(
            "%s/messages/%s" % (webhook, message_id), timeout=cfg["request_timeout"]
        )
        if not (200 <= resp.status_code < 300):
            return None
        return resp.json().get("embeds") or []
    except (requests.RequestException, ValueError):
        return None


def describe_embed(embed):
    parts = ["type=%s" % embed.get("type")]
    provider = (embed.get("provider") or {}).get("name")
    if provider:
        parts.append("provider=%s" % provider)
    for key in ("video", "image", "thumbnail"):
        block = embed.get(key)
        if block:
            # Хост важнее размеров: i.redd.it — это сама работа автора,
            # а share.redd.it — сгенерированный баннер с впечатанными
            # логотипом, названием сабреддита и счётчиками голосов.
            host = urlparse(block.get("url") or "").netloc or "?"
            parts.append(
                "%s=%sx%s@%s" % (key, block.get("width"), block.get("height"), host)
            )
    return ", ".join(parts)


def verify(webhook, sent, cfg):
    """Разбирает эмбеды отправленных сообщений. Возвращает список замечаний."""
    time.sleep(cfg["embed_check_delay"])
    complaints = []
    for post, message_id in sent:
        embeds = read_embeds(webhook, message_id, cfg)
        if embeds is None:
            logging.warning("не смог перечитать сообщение %s", message_id)
            continue
        if not embeds:
            logging.warning("[%s] %s — превью не построилось вовсе", post["kind"], message_id)
            complaints.append((post, "превью нет"))
            continue
        for embed in embeds:
            logging.info("[%s] %s: %s", post["kind"], message_id, describe_embed(embed))
        if post["kind"] == "видео" and not any(e.get("video") for e in embeds):
            logging.warning(
                "[видео] %s — в эмбеде нет блока video, будет статичный кадр: %s",
                message_id, post["url"],
            )
            complaints.append((post, "нет блока video"))
    return complaints


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
    logging.info("постов к отправке: %s (через %s)", len(posts), cfg["link_host"])
    for post in posts:
        logging.info("  %s  [%-8s]  %s", post["id"], post["kind"], post["title"][:50])

    if not posts:
        logging.info("всё из сегодняшнего топа уже отправляли — сообщений не будет")
        return 0

    # Ни одного запроса к зеркалу: картинку берём из самой ленты,
    # а видео Discord подтянет с зеркала сам, когда развернёт ссылку.
    for post in posts:
        if post["kind"] != "видео" and not post["image"]:
            raise TransmogError(
                "для %s не нашлось ссылки на исходник в ленте — похоже, Reddit "
                "сменил разметку записи" % post["id"]
            )

    if dry_run:
        logging.info("DRY-RUN, не отправляю. Шапка: %s", cfg["header"])
        for post in posts:
            how = "ссылка на зеркало (плеер)" if post["kind"] == "видео" else "своя карточка"
            logging.info(
                "DRY-RUN [%s] %s: %s | %s", post["kind"], how,
                post["title"][:40], (post["image"] or post["url"])[:66],
            )
        return 0

    # Шапка отдельным сообщением, дальше по одному посту на сообщение:
    # в общем сообщении Discord схлопывает превью в узкие карточки.
    send_message(webhook, {"content": cfg["header"]}, cfg)
    time.sleep(cfg["delay_between_posts"])

    sent = []
    for index, post in enumerate(posts):
        if post["kind"] == "видео":
            # Видео умеет проигрывать только автоматическое превью Discord,
            # в собственный эмбед бота плеер вставить нельзя. Поэтому здесь
            # шлём голую ссылку и миримся с текстом от зеркала.
            payload = {"content": post["url"]}
            how = "ссылка (плеер)"
        else:
            # Своя карточка: только название и изображение. Ни описания поста,
            # ни автора, ни счётчиков — именно от них карточка распухала.
            payload = {
                "embeds": [
                    {
                        "title": post["title"][:256],
                        "url": post["source_url"],
                        "image": {"url": post["image"]},
                    }
                ]
            }
            how = "своя карточка"
        message_id = send_message(webhook, payload, cfg)
        sent.append((post, message_id))
        logging.info("отправлено %s [%s] %s: %s", message_id, post["kind"], how, post["title"][:44])
        if index < len(posts) - 1:
            time.sleep(cfg["delay_between_posts"])

    posted.extend(p["id"] for p in posts)
    save_posted(posted, cfg["history_size"])
    logging.info("в истории теперь %s постов", len(posted[-cfg["history_size"]:]))

    if cfg["verify_embeds"]:
        complaints = verify(webhook, sent, cfg)
        if complaints:
            logging.warning("замечаний по превью: %s", len(complaints))
            for post, why in complaints:
                logging.warning("  %s — %s", post["url"], why)
        else:
            logging.info("превью в порядке у всех %s сообщений", len(sent))
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
