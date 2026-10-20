#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Парсер Menu Themes для Yume 2kki Explorer.

Источник:  https://yume.wiki/2kki/Menu_Themes
Что берёт: иконку темы, номер темы, локацию (карту), способ получения, скрин локации.
Что делает:
  1. скачивает страницу (Selenium / undetected-chromedriver — обход Cloudflare,
     как в download_yume_uc.py) или читает сохранённый HTML (--html);
  2. парсит таблицы (учитывает rowspan, несколько тем в одной ячейке,
     вторая таблица = «удалённые способы»);
  3. скачивает иконки в public/images/menu_themes (то, что ждёт app.js);
  4. пишет menu_themes.json в корень проекта — app.js подхватывает его сам;
  5. по желанию (--mysql) синхронизирует таблицы menu_themes / menu_theme_locations
     в MySQL (конфиг берётся из config/db.config.js или DATABASE_* из окружения).

Установка:
    pip install beautifulsoup4 lxml requests undetected-chromedriver selenium pymysql

Примеры:
    python menu_themes_parser.py                    # браузер -> JSON + иконки
    python menu_themes_parser.py --mysql            # + запись в MySQL
    python menu_themes_parser.py --html page.html   # без браузера (страница сохранена вручную)
    python menu_themes_parser.py --no-download      # только JSON
    python menu_themes_parser.py --update-fallback  # ещё и в data-fallback.json
"""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import unquote

try:
    from bs4 import BeautifulSoup, NavigableString, Tag
except ImportError:
    sys.exit("Нужен beautifulsoup4:  pip install beautifulsoup4 lxml")

WIKI = "https://yume.wiki"
PAGE_URL = f"{WIKI}/2kki/Menu_Themes"

ROOT = Path(__file__).resolve().parent
IMAGES_DIR = ROOT / "public" / "images" / "menu_themes"
OUT_JSON = ROOT / "menu_themes.json"
FALLBACK_JSON = ROOT / "data-fallback.json"
DB_CONFIG_JS = ROOT / "config" / "db.config.js"
DEBUG_HTML = ROOT / "menu_themes_page.html"

THEME_RE = re.compile(r"Theme[\s_#№]*(-?\d+)", re.I)
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")


# --------------------------------------------------------------------------- #
#  Получение HTML
# --------------------------------------------------------------------------- #
def _cf_passed(driver) -> bool:
    title = (driver.title or "").lower()
    src = (driver.page_source or "")[:1500].lower()
    return not any(x in title for x in ("just a moment", "checking your browser", "выполнение проверки")) \
        and "cf-browser-verification" not in src and "challenge-platform" not in src


def fetch_html_with_browser(headless: bool = False):
    """Возвращает (html, requests.Session с cookies браузера)."""
    import requests
    try:
        import undetected_chromedriver as uc
        opts = uc.ChromeOptions()
        opts.add_argument("--window-size=1280,900")
        if headless:
            opts.add_argument("--headless=new")
        driver = uc.Chrome(options=opts, use_subprocess=True)
    except ImportError:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        opts = Options()
        opts.add_argument("--window-size=1280,900")
        opts.add_argument("--disable-blink-features=AutomationControlled")
        opts.add_argument(f"user-agent={UA}")
        if headless:
            opts.add_argument("--headless=new")
        driver = webdriver.Chrome(options=opts)

    try:
        print(f"[browser] открываю {PAGE_URL}")
        driver.get(PAGE_URL)

        def table_ready() -> bool:
            try:
                return bool(driver.execute_script(
                    "return document.querySelectorAll('#mw-content-text table td, table.wikitable td').length > 5;"))
            except Exception:
                return False

        # Ждём, пока Cloudflare отпустит и таблица реально появится (до 3 попыток, по 90 с).
        for attempt in range(1, 4):
            end = time.time() + 90
            while time.time() < end and not table_ready():
                time.sleep(1)
            if table_ready():
                break
            input(f"[browser] Таблица не появилась (попытка {attempt}/3). Пройдите проверку Cloudflare "
                  f"в окне браузера, дождитесь страницы с таблицей и нажмите Enter...")
        time.sleep(2)
        html = driver.page_source
        DEBUG_HTML.write_text(html, encoding="utf-8")  # на случай, если структура изменилась

        session = requests.Session()
        try:
            ua = driver.execute_script("return navigator.userAgent;")
        except Exception:
            ua = UA
        session.headers.update({"User-Agent": ua, "Referer": WIKI + "/",
                                "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"})
        for c in driver.get_cookies():
            session.cookies.set(c["name"], c["value"], domain=c.get("domain") or "yume.wiki",
                                path=c.get("path", "/"))
        return html, session
    finally:
        try:
            driver.quit()
        except Exception:
            pass


def make_plain_session():
    import requests
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Referer": WIKI + "/"})
    return s


# --------------------------------------------------------------------------- #
#  Парсинг
# --------------------------------------------------------------------------- #
def sanitize_world_name(name: str) -> str:
    """Как sanitizeWorldName в app.js (+ unquote)."""
    name = unquote(name)
    name = name.replace("%26", "&").replace("%27", "'").replace("_", " ")
    return re.sub(r"#.*", "", name).strip()


def to_full_image_url(src: str) -> str:
    """/images/thumb/a/ab/Name.png/200px-Name.png -> https://yume.wiki/images/a/ab/Name.png"""
    if not src:
        return ""
    src = src.strip().split("#")[0].split("?")[0]
    if src.startswith("//"):
        src = "https:" + src
    elif src.startswith("/"):
        src = WIKI + src
    m = re.match(r"(https?://[^/]+/images)/thumb/(.+?)/[^/]+$", src)
    if m:
        src = f"{m.group(1)}/{m.group(2)}"
    return src


def img_src(img: Tag) -> str:
    return img.get("data-src") or img.get("src") or ""


def local_name(url: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", unquote(url.split("?")[0].rsplit("/", 1)[-1])).strip(" .")


def clean_method_html(cell: Tag) -> str:
    """Внутренний HTML ячейки «способ получения»: оставляем ссылки/выделение/переносы."""
    cell = BeautifulSoup(str(cell), "lxml").find(["td", "th"])
    for t in cell.find_all(["script", "style", "img", "sup"]):
        t.decompose()
    for t in cell.find_all(True):
        if t.name == "a":
            href = t.get("href", "")
            t.attrs = {"href": WIKI + href if href.startswith("/") else href}
        elif t.name in ("b", "i", "br", "span", "strong", "em", "ul", "ol", "li", "p"):
            t.attrs = {}
        else:
            t.unwrap()
    html = cell.decode_contents()
    html = re.sub(r"\s+", " ", html).strip()
    return re.sub(r"(\s*<br\s*/?>\s*)+$", "", html)


def expand_rows(table: Tag):
    """Строки таблицы с развёрнутыми rowspan -> список списков ячеек (Tag)."""
    rows, carried = [], {}
    for tr in table.find_all("tr"):
        tds = tr.find_all("td", recursive=False)
        if not tds:
            continue  # шапка (th)
        cells, col, it = [], 0, iter(tds)

        def fill():
            nonlocal col
            while col in carried and carried[col][1] > 0:
                cells.append(carried[col][0])
                carried[col][1] -= 1
                col += 1

        for td in it:
            fill()
            span = int(re.sub(r"\D", "", td.get("rowspan", "1")) or 1)
            cells.append(td)
            carried[col] = [td, span - 1]
            col += 1
        fill()
        rows.append(cells)
    return rows


def themes_in_cell(cell: Tag):
    """[(номер, Tag-иконки)].

    Как в app.js: ищем «Theme N» в СЫРОМ HTML ячейки (включая alt/title/href),
    иконка — последний <img> перед совпадением (если такого нет — первый <img> ячейки).
    """
    html = str(cell)
    img_pos = [m.start() for m in re.finditer(r"<img\b", html, re.I)]

    def img_at(pos):
        end = html.find(">", pos)
        frag = BeautifulSoup(html[pos:end + 1], "lxml").find("img")
        return frag

    out = []
    for m in THEME_RE.finditer(html):
        before = [p for p in img_pos if p < m.start()]
        pos = before[-1] if before else (img_pos[0] if img_pos else None)
        out.append((int(m.group(1)), img_at(pos) if pos is not None else None))
    return out


def parse_menu_themes(html: str):
    soup = BeautifulSoup(html, "lxml")
    content = soup.select_one("#mw-content-text") or soup
    tables = [t for t in content.find_all("table") if t.find("td")]
    if not tables:
        raise RuntimeError(f"На странице нет таблиц. HTML сохранён в {DEBUG_HTML.name} — откройте его: "
                           "если там проверка Cloudflare, повторите; иначе пришлите файл, подправлю парсер.")

    themes, seen_keys = {}, set()
    temp_counter = 0
    for t_index, table in enumerate(tables):
        heading = table.find_previous(["h2", "h3", "h4"])
        # «Removed ...» в заголовке перед таблицей = удалённые способы; «Temporary Menu Themes» — временные темы
        removed = bool(heading and "removed" in heading.get_text(" ", strip=True).lower())
        for cells in expand_rows(table):
            if len(cells) < 4:
                continue
            theme_cell, world_cell, method_cell, loc_cell = cells[:4]

            world = None
            for a in world_cell.find_all("a", href=True):
                href = a["href"]
                if href.startswith("/2kki/") and "redlink=1" not in href:
                    world = sanitize_world_name(href[len("/2kki/"):])
                    break
            world_text = world_cell.get_text(" ", strip=True)

            loc_img = loc_cell.find("img")
            loc_url = to_full_image_url(img_src(loc_img)) if loc_img else None
            method = clean_method_html(method_cell)

            found = themes_in_cell(theme_cell)
            if not found and theme_cell.find("img"):
                # временные темы: номера нет, только иконка -> отрицательные id (-1, -2, ...)
                temp_counter -= 1
                found = [(temp_counter, theme_cell.find("img"))]
            for number, img in found:
                # без карты (None) разные способы получения считаем разными записями
                key = (number, world or "", "" if world else (method or ""))
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                if number not in themes:
                    if img is None:
                        continue
                    url = to_full_image_url(img_src(img))
                    themes[number] = {
                        "menuThemeId": number,
                        "filename": url,
                        "localFile": local_name(url),
                        "locations": [],
                    }
                themes[number]["locations"].append({
                    "world": world,
                    "worldText": world_text,
                    "method": method or None,
                    "methodJP": None,
                    "filename": loc_url,
                    "removed": removed,
                })
    if not themes:
        print("[debug] таблиц:", len(tables))
        for i, t in enumerate(tables[:3]):
            rows = expand_rows(t)
            print(f"[debug] таблица {i}: строк {len(rows)}, ячеек в первой строке: {len(rows[0]) if rows else 0}")
            if rows:
                print("[debug] первая строка (кратко):", [c.get_text(' ', strip=True)[:40] for c in rows[0]])
                print("[debug] HTML 1-й ячейки:", str(rows[0][0])[:400])
    return sorted(themes.values(), key=lambda m: (m["menuThemeId"], 0) if m["menuThemeId"] > -1 else (999, -m["menuThemeId"]))


# --------------------------------------------------------------------------- #
#  Загрузка картинок
# --------------------------------------------------------------------------- #
def download_images(themes, session, delay=0.25):
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    urls = {}
    for t in themes:
        urls[t["filename"]] = t["localFile"]
        for l in t["locations"]:
            if l.get("filename"):
                urls[l["filename"]] = local_name(l["filename"])
    ok = skipped = failed = 0
    for url, name in urls.items():
        dest = IMAGES_DIR / name
        if dest.exists() and dest.stat().st_size > 0:
            skipped += 1
            continue
        try:
            r = session.get(url, timeout=30)
            if r.status_code == 200 and r.content:
                dest.write_bytes(r.content)
                ok += 1
            else:
                failed += 1
                print(f"  [!] {r.status_code} {url}")
        except Exception as e:
            failed += 1
            print(f"  [!] {url}: {e}")
        time.sleep(delay)
    print(f"[images] скачано {ok}, уже были {skipped}, ошибок {failed} -> {IMAGES_DIR}")


# --------------------------------------------------------------------------- #
#  data-fallback.json
# --------------------------------------------------------------------------- #
def update_fallback(themes):
    if not FALLBACK_JSON.exists():
        print("[fallback] data-fallback.json не найден — пропускаю")
        return
    fb = json.loads(FALLBACK_JSON.read_text(encoding="utf-8"))
    by_title = {w["title"]: w["id"] for w in fb.get("worldData", [])}
    out, lid = [], 0
    for t in themes:
        locs = []
        for l in t["locations"]:
            locs.append({"id": lid, "worldId": by_title.get(l["world"]) if l["world"] else None,
                         "method": l["method"], "methodJP": l["methodJP"],
                         "filename": l["filename"], "removed": l["removed"]})
            lid += 1
        out.append({"id": t["menuThemeId"], "menuThemeId": t["menuThemeId"],
                    "filename": t["filename"], "locations": locs})
    fb["menuThemeData"] = out
    FALLBACK_JSON.write_text(json.dumps(fb, ensure_ascii=False), encoding="utf-8")
    print(f"[fallback] menuThemeData записан в {FALLBACK_JSON.name} ({len(out)} тем)")


# --------------------------------------------------------------------------- #
#  MySQL
# --------------------------------------------------------------------------- #
def read_db_config():
    """Окружение (как на хостинге) либо config/db.config.js (HOST/USER/PASSWORD/DB)."""
    if os.environ.get("DATABASE_URL"):
        m = re.match(r"mysql://([^:]+):([^@]*)@([^/:]+)(?::(\d+))?/([^?]+)", os.environ["DATABASE_URL"])
        if m:
            return dict(user=m[1], password=m[2], host=m[3], port=int(m[4] or 3306), database=m[5])
    if os.environ.get("DATABASE_NAME"):
        return dict(host=os.environ.get("DATABASE_HOST", "localhost"),
                    user=os.environ.get("DATABASE_USER"), password=os.environ.get("DATABASE_PASS", ""),
                    database=os.environ["DATABASE_NAME"], port=3306)
    if not DB_CONFIG_JS.exists():
        sys.exit(f"Нет {DB_CONFIG_JS} и переменных DATABASE_* — не знаю, куда писать в MySQL.")
    js = DB_CONFIG_JS.read_text(encoding="utf-8")

    def val(key):
        m = re.search(rf"{key}\s*[:=]\s*(['\"])(.*?)\1", js)
        return m.group(2) if m else None

    cfg = dict(host=val("HOST") or "localhost", user=val("USER"), password=val("PASSWORD") or "",
               database=val("DB"), port=3306)
    if not cfg["user"] or not cfg["database"]:
        sys.exit("Не удалось прочитать USER/DB из config/db.config.js")
    return cfg


def sync_mysql(themes):
    try:
        import pymysql
        conn = pymysql.connect(charset="utf8mb4", autocommit=False, **read_db_config())
    except ImportError:
        import mysql.connector as mc
        conn = mc.connect(charset="utf8mb4", **read_db_config())
    cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS menu_themes (
        id INT AUTO_INCREMENT PRIMARY KEY, menuThemeId INT NOT NULL, filename VARCHAR(255) NOT NULL)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS menu_theme_locations (
        id INT AUTO_INCREMENT PRIMARY KEY, menuThemeId INT NOT NULL, worldId INT NULL,
        method VARCHAR(1000) NULL, methodJP VARCHAR(1000) NULL, filename VARCHAR(255) NULL,
        removed BIT NOT NULL,
        FOREIGN KEY (menuThemeId) REFERENCES menu_themes (id) ON DELETE CASCADE,
        FOREIGN KEY (worldId) REFERENCES worlds (id) ON DELETE CASCADE)""")

    cur.execute("SELECT id, title FROM worlds")
    world_ids = {title: wid for wid, title in cur.fetchall()}

    cur.execute("SELECT id, menuThemeId FROM menu_themes")
    theme_row = {mid: rid for rid, mid in cur.fetchall()}
    wanted = {t["menuThemeId"] for t in themes}

    for t in themes:
        if t["menuThemeId"] in theme_row:
            cur.execute("UPDATE menu_themes SET filename=%s WHERE id=%s",
                        (t["filename"][:255], theme_row[t["menuThemeId"]]))
        else:
            cur.execute("INSERT INTO menu_themes (menuThemeId, filename) VALUES (%s, %s)",
                        (t["menuThemeId"], t["filename"][:255]))
            theme_row[t["menuThemeId"]] = cur.lastrowid
    for mid in set(theme_row) - wanted:
        cur.execute("DELETE FROM menu_themes WHERE id=%s", (theme_row[mid],))

    cur.execute("SELECT id, menuThemeId, worldId FROM menu_theme_locations")
    existing = {(mt, w): rid for rid, mt, w in cur.fetchall()}
    seen, unknown = set(), set()
    for t in themes:
        row_id = theme_row[t["menuThemeId"]]
        for l in t["locations"]:
            wid = world_ids.get(l["world"]) if l["world"] else None
            if l["world"] and wid is None:
                unknown.add(l["world"])
            key = (row_id, wid)
            if key in seen:
                continue
            seen.add(key)
            args = ((l["method"] or None) and l["method"][:1000], None,
                    (l["filename"] or None) and l["filename"][:255], 1 if l["removed"] else 0)
            if key in existing:
                cur.execute("UPDATE menu_theme_locations SET method=%s, methodJP=%s, filename=%s, removed=%s WHERE id=%s",
                            args + (existing[key],))
            else:
                cur.execute("INSERT INTO menu_theme_locations (menuThemeId, worldId, method, methodJP, filename, removed)"
                            " VALUES (%s, %s, %s, %s, %s, %s)", (row_id, wid) + args)
    for key, rid in existing.items():
        if key not in seen:
            cur.execute("DELETE FROM menu_theme_locations WHERE id=%s", (rid,))
    conn.commit()
    conn.close()
    print(f"[mysql] синхронизировано: {len(themes)} тем, {len(seen)} локаций")
    if unknown:
        print(f"[mysql] карты не найдены в таблице worlds (worldId=NULL): {sorted(unknown)[:10]}"
              f"{' ...' if len(unknown) > 10 else ''}")


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="Парсер Menu Themes (yume.wiki) для Yume 2kki Explorer")
    ap.add_argument("--html", help="разобрать сохранённый HTML вместо запуска браузера")
    ap.add_argument("--headless", action="store_true", help="браузер без окна (Cloudflare может не пустить)")
    ap.add_argument("--no-download", action="store_true", help="не скачивать картинки")
    ap.add_argument("--mysql", action="store_true", help="записать результат в MySQL")
    ap.add_argument("--update-fallback", action="store_true", help="обновить menuThemeData в data-fallback.json")
    ap.add_argument("--out", default=str(OUT_JSON), help="куда писать JSON (по умолчанию menu_themes.json)")
    args = ap.parse_args()

    if args.html:
        html = Path(args.html).read_text(encoding="utf-8", errors="replace")
        session = make_plain_session()
    else:
        html, session = fetch_html_with_browser(args.headless)

    themes = parse_menu_themes(html)
    n_loc = sum(len(t["locations"]) for t in themes)
    print(f"[parse] тем: {len(themes)}, локаций: {n_loc}")
    if not themes:
        sys.exit("Ничего не найдено — структура страницы изменилась?")

    Path(args.out).write_text(json.dumps(themes, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[json] {args.out}")

    if not args.no_download:
        download_images(themes, session)
    if args.update_fallback:
        update_fallback(themes)
    if args.mysql:
        sync_mysql(themes)
    print("Готово. Перезапустите сервер (node app.js) и откройте Menu Themes.")


if __name__ == "__main__":
    main()
