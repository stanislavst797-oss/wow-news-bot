#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Еженедельный скриншот тир-листа Mythic+ с archon.gg в Discord.

Вебхук читается ТОЛЬКО из переменной окружения TIERLIST_WEBHOOK.

Логика публикации: одно сообщение, которое обновляется каждую неделю.
ID лежит в tierlist_state.json. Если сообщение удалили руками — создаём новое.

Скриншоты хрупкие, поэтому любая неуверенность в результате — это красный
прогон с внятной ошибкой, а не пустая картинка в канале.
"""

import io
import json
import logging
import os
import sys
import time
from pathlib import Path

import requests
from PIL import Image, ImageStat
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "tierlist_config.json"
STATE_PATH = ROOT / "tierlist_state.json"
SHOT_PATH = ROOT / "tierlist.png"

DEFAULTS = {
    "url": "https://www.archon.gg/wow/tier-list/dps-rankings/mythic-plus/10/all-dungeons/this-week",
    # Класс не хэшированный (BEM), в отличие от CSS-модулей вида Layout_x__a1b2 —
    # такой селектор переживает пересборку сайта.
    "section_selector": ".builds-tier-list-section",
    "tier_selector": ".builds-tier-list-section__tier-heading",
    "spec_selector": ".builds-tier-list-section__spec-contents",
    "label_selector": ".builds-tier-list-section__spec-label",
    "viewport_width": 1050,
    "viewport_height": 1200,
    "device_scale_factor": 2,
    "nav_timeout_ms": 60000,
    "data_timeout_ms": 45000,
    # Пороги «данные действительно отрисовались»
    "min_tiers": 3,
    "min_specs": 15,
    # Пороги «картинка не пустая»
    "min_width": 500,
    "min_height": 250,
    "min_bytes": 20000,
    "min_unique_colors": 60,
    "min_stddev": 12.0,
    "caption_title": "Тир-лист Mythic+ · +10 · все подземелья · за неделю",
    "source_name": "Archon.gg",
    "embed_color": 10038562,
    "request_timeout": 60,
}

# Прокрутка нужна: секции подгружаются лениво через IntersectionObserver
# и без неё на странице остаются пустые заглушки высотой 500px.
JS_SCROLL = """
async () => {
    const sleep = ms => new Promise(r => setTimeout(r, ms));
    for (let y = 0; y < document.body.scrollHeight; y += 300) {
        window.scrollTo(0, y);
        await sleep(90);
    }
    window.scrollTo(0, 0);
    await sleep(400);
}
"""

# Иконки специализаций тоже ленивые (loading="lazy") — без этой проверки
# можно снять тир-лист с пустыми кружками вместо иконок.
JS_IMAGES_READY = """
sel => {
    const root = document.querySelector(sel);
    if (!root) return false;
    const imgs = [...root.querySelectorAll('img')];
    return imgs.length > 0 && imgs.every(i => i.complete && i.naturalWidth > 0);
}
"""

# Названия специализаций на сайте видно только в подсказке (title у <li>).
# Подставляем их вместо надписи "Score" — она всё равно одинаковая у всех.
JS_INJECT_NAMES = """
cfg => {
    const root = document.querySelector(cfg.section);
    if (!root) return 0;
    let done = 0;
    root.querySelectorAll(cfg.spec).forEach(cell => {
        const holder = cell.closest('li') || cell.parentElement;
        const name = (holder && holder.getAttribute('title'))
                  || (cell.querySelector('img') || {}).alt;
        if (!name) return;
        const label = cell.querySelector(cfg.label);
        if (!label) return;
        label.textContent = name;
        done++;
    });
    return done;
}
"""

CSS_CLEANUP = """
  /* Реклама и длинные пояснения в кадр не нужны */
  [class*="AdPlacement"], [class*="stickyFooterAd"], [class*="PlaywireAd"],
  .builds-tier-list-section__metric-description,
  .hide-on-compact { display: none !important; }

  /* Названия специализаций подставлены вместо "Score" — дать им место */
  .builds-tier-list-section__spec-label {
      white-space: nowrap !important;
      font-size: 13px !important;
      font-weight: 600 !important;
      letter-spacing: 0 !important;
      text-transform: none !important;
      opacity: 1 !important;
  }
  .builds-tier-list-section__spec-contents { padding-right: 14px !important; }
"""


class TierListError(Exception):
    """Что-то пошло не так настолько, что публиковать нечего."""


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
            logging.warning("tierlist_config.json не читается (%s), беру умолчания", exc)
    return cfg


def load_state():
    if not STATE_PATH.exists():
        return {}
    try:
        with STATE_PATH.open(encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError) as exc:
        logging.warning("tierlist_state.json повреждён (%s) — начну с чистого листа", exc)
        return {}


def save_state(state):
    tmp = STATE_PATH.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    tmp.replace(STATE_PATH)


def take_screenshot(cfg, out_path):
    """Снимает секцию тир-листа. Кидает TierListError, если данных нет."""
    sel = cfg["section_selector"]
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--disable-dev-shm-usage", "--no-sandbox"])
        try:
            ctx = browser.new_context(
                viewport={"width": cfg["viewport_width"], "height": cfg["viewport_height"]},
                device_scale_factor=cfg["device_scale_factor"],
                locale="en-US",
            )
            page = ctx.new_page()
            logging.info("открываю %s", cfg["url"])
            page.goto(cfg["url"], wait_until="domcontentloaded", timeout=cfg["nav_timeout_ms"])

            page.evaluate(JS_SCROLL)

            # Ждём именно данные, а не фиксированную паузу.
            try:
                page.wait_for_selector(sel, state="attached", timeout=cfg["data_timeout_ms"])
                page.wait_for_function(
                    """cfg => {
                        const root = document.querySelector(cfg.section);
                        if (!root) return false;
                        return root.querySelectorAll(cfg.tier).length >= cfg.minTiers
                            && root.querySelectorAll(cfg.spec).length >= cfg.minSpecs;
                    }""",
                    arg={
                        "section": sel,
                        "tier": cfg["tier_selector"],
                        "spec": cfg["spec_selector"],
                        "minTiers": cfg["min_tiers"],
                        "minSpecs": cfg["min_specs"],
                    },
                    timeout=cfg["data_timeout_ms"],
                )
                page.wait_for_function(JS_IMAGES_READY, arg=sel, timeout=cfg["data_timeout_ms"])
            except PlaywrightTimeout as exc:
                stats = page.evaluate(
                    """cfg => {
                        const root = document.querySelector(cfg.section);
                        return {
                            found: !!root,
                            tiers: root ? root.querySelectorAll(cfg.tier).length : 0,
                            specs: root ? root.querySelectorAll(cfg.spec).length : 0,
                            placeholders: document.querySelectorAll('.lazyload-placeholder').length
                        };
                    }""",
                    arg={"section": sel, "tier": cfg["tier_selector"], "spec": cfg["spec_selector"]},
                )
                raise TierListError(
                    "таблица не отрисовалась за %s мс. Селектор «%s»: найден=%s, тиров=%s (нужно %s), "
                    "спеков=%s (нужно %s), незагруженных ленивых блоков=%s. "
                    "Похоже, сайт изменил вёрстку — проверьте селекторы в tierlist_config.json"
                    % (
                        cfg["data_timeout_ms"], sel, stats["found"], stats["tiers"], cfg["min_tiers"],
                        stats["specs"], cfg["min_specs"], stats["placeholders"],
                    )
                ) from exc

            heading = page.evaluate(
                """sel => {
                    const s = document.querySelector(sel).closest('section');
                    const h = s && s.querySelector('h1,h2,h3');
                    return h ? h.innerText.trim() : null;
                }""",
                sel,
            )
            counts = page.evaluate(
                """cfg => {
                    const root = document.querySelector(cfg.section);
                    return {tiers: root.querySelectorAll(cfg.tier).length,
                            specs: root.querySelectorAll(cfg.spec).length};
                }""",
                {"section": sel, "tier": cfg["tier_selector"], "spec": cfg["spec_selector"]},
            )
            logging.info(
                "секция «%s»: тиров %s, специализаций %s", heading, counts["tiers"], counts["specs"]
            )

            page.add_style_tag(content=CSS_CLEANUP)
            named = page.evaluate(
                JS_INJECT_NAMES,
                {"section": sel, "spec": cfg["spec_selector"], "label": cfg["label_selector"]},
            )
            if named < counts["specs"]:
                logging.warning(
                    "подписал названиями %s из %s специализаций — возможно, сайт "
                    "поменял атрибут title у ячеек",
                    named, counts["specs"],
                )
            else:
                logging.info("подписал названиями все %s специализаций", named)

            page.wait_for_timeout(500)
            target = page.locator(sel).first.locator("xpath=ancestor::section[1]")
            target.screenshot(path=str(out_path))
            logging.info("скриншот снят: %s", out_path.name)
            return heading
        finally:
            browser.close()


def validate_image(path, cfg):
    """Проверяет, что картинка не пустая и не почти одноцветная."""
    size = path.stat().st_size if path.exists() else 0
    if size < cfg["min_bytes"]:
        raise TierListError(
            "картинка подозрительно маленькая: %s байт (порог %s). "
            "Скорее всего, снялся пустой блок" % (size, cfg["min_bytes"])
        )

    with Image.open(path) as img:
        img = img.convert("RGB")
        width, height = img.size
        if width < cfg["min_width"] or height < cfg["min_height"]:
            raise TierListError(
                "картинка слишком мелкая: %sx%s (минимум %sx%s)"
                % (width, height, cfg["min_width"], cfg["min_height"])
            )
        small = img.copy()
        small.thumbnail((240, 240))
        colors = small.getcolors(maxcolors=200000) or []
        unique = len(colors)
        stddev = ImageStat.Stat(small.convert("L")).stddev[0]

    logging.info(
        "картинка: %sx%s, %s КБ, уникальных цветов %s, разброс яркости %.1f",
        width, height, size // 1024, unique, stddev,
    )
    if unique < cfg["min_unique_colors"]:
        raise TierListError(
            "картинка почти одноцветная: уникальных цветов %s (порог %s) — "
            "похоже, снялся пустой или незагруженный блок" % (unique, cfg["min_unique_colors"])
        )
    if stddev < cfg["min_stddev"]:
        raise TierListError(
            "картинка почти однородная: разброс яркости %.1f (порог %s)"
            % (stddev, cfg["min_stddev"])
        )
    return width, height


def build_embed(cfg):
    return {
        "title": cfg["caption_title"],
        "url": cfg["url"],
        "description": "Источник: [%s](%s)" % (cfg["source_name"], cfg["url"]),
        "color": cfg["embed_color"],
        "image": {"url": "attachment://tierlist.png"},
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def _multipart(cfg, image_bytes):
    payload = {
        "embeds": [build_embed(cfg)],
        "attachments": [{"id": 0, "filename": "tierlist.png"}],
        "allowed_mentions": {"parse": []},
    }
    files = {
        "payload_json": (None, json.dumps(payload, ensure_ascii=False), "application/json"),
        "files[0]": ("tierlist.png", image_bytes, "image/png"),
    }
    return files


def _check_fatal(resp, what):
    if resp.status_code in (401, 403):
        raise TierListError(
            "Discord ответил %s при %s — вебхук недействителен. Пересоздайте его "
            "в настройках канала и обновите секрет TIERLIST_WEBHOOK. Ответ: %s"
            % (resp.status_code, what, resp.text[:200])
        )


def post_new(webhook, cfg, image_bytes):
    """Создаёт новое сообщение и возвращает его id."""
    resp = requests.post(
        webhook + "?wait=true",
        files=_multipart(cfg, image_bytes),
        timeout=cfg["request_timeout"],
    )
    _check_fatal(resp, "создании сообщения")
    if resp.status_code == 404:
        raise TierListError(
            "Discord ответил 404 при создании сообщения — вебхук удалён. "
            "Пересоздайте его и обновите секрет TIERLIST_WEBHOOK"
        )
    if not (200 <= resp.status_code < 300):
        raise TierListError(
            "Discord отказал при создании сообщения: %s %s" % (resp.status_code, resp.text[:300])
        )
    message_id = str(resp.json().get("id", ""))
    if not message_id:
        raise TierListError("Discord не вернул id созданного сообщения")
    return message_id


def edit_existing(webhook, cfg, image_bytes, message_id):
    """Обновляет сообщение. Возвращает False, если его больше нет."""
    resp = requests.patch(
        "%s/messages/%s" % (webhook, message_id),
        files=_multipart(cfg, image_bytes),
        timeout=cfg["request_timeout"],
    )
    _check_fatal(resp, "обновлении сообщения")
    if resp.status_code == 404:
        logging.warning("сообщение %s больше не существует — создам новое", message_id)
        return False
    if not (200 <= resp.status_code < 300):
        raise TierListError(
            "Discord отказал при обновлении: %s %s" % (resp.status_code, resp.text[:300])
        )
    return True


def publish(webhook, cfg, image_bytes, state):
    message_id = state.get("message_id")
    if message_id:
        logging.info("обновляю существующее сообщение %s", message_id)
        if edit_existing(webhook, cfg, image_bytes, message_id):
            state["action"] = "updated"
            return state
    else:
        logging.info("сохранённого сообщения нет — создаю новое")

    new_id = post_new(webhook, cfg, image_bytes)
    logging.info("создано сообщение %s", new_id)
    state["message_id"] = new_id
    state["action"] = "created"
    return state


def run():
    cfg = load_config()
    dry_run = os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes")
    webhook = (os.environ.get("TIERLIST_WEBHOOK") or "").strip().rstrip("/")
    if not webhook and not dry_run:
        logging.error("нет переменной окружения TIERLIST_WEBHOOK — отправлять некуда")
        return 1

    take_screenshot(cfg, SHOT_PATH)
    validate_image(SHOT_PATH, cfg)

    if dry_run:
        logging.info("DRY-RUN: в Discord ничего не отправляю")
        return 0

    image_bytes = SHOT_PATH.read_bytes()
    state = load_state()
    state = publish(webhook, cfg, image_bytes, state)
    state["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    state["source_url"] = cfg["url"]
    save_state(state)
    logging.info("готово: сообщение %s (%s)", state["message_id"], state["action"])
    return 0


def main():
    setup_logging()
    try:
        return run()
    except TierListError as exc:
        logging.error("%s", exc)
        return 1
    except (PlaywrightError, requests.RequestException) as exc:
        logging.error("сбой браузера или сети: %s", exc)
        return 1
    except Exception:
        logging.exception("непредвиденная ошибка")
        return 1


if __name__ == "__main__":
    sys.exit(main())
