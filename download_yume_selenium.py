"""
Скачивание изображений Yume 2kki Wiki через Selenium (реальный Chrome).
Cloudscraper на yume.wiki не проходит Cloudflare — браузер проходит.

Зависимости:
  pip install selenium webdriver-manager requests

Запуск:
  python download_yume_selenium.py
"""

import re
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

import requests
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

try:
    from webdriver_manager.chrome import ChromeDriverManager
    USE_MANAGER = True
except ImportError:
    USE_MANAGER = False

OUT_DIR = Path("yume_images")
DELAY = 0.5
FULL_SIZE = True
HEADLESS = False  # False = видно окно (лучше для Cloudflare), True = без окна
PAGE_LOAD_WAIT = 12  # секунд ждать прохождения CF на первой странице


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


def make_driver():
    opts = Options()
    if HEADLESS:
        opts.add_argument("--headless=new")
    opts.add_argument("--window-size=1280,900")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    opts.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    )

    if USE_MANAGER:
        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=opts)
    else:
        driver = webdriver.Chrome(options=opts)

    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {
            "source": """
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            """
        },
    )
    return driver


def cookies_to_requests(driver) -> requests.Session:
    """Переносит куки из Selenium в requests.Session."""
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": driver.execute_script("return navigator.userAgent;"),
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://yume.wiki/",
        }
    )
    for c in driver.get_cookies():
        session.cookies.set(
            c["name"],
            c["value"],
            domain=c.get("domain"),
            path=c.get("path", "/"),
        )
    return session


def warmup(driver) -> bool:
    print("[warmup] Открываю https://yume.wiki/ ...")
    driver.get("https://yume.wiki/")
    print(
        f"  Если видите Cloudflare «Checking your browser» — подождите {PAGE_LOAD_WAIT} сек.\n"
        "  Если просят капчу — решите вручную в окне браузера."
    )
    time.sleep(PAGE_LOAD_WAIT)

    # ждём, пока title не будет challenge
    try:
        WebDriverWait(driver, 30).until(
            lambda d: "just a moment" not in (d.title or "").lower()
            and "cloudflare" not in (d.title or "").lower()
        )
    except Exception:
        pass

    title = driver.title or ""
    print(f"  title={title!r}")
    if "just a moment" in title.lower() or "attention required" in title.lower():
        print("  ⚠ Cloudflare не пройден. Решите капчу вручную и нажмите Enter здесь.")
        input()
        time.sleep(2)

    # дополнительный заход на страницу локации
    driver.get("https://yume.wiki/2kki/Urotsuki%27s_Room")
    time.sleep(3)
    cookies = driver.get_cookies()
    print(f"  cookies: {[c['name'] for c in cookies]}")
    return len(cookies) > 0 or "yume" in (driver.title or "").lower()


def fetch_json(url: str) -> dict:
    # wrapper обычно без CF
    r = requests.get(url, timeout=60, headers={"User-Agent": "Mozilla/5.0"})
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
        time.sleep(0.2)
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
        time.sleep(0.2)
    print(f"[images] всего: {len(items)}")
    return items


def download_with_session(session: requests.Session, url: str, dest: Path) -> bool:
    if dest.exists() and dest.stat().st_size > 0:
        print(f"  skip: {dest.name}")
        return True

    url = to_full_size(url) if FULL_SIZE else url
    dest.parent.mkdir(parents=True, exist_ok=True)

    for attempt in range(1, 4):
        try:
            r = session.get(url, timeout=30)
            ctype = (r.headers.get("content-type") or "").lower()
            if r.status_code == 200 and r.content and (
                "image" in ctype or len(r.content) > 500
            ):
                dest.write_bytes(r.content)
                print(f"  OK ({len(r.content):,} B): {dest.name}")
                return True
            print(f"  HTTP {r.status_code} (try {attempt}): {url}")
        except Exception as e:
            print(f"  ERR (try {attempt}): {e}")
        time.sleep(DELAY * attempt)

    print(f"  FAIL: {url}")
    return False


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    driver = make_driver()

    try:
        print("=== Warmup (браузер) ===")
        warmup(driver)
        session = cookies_to_requests(driver)
        print(f"Session cookies: {list(session.cookies.keys())}")

        # быстрый тест одной картинки
        test_url = "https://yume.wiki/images/0/02/3DStructures.png"
        test_dest = OUT_DIR / "worlds" / "_test_3DStructures.png"
        print("\n=== Тест одной картинки ===")
        if not download_with_session(session, test_url, test_dest):
            print(
                "\n⚠ Куки из браузера не помогли requests.\n"
                "Будем качать картинки через сам Selenium (медленнее, но надёжнее)."
            )
            use_selenium_download = True
        else:
            use_selenium_download = False

        print("\n=== Превью миров ===")
        previews = collect_location_previews()
        ok = fail = 0
        for title, url in previews:
            ext = Path(urlparse(to_full_size(url)).path).suffix or ".png"
            dest = OUT_DIR / "worlds" / f"{safe_name(title)}{ext}"
            if use_selenium_download:
                success = download_via_selenium(driver, url, dest)
            else:
                success = download_with_session(session, url, dest)
            if success:
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
            if use_selenium_download:
                success = download_via_selenium(driver, url, dest)
            else:
                success = download_with_session(session, url, dest)
            if success:
                ok += 1
            else:
                fail += 1
            time.sleep(DELAY)
        print(f"Доп.: OK={ok}, FAIL={fail}")
        print(f"\nФайлы: {OUT_DIR.resolve()}")

    finally:
        driver.quit()


def download_via_selenium(driver, url: str, dest: Path) -> bool:
    """Скачивание через Chrome DevTools (Network.getResponseBody) — запасной путь."""
    if dest.exists() and dest.stat().st_size > 0:
        print(f"  skip: {dest.name}")
        return True

    url = to_full_size(url) if FULL_SIZE else url
    dest.parent.mkdir(parents=True, exist_ok=True)

    try:
        driver.get(url)
        time.sleep(1.5)
        # если открылась картинка — body будет пустым/минимальным, берём через fetch в контексте страницы
        data = driver.execute_async_script(
            """
            const url = arguments[0];
            const done = arguments[arguments.length - 1];
            fetch(url, { credentials: 'include' })
              .then(r => r.ok ? r.arrayBuffer() : Promise.reject(r.status))
              .then(buf => {
                const arr = Array.from(new Uint8Array(buf));
                done({ok: true, data: arr});
              })
              .catch(e => done({ok: false, err: String(e)}));
            """,
            url,
        )
        if data and data.get("ok") and data.get("data"):
            dest.write_bytes(bytes(data["data"]))
            print(f"  OK selenium ({len(data['data']):,} B): {dest.name}")
            return True
        print(f"  FAIL selenium: {data}")
    except Exception as e:
        print(f"  ERR selenium: {e}")
    return False


if __name__ == "__main__":
    main()
