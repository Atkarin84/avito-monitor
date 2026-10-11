import email
from email.header import decode_header
import imaplib
import json
import os
import re
import sys
import urllib.parse
from pathlib import Path

import requests
from bs4 import BeautifulSoup

SEEN_FILE = Path(__file__).parent / "seen_items.json"

TELEGRAM_BOT_UA = "TelegramBot (like TwitterBot)"

AVITO_CATEGORY_URLS = [
    ("RX 6600", "https://www.avito.ru/saratov/tovary_dlya_kompyutera/komplektuyuschie/videokarty-ASgBAgICAkTGB~pm7gmmZw?q=RX+6600&s=104"),
    ("RX 6600 XT", "https://www.avito.ru/saratov/tovary_dlya_kompyutera/komplektuyuschie/videokarty-ASgBAgICAkTGB~pm7gmmZw?q=RX+6600+XT&s=104"),
    ("RX 7600", "https://www.avito.ru/saratov/tovary_dlya_kompyutera/komplektuyuschie/videokarty-ASgBAgICAkTGB~pm7gmmZw?q=RX+7600&s=104"),
]

IMAP_SERVERS = {
    "yandex.ru": "imap.yandex.ru",
    "ya.ru": "imap.yandex.ru",
    "yandex.com": "imap.yandex.ru",
    "mail.ru": "imap.mail.ru",
    "inbox.ru": "imap.mail.ru",
    "list.ru": "imap.mail.ru",
    "bk.ru": "imap.mail.ru",
    "internet.ru": "imap.mail.ru",
    "gmail.com": "imap.gmail.com",
    "googlemail.com": "imap.gmail.com",
    "rambler.ru": "imap.rambler.ru",
}

ITEM_URL_RE = re.compile(
    r"^https?://(?:www\.|m\.)?avito\.ru/saratov/tovary_dlya_kompyutera/([a-z0-9_-]+_\d{7,})$",
    re.IGNORECASE,
)

TARGET_GPU_RE = re.compile(r"(?:rx[\s_-]*)?(?:6600|7600)(?:[\s_-]*xt)?", re.IGNORECASE)

CLOSED_PHRASES = (
    "снято с публикации",
    "объявление закрыто",
    "товар продан",
    "снято с продажи",
    "объявление отклонено",
    "объявление заблокировано",
)


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
    parsed_outer = urllib.parse.urlsplit(url)
    if parsed_outer.query:
        qs = urllib.parse.parse_qs(parsed_outer.query)
        for key in ("u", "url", "to", "target", "redirect"):
            if key in qs and "avito.ru" in qs[key][0]:
                url = urllib.parse.unquote(qs[key][0])
                break

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
    m = re.search(r"(\d{1,3}(?:[\s\u00a0]\d{3})+|\d{4,7})\s*(?:₽|руб|р\.)", text, re.IGNORECASE)
    if m:
        val = re.sub(r"\s+", " ", m.group(1).strip())
        return f"{val} ₽"
    return None


def is_listing_active(url: str) -> bool:
    """
    Check if an Avito listing is currently active and NOT closed/removed from sale.
    Closed listings on Avito return HTTP 301 redirect to category (?isFromRedirect=1)
    or contain closed phrases in HTML.
    """
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": TELEGRAM_BOT_UA, "Accept-Language": "ru-RU,ru;q=0.9"},
            allow_redirects=False,
            timeout=15,
        )
        # Closed/removed listings redirect (301/302) to ?isFromRedirect=1 or return 404/410
        if resp.status_code in (301, 302, 303, 307, 308, 404, 410):
            loc = resp.headers.get("location", "")
            print(f"[Filter] Skipping closed/redirected listing ({resp.status_code} -> {loc}): {url}")
            return False

        if resp.status_code == 200:
            lower_html = resp.text.lower()
            for phrase in CLOSED_PHRASES:
                if phrase in lower_html:
                    print(f"[Filter] Skipping closed listing ('{phrase}'): {url}")
                    return False
            return True

        # If Avito rate-limited the check from cloud IP, fall back to item ID freshness check
        m = re.search(r"_(\d{8,})$", url)
        if m and int(m.group(1)) < 8245000000:
            print(f"[Filter] Skipping old archived item ID ({m.group(1)}): {url}")
            return False
        return True
    except Exception as exc:
        print(f"[Filter] Error verifying {url}: {exc}")
        return False


def fetch_live_avito_saratov() -> dict[str, dict]:
    """Fetch live Avito Saratov category pages using TelegramBot SSR User-Agent."""
    results: dict[str, dict] = {}
    for model_name, cat_url in AVITO_CATEGORY_URLS:
        try:
            resp = requests.get(
                cat_url,
                headers={"User-Agent": TELEGRAM_BOT_UA, "Accept-Language": "ru-RU,ru;q=0.9"},
                timeout=20,
            )
            if resp.status_code != 200 or "Доступ ограничен" in resp.text:
                print(f"[Live Avito] Status {resp.status_code} for {model_name}")
                continue

            soup = BeautifulSoup(resp.text, "html.parser")
            for a_el in soup.select('a[data-marker="item-title"]'):
                href = a_el.get("href", "")
                if not href.startswith("/saratov/tovary_dlya_kompyutera/"):
                    # Ignore ads from other cities (Avito Delivery block)
                    continue
                clean_url = normalize_item_url("https://www.avito.ru" + href)
                if not clean_url or clean_url in results:
                    continue

                raw_title = a_el.get_text(" ", strip=True)
                gpu_label = detect_gpu_label(raw_title, clean_url)
                if not gpu_label:
                    continue

                card = a_el.find_parent(attrs={"data-marker": "item"})
                price_str = None
                if card:
                    price_meta = card.select_one('[itemprop="price"]')
                    if price_meta and price_meta.get("content"):
                        price_str = f"{price_meta['content']} ₽"

                slug = clean_url.rsplit("/", 1)[-1]
                results[clean_url] = {
                    "model": gpu_label,
                    "title": clean_title(raw_title, slug),
                    "price": price_str,
                    "url": clean_url,
                }
        except Exception as exc:
            print(f"[Live Avito] Error for {model_name}: {exc}")

    print(f"[Live Avito] Found {len(results)} active Saratov listings.")
    return results


def decode_mime_words(s: str) -> str:
    if not s:
        return ""
    parts = decode_header(s)
    decoded = []
    for content, enc in parts:
        if isinstance(content, bytes):
            decoded.append(content.decode(enc or "utf-8", errors="replace"))
        else:
            decoded.append(content)
    return "".join(decoded)


def fetch_from_avito_emails(email_user: str, email_pass: str, imap_host: str | None = None) -> tuple[dict[str, dict], str]:
    """Connect to user's mailbox via IMAP and parse Avito 'Saved Search' notification emails."""
    results: dict[str, dict] = {}
    if not email_user or not email_pass:
        return results, "⚠️ не настроена"

    if not imap_host:
        domain = email_user.split("@")[-1].lower().strip()
        imap_host = IMAP_SERVERS.get(domain, f"imap.{domain}")

    print(f"[IMAP] Connecting to {imap_host} as {email_user}...")
    try:
        mail = imaplib.IMAP4_SSL(imap_host, 993)
        mail.login(email_user, email_pass)
        mail.select("INBOX", readonly=True)

        status, data = mail.search(None, '(FROM "avito.ru")')
        if status != "OK" or not data or not data[0]:
            status, data = mail.search(None, "ALL")

        msg_ids = data[0].split()[-20:] if (data and data[0]) else []
        avito_emails_count = 0

        for num in reversed(msg_ids):
            status, msg_data = mail.fetch(num, "(RFC822)")
            if status != "OK" or not msg_data:
                continue
            raw_email = msg_data[0][1]
            if not isinstance(raw_email, bytes):
                continue

            msg = email.message_from_bytes(raw_email)
            from_hdr = decode_mime_words(msg.get("From", "")).lower()
            if "avito" not in from_hdr:
                continue
            avito_emails_count += 1

            html_parts = []
            for part in msg.walk():
                ctype = part.get_content_type()
                if ctype in ("text/html", "text/plain"):
                    payload = part.get_payload(decode=True)
                    if payload:
                        charset = part.get_content_charset() or "utf-8"
                        html_parts.append(payload.decode(charset, errors="replace"))

            full_body = "\n".join(html_parts)
            soup = BeautifulSoup(full_body, "html.parser")

            for a_el in soup.find_all("a", href=True):
                href = a_el["href"]
                clean_url = normalize_item_url(href)
                if not clean_url:
                    continue

                link_text = a_el.get_text(" ", strip=True)
                parent_text = a_el.parent.get_text(" ", strip=True) if a_el.parent else link_text
                gpu_label = detect_gpu_label(f"{link_text} {parent_text}", clean_url)
                if not gpu_label:
                    continue

                slug = clean_url.rsplit("/", 1)[-1]
                existing = results.get(clean_url)
                title = clean_title(link_text, slug)
                price = extract_price(parent_text)

                if not existing or (len(title) > len(existing["title"])):
                    results[clean_url] = {
                        "model": gpu_label,
                        "title": title,
                        "price": price or (existing.get("price") if existing else None),
                        "url": clean_url,
                    }

        mail.logout()
        return results, f"✅ подключена ({email_user}, писем Авито: {avito_emails_count})"
    except Exception as exc:
        print(f"[IMAP] Error checking email: {exc}")
        return results, f"❌ ошибка подключения к {imap_host} ({exc})"


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
    email_user = os.environ.get("EMAIL_USER", "").strip()
    email_pass = os.environ.get("EMAIL_PASSWORD", "").strip()
    imap_host = os.environ.get("IMAP_HOST", "").strip() or None
    force_notify = os.environ.get("FORCE_NOTIFY", "false").lower() == "true"

    if not token or not chat_id:
        print("ERROR: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID environment variables are required.")
        sys.exit(1)

    seen_urls = load_seen()
    all_found: dict[str, dict] = {}

    # 1. Check live Avito Saratov pages via TelegramBot SSR User-Agent
    all_found.update(fetch_live_avito_saratov())

    # 2. Check Avito Saved Search emails via IMAP
    imap_items, imap_status = fetch_from_avito_emails(email_user, email_pass, imap_host)
    for url, item in imap_items.items():
        if url not in all_found:
            all_found[url] = item

    # 3. Filter for unseen AND verified active (not closed/removed) listings
    new_items = []
    for url, item in all_found.items():
        if url in seen_urls:
            continue
        # Always mark as seen so we never re-check it
        seen_urls.add(url)
        if is_listing_active(url):
            new_items.append(item)

    print(f"Total candidates: {len(all_found)}, Active new listings: {len(new_items)}")

    if new_items:
        lines = [f"🔥 <b>Новые активные объявления в Саратове ({len(new_items)} шт.):</b>\n"]
        for item in new_items[:15]:
            price_part = f" — <b>{item['price']}</b>" if item.get("price") else ""
            lines.append(
                f"🔹 <b>{item['model']}</b>{price_part}\n"
                f"📄 {item['title']}\n"
                f"👉 <a href=\"{item['url']}\">{item['url']}</a>\n"
            )
        send_telegram_message(token, chat_id, "\n".join(lines))
        save_seen(seen_urls)
        print(f"Sent Telegram notification with {len(new_items)} active listing URLs.")
    else:
        save_seen(seen_urls)
        if force_notify:
            msg = (
                f"✅ <b>Фильтр снятых объявлений включён!</b>\n"
                f"📧 Почта IMAP: <b>{imap_status}</b>\n"
                f"📦 В базе отслеживания: <b>{len(seen_urls)}</b> объявлений.\n"
                f"Старые и снятые с публикации объявления из поисковиков полностью отключены."
            )
            send_telegram_message(token, chat_id, msg)
        else:
            print("No new active listings since last check. Silent mode.")


if __name__ == "__main__":
    main()
