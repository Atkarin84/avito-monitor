import json
import os
import re
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
from bs4 import BeautifulSoup

try:
    from curl_cffi import requests as cffi_requests
except ImportError:
    cffi_requests = None

SEEN_FILE = Path(__file__).parent / "seen_items.json"

AVITO_CATEGORY_URLS = [
    ("RX 6600", "https://www.avito.ru/saratov/tovary_dlya_kompyutera/komplektuyuschie/videokarty-ASgBAgICAkTGB~pm7gmmZw?q=RX+6600&s=104"),
    ("RX 6600 XT", "https://www.avito.ru/saratov/tovary_dlya_kompyutera/komplektuyuschie/videokarty-ASgBAgICAkTGB~pm7gmmZw?q=RX+6600+XT&s=104"),
    ("RX 7600", "https://www.avito.ru/saratov/tovary_dlya_kompyutera/komplektuyuschie/videokarty-ASgBAgICAkTGB~pm7gmmZw?q=RX+7600&s=104"),
]

SEARCH_QUERIES = [
    'site:avito.ru/saratov/tovary_dlya_kompyutera RX 6600',
    'site:avito.ru/saratov/tovary_dlya_kompyutera RX 7600',
]

ITEM_URL_RE = re.compile(
    r"^https?://(?:www\.|m\.)?avito\.ru/saratov/tovary_dlya_kompyutera/([a-z0-9_-]+_\d{7,})$",
    re.IGNORECASE,
)

TARGET_GPU_RE = re.compile(r"(?:rx[\s_-]*)?(?:6600|7600)(?:[\s_-]*xt)?", re.IGNORECASE)


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


def normalize_item_url(url: str) -> str | None:
    url = urllib.parse.unquote(url).strip()
    if "uddg=" in url:
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        url = qs.get("uddg", [url])[0]
    parsed = urllib.parse.urlsplit(url)
    clean = f"https://www.avito.ru{parsed.path.rstrip('/')}"
    if ITEM_URL_RE.match(clean):
        return clean
    return None


def detect_gpu_label(title: str, url: str) -> str | None:
    combined = f"{title} {url}".lower()
    if not TARGET_GPU_RE.search(combined):
        return None
    if re.search(r"6600[\s_-]*xt", combined):
        return "AMD Radeon RX 6600 XT"
    if "6600" in combined:
        return "AMD Radeon RX 6600"
    if re.search(r"7600[\s_-]*xt", combined):
        return "AMD Radeon RX 7600 XT"
    if "7600" in combined:
        return "AMD Radeon RX 7600"
    return None


def clean_title(raw_title: str, slug: str) -> str:
    title = re.sub(r"\s*[-—|]\s*купить.*$", "", raw_title, flags=re.IGNORECASE).strip()
    title = re.sub(r"\s*купить в Саратове.*$", "", title, flags=re.IGNORECASE).strip()
    if not title or len(title) < 4:
        slug_words = re.sub(r"_\d{7,}$", "", slug).replace("_", " ").strip()
        title = slug_words.capitalize()
    return title


def extract_price(text: str) -> str | None:
    m = re.search(r"(?:цена|за|—|-|:)\s*(\d[\d\s\u00a0]{2,8})\s*(?:₽|руб|р\.)", text, re.IGNORECASE)
    if not m:
        m = re.search(r"(\d{1,3}(?:[\s\u00a0]\d{3})+)\s*(?:₽|руб|р\.)", text, re.IGNORECASE)
    if m:
        val = re.sub(r"\s+", " ", m.group(1).strip())
        return f"{val} ₽"
    return None


def get_free_ru_proxies() -> list[str]:
    """Fetch free RU proxies from public GitHub lists to bypass geo-blocking on GitHub Actions."""
    urls = [
        "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/countries/RU/data.json",
    ]
    candidates = []
    for u in urls:
        try:
            r = requests.get(u, timeout=10)
            if r.status_code == 200:
                for entry in r.json():
                    p = entry.get("proxy")
                    if p:
                        candidates.append(p)
        except Exception:
            pass
    return candidates[:25]


def parse_avito_html(html: str, default_model: str) -> dict[str, dict]:
    results: dict[str, dict] = {}
    soup = BeautifulSoup(html, "html.parser")
    for card in soup.select('[data-marker="item"]'):
        link_el = card.select_one('a[data-marker="item-title"]')
        if not link_el or not link_el.get("href"):
            continue
        href = link_el["href"]
        if not href.startswith("http"):
            href = "https://www.avito.ru" + href
        clean_url = normalize_item_url(href)
        if not clean_url:
            continue

        title_el = card.select_one('[itemprop="name"]') or link_el
        raw_title = title_el.get_text(strip=True) if title_el else default_model
        gpu_label = detect_gpu_label(raw_title, clean_url)
        if not gpu_label:
            continue

        price_el = card.select_one('[itemprop="price"]')
        price_val = price_el.get("content") if price_el and price_el.get("content") else None
        price_str = f"{price_val} ₽" if price_val else None

        slug = clean_url.rsplit("/", 1)[-1]
        results[clean_url] = {
            "model": gpu_label,
            "title": clean_title(raw_title, slug),
            "price": price_str,
            "url": clean_url,
        }
    return results


def fetch_direct_avito(proxy_url: str | None = None) -> dict[str, dict]:
    results: dict[str, dict] = {}
    if cffi_requests is None:
        return results

    proxies_to_try = [proxy_url] if proxy_url else [None] + get_free_ru_proxies()

    for model_name, avito_url in AVITO_CATEGORY_URLS:
        for px in proxies_to_try[:10]:
            px_dict = {"http": px, "https": px} if px else None
            try:
                resp = cffi_requests.get(
                    avito_url,
                    impersonate="chrome120",
                    proxies=px_dict,
                    timeout=12,
                    headers={"Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8"},
                )
                if resp.status_code == 200 and "Доступ ограничен" not in resp.text:
                    parsed = parse_avito_html(resp.text, model_name)
                    if parsed:
                        results.update(parsed)
                        break
            except Exception:
                continue
    return results


def fetch_via_ddg_cffi() -> dict[str, dict]:
    results: dict[str, dict] = {}
    req_mod = cffi_requests if cffi_requests is not None else requests
    for query in SEARCH_QUERIES:
        try:
            kwargs = {
                "data": {"q": query},
                "headers": {"Accept-Language": "ru-RU,ru;q=0.9"},
                "timeout": 15,
            }
            if cffi_requests is not None:
                kwargs["impersonate"] = "chrome120"
            resp = req_mod.post("https://lite.duckduckgo.com/lite/", **kwargs)
            text = urllib.parse.unquote(resp.text)
            for raw_match in re.findall(
                r"https?://(?:www\.)?avito\.ru/saratov/tovary_dlya_kompyutera/[a-zA-Z0-9_-]+_\d{7,}",
                text,
            ):
                clean_url = normalize_item_url(raw_match)
                if not clean_url or clean_url in results:
                    continue
                slug = clean_url.rsplit("/", 1)[-1]
                gpu_label = detect_gpu_label("", clean_url)
                if gpu_label:
                    results[clean_url] = {
                        "model": gpu_label,
                        "title": clean_title("", slug),
                        "price": None,
                        "url": clean_url,
                    }
            time.sleep(2)
        except Exception as exc:
            print(f"[DDG] {exc}")
    return results


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
    all_found: dict[str, dict] = {}

    all_found.update(fetch_direct_avito(proxy_url=proxy_url))
    for url, item in fetch_via_ddg_cffi().items():
        if url not in all_found:
            all_found[url] = item

    new_items = []
    for url, item in all_found.items():
        if url not in seen_urls:
            seen_urls.add(url)
            new_items.append(item)

    print(f"Total matching listings found: {len(all_found)}, New listings: {len(new_items)}")

    if new_items:
        lines = [f"🔥 <b>Новые объявления в Саратове ({len(new_items)} шт.):</b>\n"]
        for item in new_items[:15]:
            price_part = f" — <b>{item['price']}</b>" if item.get("price") else ""
            lines.append(
                f"🔹 <b>{item['model']}</b>{price_part}\n"
                f"📄 {item['title']}\n"
                f"👉 <a href=\"{item['url']}\">{item['url']}</a>\n"
            )
        send_telegram_message(token, chat_id, "\n".join(lines))
        save_seen(seen_urls)
        print(f"Sent Telegram notification with {len(new_items)} individual listing URLs.")
    elif force_notify:
        # Only on manual "Run workflow" button click, confirm that check completed and list tracked count
        msg = (
            f"ℹ️ <b>Ручная проверка завершена.</b>\n"
            f"Новых объявлений с момента прошлой проверки пока не появилось "
            f"(уже в базе отслеживания: <b>{len(seen_urls)}</b> объявлений).\n"
            f"Как только выйдет новое — пришлю прямую ссылку!"
        )
        send_telegram_message(token, chat_id, msg)
    else:
        print("No new listings since last check. Silent mode.")


if __name__ == "__main__":
    main()
