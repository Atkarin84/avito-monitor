import json
import os
import re
import sys
import urllib.parse
from pathlib import Path

import requests
from bs4 import BeautifulSoup

try:
    from curl_cffi import requests as cffi_requests
except ImportError:
    cffi_requests = None

SEEN_FILE = Path(__file__).parent / "seen_items.json"

MODELS = [
    ("RX 6600", "https://www.avito.ru/saratov/tovary_dlya_kompyutera/komplektuyuschie/videokarty-ASgBAgICAkTGB~pm7gmmZw?q=RX+6600&s=104"),
    ("RX 6600 XT", "https://www.avito.ru/saratov/tovary_dlya_kompyutera/komplektuyuschie/videokarty-ASgBAgICAkTGB~pm7gmmZw?q=RX+6600+XT&s=104"),
    ("RX 7600", "https://www.avito.ru/saratov/tovary_dlya_kompyutera/komplektuyuschie/videokarty-ASgBAgICAkTGB~pm7gmmZw?q=RX+7600&s=104"),
]


def load_seen() -> set[str]:
    if not SEEN_FILE.exists():
        return set()
    try:
        data = json.loads(SEEN_FILE.read_text(encoding="utf-8"))
        return set(data.get("seen_urls", []))
    except Exception:
        return set()


def save_seen(seen: set[str]) -> None:
    SEEN_FILE.write_text(
        json.dumps({"seen_urls": sorted(seen)}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def normalize_avito_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    clean_path = parsed.path.rstrip("/")
    return f"https://www.avito.ru{clean_path}"


def fetch_direct_avito(model_name: str, avito_url: str, proxy_url: str | None = None) -> list[dict]:
    """Attempt direct scraping of Avito category page using Chrome TLS impersonation."""
    if cffi_requests is None:
        return []

    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
    try:
        resp = cffi_requests.get(
            avito_url,
            impersonate="chrome120",
            proxies=proxies,
            timeout=20,
            headers={
                "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8",
            },
        )
        if resp.status_code != 200 or "Доступ ограничен" in resp.text:
            print(f"[Direct] Avito returned status {resp.status_code} or IP block for {model_name}")
            return []

        soup = BeautifulSoup(resp.text, "html.parser")
        items = []
        for card in soup.select('[data-marker="item"]'):
            title_el = card.select_one('[itemprop="name"]') or card.select_one('a[data-marker="item-title"]')
            link_el = card.select_one('a[data-marker="item-title"]')
            price_el = card.select_one('[itemprop="price"]')
            if not link_el or not link_el.get("href"):
                continue

            href = link_el["href"]
            if not href.startswith("http"):
                href = "https://www.avito.ru" + href
            clean_url = normalize_avito_url(href)

            title = title_el.get_text(strip=True) if title_el else model_name
            price = price_el.get("content") if price_el and price_el.get("content") else None
            price_str = f"{price} ₽" if price else "Цена по ссылке"

            items.append({
                "model": model_name,
                "title": title,
                "price": price_str,
                "url": clean_url,
            })
        return items
    except Exception as exc:
        print(f"[Direct] Error fetching {model_name}: {exc}")
        return []


def fetch_via_search_fallback(model_name: str) -> list[dict]:
    """
    Fallback for foreign cloud IPs (like GitHub Actions):
    Queries DuckDuckGo HTML search for Saratov video card listings on Avito.
    """
    query = f'site:avito.ru/saratov/tovary_dlya_kompyutera/komplektuyuschie/videokarty "{model_name}"'
    ddg_url = "https://html.duckduckgo.com/html/"
    items = []
    try:
        resp = requests.post(
            ddg_url,
            data={"q": query, "kl": "ru-ru"},
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                "Accept-Language": "ru-RU,ru;q=0.9",
            },
            timeout=20,
        )
        if resp.status_code != 200:
            return []

        soup = BeautifulSoup(resp.text, "html.parser")
        for result in soup.select(".result"):
            a_el = result.select_one("a.result__a")
            snippet_el = result.select_one(".result__snippet")
            if not a_el:
                continue
            href = a_el.get("href", "")
            if "uddg=" in href:
                qs = urllib.parse.parse_qs(urllib.parse.urlsplit(href).query)
                href = qs.get("uddg", [href])[0]

            if "avito.ru/saratov/" not in href:
                continue
            # Filter out general category pages without a specific item ID at the end
            if not re.search(r"_\d{8,}$", urllib.parse.urlsplit(href).path):
                continue

            clean_url = normalize_avito_url(href)
            title = a_el.get_text(strip=True)
            snippet = snippet_el.get_text(" ", strip=True) if snippet_el else ""
            price_match = re.search(r"(\d[\d\s]{2,8})\s*(?:₽|руб)", title + " " + snippet)
            price_str = f"{price_match.group(1).strip()} ₽" if price_match else "См. по ссылке"

            items.append({
                "model": model_name,
                "title": title,
                "price": price_str,
                "url": clean_url,
            })
    except Exception as exc:
        print(f"[Fallback] Error searching {model_name}: {exc}")
    return items


def send_telegram_message(token: str, chat_id: str, text: str) -> None:
    api_url = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = requests.post(
        api_url,
        json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        },
        timeout=15,
    )
    resp.raise_for_status()


def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    proxy_url = os.environ.get("PROXY_URL", "").strip() or None
    force_notify = os.environ.get("FORCE_NOTIFY", "false").lower() == "true"

    if not token or not chat_id:
        print("ERROR: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID environment variables are required.")
        sys.exit(1)

    seen_urls = load_seen()
    is_first_run = len(seen_urls) == 0

    new_items = []
    for model_name, avito_url in MODELS:
        found = fetch_direct_avito(model_name, avito_url, proxy_url=proxy_url)
        if not found:
            found = fetch_via_search_fallback(model_name)

        for item in found:
            url = item["url"]
            if url not in seen_urls:
                seen_urls.add(url)
                new_items.append(item)

    if new_items:
        lines = ["🔔 <b>Новые объявления в Саратове (Авито):</b>\n"]
        for item in new_items[:15]:
            lines.append(
                f"• <b>{item['model']}</b>: {item['title']}\n"
                f"  💰 {item['price']}\n"
                f"  🔗 <a href=\"{item['url']}\">Открыть объявление</a>\n"
            )
        lines.append(
            "\n📌 <b>Быстрые ссылки (свежие по дате):</b>\n"
            "• <a href=\"https://www.avito.ru/saratov/tovary_dlya_kompyutera/komplektuyuschie/videokarty-ASgBAgICAkTGB~pm7gmmZw?q=RX+6600&s=104\">RX 6600</a> | "
            "<a href=\"https://www.avito.ru/saratov/tovary_dlya_kompyutera/komplektuyuschie/videokarty-ASgBAgICAkTGB~pm7gmmZw?q=RX+6600+XT&s=104\">RX 6600 XT</a> | "
            "<a href=\"https://www.avito.ru/saratov/tovary_dlya_kompyutera/komplektuyuschie/videokarty-ASgBAgICAkTGB~pm7gmmZw?q=RX+7600&s=104\">RX 7600</a>"
        )
        send_telegram_message(token, chat_id, "\n".join(lines))
        save_seen(seen_urls)
        print(f"Sent notification with {len(new_items)} new items.")
    elif is_first_run or force_notify:
        msg = (
            "✅ <b>Мониторинг Авито (Саратов) успешно запущен в облаке 24/7!</b>\n\n"
            "Я проверяю новые объявления по видеокартам <b>RX 6600 / RX 6600 XT / RX 7600</b> каждые 4 часа.\n\n"
            "📌 <b>Прямые ссылки на свежие объявления в Саратове:</b>\n"
            "• <a href=\"https://www.avito.ru/saratov/tovary_dlya_kompyutera/komplektuyuschie/videokarty-ASgBAgICAkTGB~pm7gmmZw?q=RX+6600&s=104\">AMD Radeon RX 6600</a>\n"
            "• <a href=\"https://www.avito.ru/saratov/tovary_dlya_kompyutera/komplektuyuschie/videokarty-ASgBAgICAkTGB~pm7gmmZw?q=RX+6600+XT&s=104\">AMD Radeon RX 6600 XT</a>\n"
            "• <a href=\"https://www.avito.ru/saratov/tovary_dlya_kompyutera/komplektuyuschie/videokarty-ASgBAgICAkTGB~pm7gmmZw?q=RX+7600&s=104\">AMD Radeon RX 7600</a>"
        )
        send_telegram_message(token, chat_id, msg)
        print("Sent initial confirmation message.")
    else:
        print("No new items found since last check.")


if __name__ == "__main__":
    main()
