"""
Скачивание изображений Yume 2kki Wiki через cloudscraper.
Сначала прогревает сессию на yume.wiki (куки Cloudflare), потом качает /images/.

  pip install cloudscraper
  python download_yume_images.py
"""

import re
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

import cloudscraper

OUT_DIR = Path("yume_images")
DELAY = 0.4
FULL_SIZE = True
MAX_RETRIES = 3

# "native" | "js2py" | "nodejs"  (nodejs часто лучше против Cloudflare)
INTERPRETER = "native"

scraper = cloudscraper.create_scraper(
    browser={"browser": "chrome", "platform": "windows", "mobile": False},
    interpreter=INTERPRETER,
)

BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def safe_name(name: str) -> str:
    name = unquote(name)
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    return name.strip(" .") or "unnamed"


def to_full_size(url: str) -> str:
    m = re.match(
        r"(https?://yume\.wiki/images)/thumb/(.+?)/\d+px-[^/]+$",
        url,
    )
    if m:
        return f"{m.group(1)}/{m.group(2)}"
    return url


def warmup() -> bool:
    """Проходим Cloudflare, сохраняем куки в scraper."""
    pages = [
        "https://yume.wiki/",
        "https://yume.wiki/2kki/Urotsuki%27s_Room",
        "https://yume.wiki/2kki/Yume_2kki_Wiki",
    ]
    for url in pages:
        print(f"[warmup] {url}")
        try:
            r = scraper.get(
                url,
                headers={
                    **BASE_HEADERS,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                },
                timeout=60,
            )
            print(f"  status={r.status_code}, cookies={list(scraper.cookies.keys())}")
            if r.status_code == 200:
                return True
        except Exception as e:
            print(f"  warmup error: {e}")
        time.sleep(1.5)
    return False


def download_one(url: str, dest: Path) -> bool:
    if dest.exists() and dest.stat().st_size > 0:
        print(f"  skip: {dest.name}")
        return True

    url = to_full_size(url) if FULL_SIZE else url
    dest.parent.mkdir(parents=True, exist_ok=True)

    header_variants = [
        {
            **BASE_HEADERS,
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            "Referer": "https://yume.wiki/",
        },
        {**BASE_HEADERS, "Accept": "image/*,*/*;q=0.8"},  # без Referer
        {
            **BASE_HEADERS,
            "Accept": "*/*",
            "Referer": "https://yume.wiki/2kki/Urotsuki%27s_Room",
        },
    ]

    for attempt in range(1, MAX_RETRIES + 1):
        headers = header_variants[(attempt - 1) % len(header_variants)]
        try:
            r = scraper.get(url, headers=headers, timeout=30)
            ctype = (r.headers.get("content-type") or "").lower()
            if r.status_code == 200 and r.content and (
                "image" in ctype or len(r.content) > 1000
            ):
                dest.write_bytes(r.content)
                print(f"  OK ({len(r.content):,} B): {dest.name}")
                return True
            print(f"  HTTP {r.status_code} ctype={ctype[:30]} (try {attempt})")
            if r.status_code in (403, 503):
                text_head = (r.text or "")[:400].lower()
                if "cloudflare" in text_head or "just a moment" in text_head:
                    warmup()
        except Exception as e:
            print(f"  ERR (try {attempt}): {e}")
        time.sleep(DELAY * attempt)

    print(f"  FAIL: {url}")
    return False


def fetch_json(url: str) -> dict:
    r = scraper.get(
        url, headers={**BASE_HEADERS, "Accept": "application/json"}, timeout=60
    )
    r.raise_for_status()
    return r.json()


def collect_location_previews():
    items = []
    continue_key = None
    page = 0
    while True:
        page += 1
        url = "https://wrapper.yume.wiki/locations?game=2kki"
        if continue_key:
            url += f"&continueKey={continue_key}"
        print(f"[locations] page {page}...")
        data = fetch_json(url)
        for loc in data.get("locations", []):
            img = loc.get("locationImage")
            title = loc.get("title") or "unknown"
            if img:
                items.append((title, img))
        continue_key = data.get("continueKey")
        if not continue_key:
            break
        time.sleep(DELAY)
    print(f"[locations] всего: {len(items)}")
    return items


def collect_extra_images():
    items = []
    continue_key = None
    page = 0
    while True:
        page += 1
        url = "https://wrapper.yume.wiki/images?game=2kki"
        if continue_key:
            url += f"&continueKey={continue_key}"
        print(f"[images] page {page}...")
        data = fetch_json(url)
        for loc in data.get("locationImages", []):
            title = loc.get("title") or "unknown"
            for img in loc.get("images", []):
                u = img.get("url")
                if u:
                    items.append((title, u))
        continue_key = data.get("continueKey")
        if not continue_key:
            break
        time.sleep(DELAY)
    print(f"[images] всего: {len(items)}")
    return items


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print("=== Warmup Cloudflare ===")
    ok_warm = warmup()
    if not ok_warm:
        print(
            "\n⚠ Не удалось пройти Cloudflare на yume.wiki.\n"
            "Попробуйте в скрипте: INTERPRETER = 'nodejs'  (нужен Node.js)\n"
            "или Selenium-вариант.\n"
        )

    print("\n=== Превью миров ===")
    previews = collect_location_previews()
    ok = fail = 0
    for title, url in previews:
        ext = Path(urlparse(to_full_size(url)).path).suffix or ".png"
        dest = OUT_DIR / "worlds" / f"{safe_name(title)}{ext}"
        if download_one(url, dest):
            ok += 1
        else:
            fail += 1
        time.sleep(DELAY)
    print(f"Превью: OK={ok}, FAIL={fail}\n")

    print("=== Доп. скриншоты ===")
    extras = collect_extra_images()
    ok = fail = 0
    seen = set()
    for title, url in extras:
        full = to_full_size(url) if FULL_SIZE else url
        name = Path(urlparse(full).path).name
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        dest = OUT_DIR / "world_extra" / f"{safe_name(title)}__{safe_name(name)}"
        if download_one(url, dest):
            ok += 1
        else:
            fail += 1
        time.sleep(DELAY)
    print(f"Доп.: OK={ok}, FAIL={fail}")
    print(f"\nФайлы: {OUT_DIR.resolve()}")


if __name__ == "__main__":
    main()
