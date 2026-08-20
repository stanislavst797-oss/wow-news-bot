#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ежедневные скриншоты тир-листов Mythic+ с archon.gg в Discord.

Роли (ДД, танки, хилы) описаны списком targets в tierlist_config.json — каждая
со своим url, подписью и именем секрета с вебхуком. Вебхуки читаются ТОЛЬКО
из переменных окружения, в файлы не попадают. Четвёртая роль добавляется
правкой конфига, без изменения кода.

Логика публикации: у каждой роли своё сообщение с двумя картинками, которое обновляется
каждый день. ID ролей лежат в tierlist_state.json. Если сообщение удалили руками —
создаём новое.

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

DEFAULTS = {
    "url": "https://www.archon.gg/wow/tier-list/dps-rankings/mythic-plus/10/all-dungeons/this-week",
    # Класс не хэшированный (BEM), в отличие от CSS-модулей вида Layout_x__a1b2 —
    # такой селектор переживает пересборку сайта.
    "section_selector": ".builds-tier-list-section",
    "tier_selector": ".builds-tier-list-section__tier-heading",
    "spec_selector": ".builds-tier-list-section__spec-contents",
    "label_selector": ".builds-tier-list-section__spec-label",
    # Вторая картинка — таблица рейтингов под тир-листом.
    "rankings_selector": "table.react-table",
    "rankings_rows": 15,
    "min_rankings_rows": 8,
    # Рекламные слои. Классы у Archon хэшированные (Advertisements_x__a1b2),
    # поэтому цепляемся за устойчивый префикс через [class*=...].
    "ad_selectors": [
        '[class*="AdPlacement"]',
        '[class*="stickyFooterAd"]',
        '[class*="PlaywireAd"]',
        '[class*="Advertisements_"]',
        '[class*="containerSidebar"]',
        'iframe[src*="ads"]',
        'iframe[src*="doubleclick"]',
    ],
    "viewport_width": 1050,
    "viewport_height": 1200,
    "device_scale_factor": 2,
    "nav_timeout_ms": 60000,
    "data_timeout_ms": 45000,
    "ad_wait_ms": 8000,
    # Пороги «данные действительно отрисовались»
    "min_tiers": 3,
    "min_specs": 15,
    # Пороги «картинка не пустая»
    "min_width": 500,
    "min_height": 250,
    "min_bytes": 20000,
    "min_unique_colors": 60,
    "min_stddev": 12.0,
    "caption_title": "Тир-лист Mythic+ · ключи +7 и выше · все подземелья · за 14 дней",
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

# Сначала пробуем закрыть рекламу штатным крестиком — так же, как это сделал бы
# человек. Что не закрылось, добиваем стилями (CSS_CLEANUP) и потом проверяем,
# что на кадре ничего не осталось поверх (JS_OVERLAP).
JS_CLOSE_ADS = """
() => {
    const rx = /^(close ad|close|закрыть|×|✕|✖|x)$/i;
    let clicked = 0;
    document.querySelectorAll('button, a, [role="button"], [class*="close" i]').forEach(el => {
        const label = (el.innerText || el.getAttribute('aria-label')
                    || el.getAttribute('title') || '').trim();
        if (!rx.test(label)) return;
        const r = el.getBoundingClientRect();
        if (r.width < 1 || r.height < 1) return;
        try { el.click(); clicked++; } catch (e) { /* не кликнулось — переживём */ }
    });
    return clicked;
}
"""

# Сайт держит в DOM все строки, но показывает только первые семь —
# остальные раскрывает кнопка «Show More». Жмём её штатно, а не боремся с CSS.
JS_EXPAND_ROWS = """
cfg => {
    const table = document.querySelector(cfg.table);
    if (!table) return false;
    const scope = table.closest('section') || document;
    const btn = [...scope.querySelectorAll('button, a')].find(
        e => /show more|показать ещё/i.test((e.innerText || '').trim()));
    if (!btn) return false;
    btn.click();
    return true;
}
"""

# Оставляем в кадре только первые N строк.
JS_LIMIT_ROWS = """
cfg => {
    const table = document.querySelector(cfg.table);
    if (!table || !table.tBodies.length) return 0;
    const rows = [...table.tBodies[0].rows];
    rows.forEach((tr, i) => { tr.style.display = i < cfg.limit ? 'table-row' : 'none'; });
    return rows.filter(tr => getComputedStyle(tr).display !== 'none').length;
}
"""

# Не закрывает ли что-нибудь рекламное итоговый кадр.
JS_OVERLAP = """
cfg => {
    const anchor = document.querySelector(cfg.anchor);
    if (!anchor) return [{cls: 'секция не найдена', w: 0, h: 0}];
    const sec = anchor.closest('section') || anchor;
    const r = sec.getBoundingClientRect();
    const bad = [];
    document.querySelectorAll(cfg.ads.join(',')).forEach(el => {
        const cs = getComputedStyle(el);
        if (cs.display === 'none' || cs.visibility === 'hidden' || cs.opacity === '0') return;
        const b = el.getBoundingClientRect();
        if (b.width < 2 || b.height < 2) return;
        const overlaps = !(b.right <= r.left || b.left >= r.right
                        || b.bottom <= r.top || b.top >= r.bottom);
        if (overlaps) bad.push({cls: (el.className || el.tagName).toString().slice(0, 60),
                                w: Math.round(b.width), h: Math.round(b.height)});
    });
    return bad;
}
"""

CSS_CLEANUP = """
  /* Реклама и длинные пояснения в кадр не нужны */
  [class*="AdPlacement"], [class*="stickyFooterAd"], [class*="PlaywireAd"],
  [class*="Advertisements_"], [class*="containerSidebar"],
  .builds-tier-list-section__metric-description,
  .hide-on-compact { display: none !important; }

  /* Таблица обрезана до первых строк — кнопка «Show More» вводила бы в заблуждение */
  .react-table__wrapper ~ * button.react-button--style-gradient-rounded,
  section button.react-button--style-gradient-rounded { display: none !important; }

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
    """Возвращает (общие_настройки, список_целей).

    Цель — это роль со своим url, секретом и подписью. Чтобы добавить
    четвёртую роль, достаточно дописать объект в targets в конфиге.
    """
    raw = {}
    if CONFIG_PATH.exists():
        try:
            with CONFIG_PATH.open(encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, ValueError) as exc:
            logging.warning("tierlist_config.json не читается (%s), беру умолчания", exc)

    shared = dict(DEFAULTS)
    shared.update(raw.get("defaults") or {})

    targets = raw.get("targets")
    if not targets and raw.get("url"):
        # Конфиг старого формата — одна цель прямо в корне.
        targets = [dict(raw, role=raw.get("role", "dps"), name=raw.get("name", "ДД"))]
    if not targets:
        raise TierListError(
            "в tierlist_config.json нет ни одной цели: ожидается список targets "
            "с полями role, url, webhook_env, caption_title"
        )

    for i, target in enumerate(targets):
        for field in ("role", "url", "webhook_env", "caption_title"):
            if not target.get(field):
                raise TierListError(
                    "у цели №%s в tierlist_config.json не задано поле «%s»" % (i + 1, field)
                )
    return shared, targets


def resolve_target(shared, target):
    """Настройки конкретной роли: общие, поверх них — её собственные."""
    cfg = dict(shared)
    cfg.update(target)
    cfg.setdefault("name", cfg["role"])
    return cfg


def load_state(first_role):
    """Состояние по ролям: {"roles": {"dps": {"message_id": ...}, ...}}.

    Старый формат (один message_id в корне) переносится на первую роль,
    чтобы уже существующее сообщение продолжило обновляться, а не создалось заново.
    """
    if not STATE_PATH.exists():
        return {"roles": {}}
    try:
        with STATE_PATH.open(encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        logging.warning("tierlist_state.json повреждён (%s) — начну с чистого листа", exc)
        return {"roles": {}}

    if not isinstance(data, dict):
        return {"roles": {}}
    if isinstance(data.get("roles"), dict):
        return data
    if data.get("message_id"):
        logging.info(
            "переношу состояние старого формата: сообщение %s закрепляю за ролью «%s»",
            data["message_id"], first_role,
        )
        return {"roles": {first_role: {k: v for k, v in data.items() if k != "action"}}}
    return {"roles": {}}


def save_state(state):
    tmp = STATE_PATH.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    tmp.replace(STATE_PATH)


def _assert_not_covered(page, cfg, anchor, name):
    """Убеждается, что поверх будущего кадра не осталось рекламы."""
    bad = page.evaluate(JS_OVERLAP, {"anchor": anchor, "ads": cfg["ad_selectors"]})
    if bad:
        what = ", ".join("%s (%sx%s)" % (b["cls"], b["w"], b["h"]) for b in bad[:4])
        raise TierListError(
            "на кадре «%s» поверх содержимого осталась реклама: %s. "
            "Крестик её не убрал и CSS тоже — дополните ad_selectors в tierlist_config.json"
            % (name, what)
        )


def take_screenshots(cfg, tier_path, rank_path):
    """Снимает тир-лист и таблицу рейтингов. Кидает TierListError, если данных нет."""
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

            # Липкий баннер появляется позже данных, поэтому ждём именно крестик:
            # раньше него закрывать нечего, а ждать всю рекламу бесполезно —
            # боковые блоки есть на странице с самого начала.
            try:
                page.wait_for_function(
                    """() => [...document.querySelectorAll('button, a, [role="button"]')].some(e => {
                        const t = (e.innerText || e.getAttribute('aria-label') || '').trim();
                        return /^(close ad|close|закрыть)$/i.test(t)
                            && e.getBoundingClientRect().height > 0;
                    })""",
                    timeout=cfg["ad_wait_ms"],
                )
            except PlaywrightTimeout:
                logging.info("крестик закрытия рекламы не появился — обойдусь стилями")

            # Сначала штатный крестик, как сделал бы человек, остатки — стилями.
            closed = page.evaluate(JS_CLOSE_ADS)
            if closed:
                page.wait_for_timeout(400)
            logging.info("закрыто рекламных блоков крестиком: %s", closed)
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

            # Вторая картинка: таблица рейтингов. Тоже ждём данные, а не паузу.
            rank_sel = cfg["rankings_selector"]
            try:
                page.wait_for_function(
                    """cfg => {
                        const t = document.querySelector(cfg.table);
                        return !!t && t.tBodies.length > 0
                            && t.tBodies[0].rows.length >= cfg.minRows;
                    }""",
                    arg={"table": rank_sel, "minRows": cfg["min_rankings_rows"]},
                    timeout=cfg["data_timeout_ms"],
                )
            except PlaywrightTimeout as exc:
                got = page.evaluate(
                    """sel => {
                        const t = document.querySelector(sel);
                        return {found: !!t,
                                rows: t && t.tBodies.length ? t.tBodies[0].rows.length : 0};
                    }""",
                    rank_sel,
                )
                raise TierListError(
                    "таблица рейтингов не набралась за %s мс. Селектор «%s»: найдена=%s, "
                    "строк=%s (нужно %s). Похоже, сайт изменил вёрстку — проверьте "
                    "rankings_selector в tierlist_config.json"
                    % (cfg["data_timeout_ms"], rank_sel, got["found"], got["rows"],
                       cfg["min_rankings_rows"])
                ) from exc

            table_info = page.evaluate(
                """sel => {
                    const t = document.querySelector(sel);
                    const s = t.closest('section');
                    const h = s && s.querySelector('h1,h2,h3');
                    return {heading: h ? h.innerText.trim() : null,
                            columns: [...t.querySelectorAll('thead th')]
                                       .map(e => e.innerText.replace(/\\s+/g, ' ').trim())
                                       .filter(Boolean),
                            total: t.tBodies[0].rows.length};
                }""",
                rank_sel,
            )
            # У ролей с коротким списком (танки, хилы) кнопки «Show More» нет:
            # там все строки видны сразу, и это не повод для тревоги.
            if page.evaluate(JS_EXPAND_ROWS, {"table": rank_sel}):
                page.wait_for_timeout(800)
            shown = page.evaluate(
                JS_LIMIT_ROWS, {"table": rank_sel, "limit": cfg["rankings_rows"]}
            )
            expected = min(table_info["total"], cfg["rankings_rows"])
            if shown < expected:
                logging.warning(
                    "в кадр попало %s строк из ожидаемых %s — список раскрылся не полностью",
                    shown, expected,
                )
            logging.info(
                "секция «%s»: колонки %s, строк всего %s, показываю %s",
                table_info["heading"], " | ".join(table_info["columns"]),
                table_info["total"], shown,
            )
            if shown < cfg["min_rankings_rows"]:
                raise TierListError(
                    "в таблицу рейтингов попало всего %s строк (нужно минимум %s). "
                    "Раскрыть список не удалось — проверьте, не переименовал ли сайт "
                    "кнопку «Show More»" % (shown, cfg["min_rankings_rows"])
                )

            page.wait_for_timeout(500)

            for name, selector, path in (
                ("тир-лист", sel, tier_path),
                ("рейтинги", rank_sel, rank_path),
            ):
                target = page.locator(selector).first.locator("xpath=ancestor::section[1]")
                # Сначала подводим кадр в вид: липкая реклама позиционируется
                # относительно окна, и проверять перекрытие имеет смысл только тут.
                target.scroll_into_view_if_needed()
                page.wait_for_timeout(250)
                _assert_not_covered(page, cfg, selector, name)
                target.screenshot(path=str(path))
                logging.info("скриншот «%s» снят: %s", name, path.name)

            return {
                "heading": heading,
                "tiers": counts["tiers"],
                "specs": counts["specs"],
                "rank_heading": table_info["heading"],
                "rank_rows": shown,
            }
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


def build_embeds(cfg, names):
    """Два эмбеда с одинаковым url — Discord склеивает такие в одну карточку
    с галереей картинок, поэтому обе уходят одним сообщением."""
    first = {
        "title": cfg["caption_title"],
        "url": cfg["url"],
        "description": "Источник: [%s](%s)" % (cfg["source_name"], cfg["url"]),
        "color": cfg["embed_color"],
        "image": {"url": "attachment://%s" % names[0]},
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    second = {
        "url": cfg["url"],
        "color": cfg["embed_color"],
        "image": {"url": "attachment://%s" % names[1]},
    }
    return [first, second]


def _multipart(cfg, images):
    """images — список пар (имя файла, байты) в порядке эмбедов."""
    payload = {
        "embeds": build_embeds(cfg, [name for name, _ in images]),
        "attachments": [
            {"id": i, "filename": name} for i, (name, _) in enumerate(images)
        ],
        "allowed_mentions": {"parse": []},
    }
    files = {
        "payload_json": (None, json.dumps(payload, ensure_ascii=False), "application/json"),
    }
    for i, (name, blob) in enumerate(images):
        files["files[%d]" % i] = (name, blob, "image/png")
    return files


def _check_fatal(resp, what):
    if resp.status_code in (401, 403):
        raise TierListError(
            "Discord ответил %s при %s — вебхук недействителен. Пересоздайте его "
            "в настройках канала и обновите секрет TIERLIST_WEBHOOK. Ответ: %s"
            % (resp.status_code, what, resp.text[:200])
        )


def post_new(webhook, cfg, images):
    """Создаёт новое сообщение и возвращает его id."""
    resp = requests.post(
        webhook + "?wait=true",
        files=_multipart(cfg, images),
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


def edit_existing(webhook, cfg, images, message_id):
    """Обновляет сообщение. Возвращает False, если его больше нет."""
    resp = requests.patch(
        "%s/messages/%s" % (webhook, message_id),
        files=_multipart(cfg, images),
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


def publish(webhook, cfg, images, state):
    message_id = state.get("message_id")
    if message_id:
        logging.info("обновляю существующее сообщение %s", message_id)
        if edit_existing(webhook, cfg, images, message_id):
            state["action"] = "updated"
            return state
    else:
        logging.info("сохранённого сообщения нет — создаю новое")

    new_id = post_new(webhook, cfg, images)
    logging.info("создано сообщение %s", new_id)
    state["message_id"] = new_id
    state["action"] = "created"
    return state


def process_target(cfg, state, dry_run):
    """Обрабатывает одну роль. Возвращает строку итога для лога."""
    role = cfg["role"]
    tier_path = ROOT / ("tierlist-%s.png" % role)
    rank_path = ROOT / ("rankings-%s.png" % role)

    webhook = (os.environ.get(cfg["webhook_env"]) or "").strip().rstrip("/")
    if not webhook and not dry_run:
        raise TierListError(
            "нет переменной окружения %s — отправлять некуда. Проверьте, что секрет "
            "задан в репозитории и проброшен в воркфлоу" % cfg["webhook_env"]
        )

    take_screenshots(cfg, tier_path, rank_path)
    for path in (tier_path, rank_path):
        validate_image(path, cfg)

    if dry_run:
        return "DRY-RUN, не отправлено"

    images = [
        (tier_path.name, tier_path.read_bytes()),
        (rank_path.name, rank_path.read_bytes()),
    ]
    role_state = dict(state["roles"].get(role) or {})
    role_state = publish(webhook, cfg, images, role_state)
    role_state["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    role_state["source_url"] = cfg["url"]
    state["roles"][role] = role_state
    # Сохраняем сразу: если следующая роль упадёт, эта не потеряется.
    save_state(state)
    return "сообщение %s (%s)" % (role_state["message_id"], role_state["action"])


def run():
    shared, targets = load_config()
    dry_run = os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes")
    state = load_state(targets[0]["role"])
    logging.info("ролей к обработке: %s", len(targets))

    results, failures = [], []
    for target in targets:
        cfg = resolve_target(shared, target)
        name = cfg["name"]
        logging.info("--- роль «%s» (%s) ---", name, cfg["role"])
        try:
            outcome = process_target(cfg, state, dry_run)
            results.append((name, outcome))
            logging.info("роль «%s»: %s", name, outcome)
        except Exception as exc:
            # Одна упавшая роль не должна отменять остальные, но и молча
            # проглотить её нельзя — соберём и подсветим в конце.
            failures.append((name, exc))
            logging.error("роль «%s» не обработана: %s", name, exc)

    logging.info("итог: успешно %s из %s", len(results), len(targets))
    for name, outcome in results:
        logging.info("  ok  %s — %s", name, outcome)
    if failures:
        for name, exc in failures:
            logging.error("  СБОЙ %s — %s", name, exc)
        logging.error("ролей с ошибкой: %s из %s", len(failures), len(targets))
        return 1
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
