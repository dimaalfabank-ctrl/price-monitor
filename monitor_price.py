# -*- coding: utf-8 -*-
"""
Мониторинг цен товаров на Wildberries + уведомления в Telegram при снижении цены.

Как это работает:
  1. Список товаров для отслеживания лежит в items.json (артикул WB = nmId + метка).
  2. Скрипт запрашивает публичный "карточный" API WB (без ключа, без авторизации) —
     тот же, что использует сам сайт для отображения цены в каталоге.
  3. Сравнивает текущую цену с последней сохранённой в last_prices.json.
  4. Если цена упала — шлёт сообщение в Telegram.
  5. Обновляет last_prices.json (это состояние коммитится обратно в репозиторий
     воркфлоу GitHub Actions — см. price-monitor.yml).

Запуск вручную для проверки:
  WB_ITEMS_FILE=items.json TELEGRAM_BOT_TOKEN=xxx TELEGRAM_CHAT_ID=xxx python monitor_price.py
"""

import os
import json
import sys
import time
import requests

ITEMS_FILE = os.environ.get("WB_ITEMS_FILE", "items.json")
STATE_FILE = os.environ.get("WB_STATE_FILE", "last_prices.json")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# Публичный неавторизованный API карточек WB (тот же, что использует сайт).
# dest — код региона доставки, влияет на отображаемую цену/акции.
# -1257786 — примерно Москва; при желании можно заменить на свой регион.
WB_CARD_API = "https://card.wb.ru/cards/detail"
WB_DEST = os.environ.get("WB_DEST", "-1257786")


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


def fetch_prices(nm_ids):
    """Запрашивает цены пачкой (WB позволяет до ~100 nm через ';')."""
    result = {}
    chunk_size = 50
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "*/*",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": "https://www.wildberries.ru/",
    }
    for i in range(0, len(nm_ids), chunk_size):
        chunk = nm_ids[i:i + chunk_size]
        params = {
            "appType": 1,
            "curr": "rub",
            "dest": WB_DEST,
            "spp": 30,
            "nm": ";".join(str(x) for x in chunk),
        }
        resp = requests.get(WB_CARD_API, params=params, timeout=20, headers=headers)

        if resp.status_code != 200:
            print(f"⚠️  WB API вернул статус {resp.status_code} для nm={chunk}")
            print(f"    Тело ответа (первые 300 симв.): {resp.text[:300]}")
            time.sleep(1)
            continue

        data = resp.json()
        products = (data.get("data") or {}).get("products") or []
        if not products:
            print(f"⚠️  Пустой список products для nm={chunk}. Полный ответ (первые 300 симв.): {json.dumps(data)[:300]}")
        for p in products:
            nm_id = p.get("id")
            price = _extract_price(p)
            name = p.get("name", "")
            brand = p.get("brand", "")
            if price is not None:
                result[nm_id] = {"price": price, "name": name, "brand": brand}
        time.sleep(1)  # вежливая пауза между пачками
    return result


def _extract_price(product):
    """WB несколько раз меняла формат ответа — проверяем разные варианты."""
    # Старый формат: цена сразу в продукте (в копейках)
    for key in ("salePriceU", "priceU"):
        if key in product and product[key]:
            return round(product[key] / 100)

    # Новый формат: цена внутри sizes[].price
    sizes = product.get("sizes") or []
    for size in sizes:
        price_obj = size.get("price") or {}
        for key in ("product", "total", "basic"):
            if price_obj.get(key):
                return round(price_obj[key] / 100)

    return None


def main():
    items = load_json(ITEMS_FILE, [])
    if not items:
        print(f"Файл {ITEMS_FILE} пуст или не найден — нечего мониторить.")
        sys.exit(0)

    nm_ids = [item["nm_id"] for item in items]
    labels = {item["nm_id"]: item.get("label", str(item["nm_id"])) for item in items}
    target_prices = {item["nm_id"]: item.get("notify_below") for item in items}

    current = fetch_prices(nm_ids)
    state = load_json(STATE_FILE, {})

    changed = False
    for nm_id in nm_ids:
        nm_key = str(nm_id)
        label = labels[nm_id]
        info = current.get(nm_id)

        if not info:
            print(f"⚠️  {label} ({nm_id}): не удалось получить цену (товар снят с продажи?)")
            continue

        new_price = info["price"]
        old_price = state.get(nm_key, {}).get("price")
        product_name = info["name"] or label

        product_url = f"https://www.wildberries.ru/catalog/{nm_id}/detail.aspx"

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

        # отдельно: если задан порог notify_below и цена впервые опустилась ниже него
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

    if changed:
        save_json(STATE_FILE, state)
        print(f"Состояние обновлено: {STATE_FILE}")
    else:
        print("Изменений цен нет.")


if __name__ == "__main__":
    main()

