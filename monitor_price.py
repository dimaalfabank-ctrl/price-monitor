# -*- coding: utf-8 -*-
"""
Мониторинг цен товаров на Wildberries + уведомления в Telegram при снижении цены.

ВЕРСИЯ 2: вместо внутреннего JSON API (которое WB нестабильно меняет/блокирует)
открывает реальную страницу товара headless-браузером (Playwright) и читает
цену прямо из отображаемого HTML — так же, как её видит обычный покупатель.
Это медленнее, но гораздо устойчивее к изменениям на стороне WB.

Как это работает:
  1. Список товаров — в items.json (nm_id + опционально label, notify_below).
  2. Для каждого товара открывается https://www.wildberries.ru/catalog/{nm_id}/detail.aspx
  3. Из страницы вытаскивается текущая цена (несколько запасных вариантов селекторов,
     т.к. WB меняет вёрстку).
  4. Сравнивается с last_prices.json. Если цена упала — уведомление в Telegram.
  5. last_prices.json коммитится обратно в репозиторий воркфлоу GitHub Actions.
"""

import os
import json
import sys
import re
import time
import requests
from playwright.sync_api import sync_playwright

ITEMS_FILE = os.environ.get("WB_ITEMS_FILE", "items.json")
STATE_FILE = os.environ.get("WB_STATE_FILE", "last_prices.json")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

PRODUCT_URL_TEMPLATE = "https://www.wildberries.ru/catalog/{nm_id}/detail.aspx"

# Несколько вариантов селекторов цены — WB периодически меняет классы вёрстки.
PRICE_SELECTORS = [
    ".price-block__final-price",
    ".price-block__wallet-price",
    "[class*='price-block__final-price']",
    "ins.price-block__final-price",
]


def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️  TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID не заданы — сообщение не отправлено:")
        print(text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(url, data={
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }, timeout=20)
    if not resp.ok:
        print(f"Ошибка отправки в Telegram: {resp.status_code} {resp.text}")


def _parse_price_text(text):
    """'1 234 ₽' -> 1234"""
    if not text:
        return None
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else None


def fetch_price_via_browser(page, nm_id):
    """Открывает страницу товара и вытаскивает цену + название. Возвращает dict или None."""
    url = PRODUCT_URL_TEMPLATE.format(nm_id=nm_id)
    try:
        page.goto(url, timeout=30000, wait_until="domcontentloaded")
        page.wait_for_timeout(2500)  # даём догрузиться динамическому контенту

        if page.locator("text=Товара нет на сайте").count() > 0 or \
           page.locator("text=не найден").count() > 0:
            return None

        price_text = None
        for selector in PRICE_SELECTORS:
            loc = page.locator(selector).first
            if loc.count() > 0:
                try:
                    price_text = loc.inner_text(timeout=3000)
                    if price_text:
                        break
                except Exception:
                    continue

        price = _parse_price_text(price_text)
        if price is None:
            return None

        name = ""
        try:
            name_loc = page.locator("h1").first
            if name_loc.count() > 0:
                name = name_loc.inner_text(timeout=3000).strip()
        except Exception:
            pass

        return {"price": price, "name": name}
    except Exception as e:
        print(f"⚠️  Ошибка при загрузке страницы {url}: {e}")
        return None


def main():
    items = load_json(ITEMS_FILE, [])
    if not items:
        print(f"Файл {ITEMS_FILE} пуст или не найден — нечего мониторить.")
        sys.exit(0)

    labels = {item["nm_id"]: item.get("label", str(item["nm_id"])) for item in items}
    target_prices = {item["nm_id"]: item.get("notify_below") for item in items}

    state = load_json(STATE_FILE, {})
    changed = False

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            locale="ru-RU",
        )
        page = context.new_page()

        for item in items:
            nm_id = item["nm_id"]
            nm_key = str(nm_id)
            label = labels[nm_id]

            info = fetch_price_via_browser(page, nm_id)
            time.sleep(2)  # вежливая пауза между товарами

            if not info:
                print(f"⚠️  {label} ({nm_id}): не удалось получить цену (товар снят с продажи или изменилась вёрстка страницы)")
                continue

            new_price = info["price"]
            old_price = state.get(nm_key, {}).get("price")
            product_name = info["name"] or label
            product_url = PRODUCT_URL_TEMPLATE.format(nm_id=nm_id)

            if old_price is not None and new_price < old_price:
                diff = old_price - new_price
                pct = diff / old_price * 100
                send_telegram(
                    f"📉 <b>Цена снизилась!</b>\n\n"
                    f"<b>{label}</b>\n{product_name}\n\n"
                    f"Было: {old_price} ₽\n"
                    f"Стало: <b>{new_price} ₽</b>\n"
                    f"Снижение: {diff} ₽ (-{pct:.1f}%)\n\n"
                    f"{product_url}"
                )
                print(f"📉 {label}: {old_price} → {new_price}")
            elif old_price is not None and new_price > old_price:
                print(f"📈 {label}: {old_price} → {new_price} (рост, без уведомления)")
            else:
                print(f"— {label}: {new_price} ₽ (без изменений)" if old_price else f"Первое сохранение: {label} = {new_price} ₽")

            threshold = target_prices.get(nm_id)
            if threshold and new_price <= threshold and (old_price is None or old_price > threshold):
                send_telegram(
                    f"🎯 <b>Достигнут целевой порог!</b>\n\n"
                    f"<b>{label}</b>\n{product_name}\n\n"
                    f"Цена: <b>{new_price} ₽</b> (порог: {threshold} ₽)\n\n"
                    f"{product_url}"
                )

            if new_price != old_price:
                state[nm_key] = {"price": new_price, "label": label}
                changed = True

        browser.close()

    if changed:
        save_json(STATE_FILE, state)
        print(f"Состояние обновлено: {STATE_FILE}")
    else:
        print("Изменений цен нет.")


if __name__ == "__main__":
    main()
