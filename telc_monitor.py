#!/usr/bin/env python3
"""
telc_monitor.py — theo doi suat thi telc tai abroad.ibk-merseburg.de

Chi dung thu vien chuan (khong pip install -> khong dinh loi `cache: pip`).

Ba nguon tin hieu, doc lap nhau, hong mot cai khong lam chet cac cai con lai:
  1. WooCommerce Store API  -> danh sach san pham + ton kho (tin hieu manh nhat)
  2. Trang Exam Dates       -> hash noi dung da lam sach
  3. wp-sitemap.xml         -> URL moi xuat hien (event/product post type)

Trang thai luu o state/telc.json. Lan chay dau tien chi ghi baseline, khong bao.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

BASE = "https://abroad.ibk-merseburg.de"

STORE_API_CANDIDATES = [
    f"{BASE}/wp-json/wc/store/v1/products?per_page=100",
    f"{BASE}/wp-json/wc/store/products?per_page=100",
]
EXAM_PAGES = [
    f"{BASE}/",
    f"{BASE}/home/lich-thi/",
    f"{BASE}/vi/home/lich-thi/",
]
SITEMAP_CANDIDATES = [
    f"{BASE}/wp-sitemap.xml",      # WordPress core
    f"{BASE}/sitemap_index.xml",   # Yoast SEO
    f"{BASE}/sitemap.xml",         # Rank Math / plugin khac
]

STATE_PATH = os.environ.get("TELC_STATE", "state/telc.json")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 " \
     "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
TIMEOUT = 25
ICT = timezone(timedelta(hours=7))


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def fetch(url: str, retries: int = 2) -> tuple[int, str]:
    """Tra ve (status, body). Khong raise — loi mang tra ve status 0."""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "text/html,application/json,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "vi,en;q=0.8,de;q=0.6",
            "Cache-Control": "no-cache",
        },
    )
    last = (0, "")
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                raw = resp.read()
                charset = resp.headers.get_content_charset() or "utf-8"
                return resp.status, raw.decode(charset, errors="replace")
        except urllib.error.HTTPError as e:
            last = (e.code, "")
            if e.code in (404, 401, 403):
                return last                      # khong retry loi vinh vien
        except Exception as e:                   # noqa: BLE001
            last = (0, f"{type(e).__name__}: {e}")
        if attempt < retries:
            time.sleep(2 * (attempt + 1))
    return last


# --------------------------------------------------------------------------- #
# 1. Store API
# --------------------------------------------------------------------------- #
def check_products() -> tuple[dict | None, str | None]:
    """Tra ve ({id: {...}}, None) neu API song, nguoc lai (None, ly_do)."""
    reason = "khong tim thay endpoint Store API"
    for url in STORE_API_CANDIDATES:
        status, body = fetch(url)
        if status != 200:
            reason = f"HTTP {status} tai {urllib.parse.urlparse(url).path}"
            continue
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            reason = "endpoint tra ve khong phai JSON"
            continue
        if not isinstance(data, list):
            reason = "JSON khong phai danh sach san pham"
            continue

        products = {}
        for p in data:
            pid = str(p.get("id"))
            prices = p.get("prices") or {}
            products[pid] = {
                "name": (p.get("name") or "").strip(),
                "permalink": p.get("permalink") or "",
                "in_stock": bool(p.get("is_in_stock")),
                "stock": p.get("stock_availability", {}).get("text")
                         or p.get("low_stock_remaining"),
                "price": prices.get("price"),
                "currency": prices.get("currency_code"),
            }
        return products, None
    return None, reason


def diff_products(old: dict, new: dict) -> list[str]:
    events = []
    for pid, p in new.items():
        name = p["name"] or f"san pham #{pid}"
        link = p["permalink"]
        if pid not in old:
            state = "CON CHO" if p["in_stock"] else "het cho"
            events.append(f"🆕 <b>Suat thi moi</b>: {esc(name)} ({state})\n{link}")
            continue
        o = old[pid]
        if p["in_stock"] and not o.get("in_stock"):
            events.append(f"🟢 <b>MO BAN LAI</b>: {esc(name)}\n{link}")
        elif not p["in_stock"] and o.get("in_stock"):
            events.append(f"🔴 Het cho: {esc(name)}")
        if p.get("price") != o.get("price"):
            events.append(
                f"💶 Doi gia: {esc(name)} — {o.get('price')} → {p.get('price')}"
            )
        if p.get("stock") and p.get("stock") != o.get("stock"):
            events.append(f"📉 Ton kho: {esc(name)} — {esc(str(p['stock']))}")
    for pid, o in old.items():
        if pid not in new:
            events.append(f"➖ Go bo: {esc(o.get('name') or pid)}")
    return events


# --------------------------------------------------------------------------- #
# 1b. WP REST: Store API 404 khong co nghia la REST tat han
# --------------------------------------------------------------------------- #
CPT_HINTS = ("product", "ticket", "event", "exam", "pruef", "telc",
             "lich", "thi", "termin", "course", "kurs")


def check_cpts() -> tuple[dict, str]:
    """Tra ve ({rest_base: [{id,title,link}]}, ghi_chu)."""
    status, body = fetch(f"{BASE}/wp-json/wp/v2/types")
    if status != 200:
        return {}, f"HTTP {status} tai /wp-json/wp/v2/types"
    try:
        types = json.loads(body)
    except json.JSONDecodeError:
        return {}, "/types tra ve khong phai JSON"

    bases = []
    for slug, info in (types or {}).items():
        base = (info or {}).get("rest_base") or slug
        blob = f"{slug} {base} {(info or {}).get('name','')}".lower()
        if any(h in blob for h in CPT_HINTS):
            bases.append(base)

    found = {}
    for base in bases[:8]:
        st, b = fetch(f"{BASE}/wp-json/wp/v2/{base}"
                      f"?per_page=30&_fields=id,link,title,date", retries=1)
        if st != 200:
            continue
        try:
            items = json.loads(b)
        except json.JSONDecodeError:
            continue
        if isinstance(items, list):
            found[base] = [{
                "id": it.get("id"),
                "link": it.get("link", ""),
                "title": ((it.get("title") or {}).get("rendered") or "").strip(),
            } for it in items]
    note = f"{len(bases)} post type nghi van, {len(found)} doc duoc" if bases \
        else "khong co post type nao khop tu khoa"
    return found, note


def diff_cpts(old: dict, new: dict) -> list[str]:
    events = []
    for base, items in new.items():
        old_ids = {str(i.get("id")) for i in (old.get(base) or [])}
        if base not in old:
            continue                      # post type moi xuat hien -> bao rieng
        for it in items:
            if str(it.get("id")) not in old_ids:
                events.append(f"🎟️ <b>SUAT THI MOI ({esc(base)})</b>: "
                              f"{esc(it.get('title') or it.get('id'))}\n{it.get('link','')}")
    for base in new:
        if base not in old and old:
            events.append(f"📦 Post type moi lo dien: <code>{esc(base)}</code> "
                          f"({len(new[base])} muc)")
    return events


# --------------------------------------------------------------------------- #
# 2. Hash trang Exam Dates
# --------------------------------------------------------------------------- #
NOISE = [
    re.compile(r"<script\b.*?</script>", re.S | re.I),
    re.compile(r"<style\b.*?</style>", re.S | re.I),
    re.compile(r"<!--.*?-->", re.S),
    re.compile(r"<noscript\b.*?</noscript>", re.S | re.I),
]
VOLATILE = [
    re.compile(r'\b(nonce|_wpnonce|wc-ajax|ver|_fs_blog_admin)=[^"&\'\s]+', re.I),
    re.compile(r'\bwp-json[^"\']*_locale=[^"&\'\s]+', re.I),
    re.compile(r"\b[0-9a-f]{32}\b"),           # cart hash / nonce tho
    re.compile(r"\?ver=[\d.]+"),
]
EMPTY_MARKERS = ("no posts matched", "khong co bai viet", "keine beitr")


MIN_TEXT = 200   # duoi nguong nay coi nhu trich hut, dung ban toan trang


def _to_text(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html)
    text = urllib.parse.unquote(text)
    return re.sub(r"\s+", " ", text).strip().lower()


def clean_html(html: str) -> tuple[str, bool]:
    """Tra ve (text, da_lay_duoc_vung_noi_dung)."""
    for rx in NOISE:
        html = rx.sub(" ", html)
    for rx in VOLATILE:
        html = rx.sub(" ", html)
    full = _to_text(html)
    for pat in (r"<main\b[^>]*>(.*?)</main>", r"<article\b[^>]*>(.*?)</article>"):
        m = re.search(pat, html, re.S | re.I)
        if m:
            part = _to_text(m.group(1))
            if len(part) >= MIN_TEXT:
                return part, True
    return full, False


def check_pages() -> dict:
    out = {}
    for url in EXAM_PAGES:
        status, body = fetch(url)
        if status != 200:
            out[url] = {"status": status}
            continue
        text, real_content = clean_html(body)
        out[url] = {
            "status": 200,
            "hash": hashlib.sha256(text.encode()).hexdigest()[:16],
            "len": len(text),
            "empty": any(k in text for k in EMPTY_MARKERS),
            # khong trich duoc <main>/<article> -> chi dang hash header/footer,
            # rat co the noi dung ky thi duoc render bang JS
            "suspect": (not real_content) or len(text) < MIN_TEXT,
        }
    return out


def diff_pages(old: dict, new: dict) -> list[str]:
    events = []
    for url, cur in new.items():
        prev = old.get(url, {})
        if cur.get("status") != 200:
            continue
        if prev.get("status") == 200 and prev.get("hash") != cur.get("hash"):
            delta = cur["len"] - prev.get("len", 0)
            tag = "ℹ️" if cur.get("suspect") else "📄"
            note = " (chi la phan khung trang)" if cur.get("suspect") else ""
            events.append(f"{tag} Trang thay doi ({delta:+d} ky tu){note}\n{url}")
        if prev.get("empty") and not cur.get("empty"):
            events.append(f"⚡️ <b>Danh sach ky thi da co noi dung!</b>\n{url}")
    return events


# --------------------------------------------------------------------------- #
# 3. Sitemap
# --------------------------------------------------------------------------- #
LOC_RX = re.compile(r"<loc>\s*([^<]+?)\s*</loc>", re.I)


def check_sitemap(max_subs: int = 12) -> tuple[list[str] | None, str]:
    body, src = None, ""
    for url in SITEMAP_CANDIDATES:
        status, b = fetch(url)
        if status == 200 and "<loc>" in b:
            body, src = b, url
            break
        src = f"HTTP {status} tai {urllib.parse.urlparse(url).path}"
    if body is None:
        return None, src
    subs = LOC_RX.findall(body)
    urls: set[str] = set()
    if any(s.endswith(".xml") for s in subs):
        for sub in subs[:max_subs]:
            s2, b2 = fetch(sub, retries=1)
            if s2 == 200:
                urls.update(LOC_RX.findall(b2))
    else:
        urls.update(subs)
    if not urls:
        urls.update(subs)          # sub-sitemap rong: theo doi chinh index
    if not urls:
        peek = re.sub(r"\s+", " ", body[:160]).strip()
        return sorted(urls), f"{src} — 0 URL, dau file: {peek!r}"
    return sorted(urls), f"{src} — {len(urls)} URL"


KEYWORDS = ("telc", "b1", "exam", "ticket", "lich", "thi", "pruef", "product")


def diff_sitemap(old: list, new: list) -> list[str]:
    added = [u for u in new if u not in set(old)]
    if not added:
        return []
    hot = [u for u in added if any(k in u.lower() for k in KEYWORDS)]
    picked = hot or added
    lines = "\n".join(picked[:8])
    tag = "🎯 URL moi lien quan ky thi" if hot else "🔗 URL moi tren site"
    more = f"\n… va {len(picked) - 8} URL nua" if len(picked) > 8 else ""
    return [f"{tag} ({len(added)}):\n{lines}{more}"]


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #
def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def notify(text: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        print("[warn] thieu TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID — in ra stdout")
        print(text)
        return False
    ok = True
    for chunk in [text[i:i + 3800] for i in range(0, len(text), 3800)]:
        payload = urllib.parse.urlencode({
            "chat_id": chat,
            "text": chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": "false",
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                if r.status != 200:
                    ok = False
                    print(f"[error] telegram HTTP {r.status}")
        except Exception as e:                    # noqa: BLE001
            ok = False
            # KHONG in exception tho: URL chua bot token
            print(f"[error] gui telegram that bai: {type(e).__name__}")
    return ok


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #
def load_state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_PATH) or ".", exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, STATE_PATH)


# --------------------------------------------------------------------------- #
def main() -> int:
    old = load_state()
    first_run = not old
    now = datetime.now(ICT).strftime("%d/%m/%Y %H:%M ICT")

    products, api_reason = check_products()
    cpts, cpt_note = check_cpts()
    pages = check_pages()
    sitemap, sitemap_src = check_sitemap()

    events: list[str] = []
    if products is not None:
        events += diff_products(old.get("products") or {}, products)
    if cpts:
        events = diff_cpts(old.get("cpts") or {}, cpts) + events
    events += diff_pages(old.get("pages") or {}, pages)
    if sitemap is not None:
        events += diff_sitemap(old.get("sitemap") or [], sitemap)

    new_state = {
        "checked_at": now,
        "products": products if products is not None else old.get("products", {}),
        "products_api": "ok" if products is not None else api_reason,
        "cpts": cpts if cpts else old.get("cpts", {}),
        "pages": pages,
        "sitemap": sitemap if sitemap is not None else old.get("sitemap", []),
        "sources": {
            "store_api": "ok" if products is not None else api_reason,
            "sitemap": sitemap_src if sitemap is not None else f"khong doc duoc ({sitemap_src})",
            "wp_rest": cpt_note,
            "pages": {u: v.get("status") for u, v in pages.items()},
        },
    }
    save_state(new_state)

    n_prod = len(new_state["products"] or {})
    src = new_state["sources"]
    print(f"[{now}] products={n_prod} sitemap={len(new_state['sitemap'])} "
          f"events={len(events)}")
    print(f"  store_api : {src['store_api']}")
    print(f"  sitemap   : {src['sitemap']}")
    print(f"  wp_rest   : {src['wp_rest']}")
    for base, items in (new_state.get("cpts") or {}).items():
        print(f"  cpt       : {base} — {len(items)} muc")
    for u, st in src["pages"].items():
        flag = " ⚠︎ text ngan, co the render bang JS" \
            if pages.get(u, {}).get("suspect") else ""
        print(f"  page      : HTTP {st} — {u}{flag}")

    live_pages = [u for u, st in src["pages"].items() if st == 200]
    if not live_pages and products is None and sitemap is None:
        notify("🛑 <b>Monitor telc: KHONG nguon nao truy cap duoc</b>\n"
               "Co the site chan IP cua GitHub runner. Kiem tra log Actions.")
        return 1

    if first_run:
        notify(f"✅ <b>Monitor telc IBK da khoi dong</b>\n"
               f"Baseline: {n_prod} san pham, {len(new_state['sitemap'])} URL\n"
               f"• Store API: {esc(str(src['store_api']))}\n"
               f"• Sitemap: {esc(str(src['sitemap']))}\n"
               f"• WP REST: {esc(str(src['wp_rest']))}\n"
               f"• Trang theo doi: {len(live_pages)}/{len(src['pages'])} truy cap duoc\n"
               f"Luc {now}")
        return 0

    if events:
        body = "\n\n".join(events)
        notify(f"🔔 <b>IBK Merseburg — thay doi phat hien luc {now}</b>\n\n{body}\n\n"
               f"👉 {BASE}/home/lich-thi/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
