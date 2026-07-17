#!/usr/bin/env python3
"""
Local Events Email Agent v2
Strategy per site:
  1. Fetch with requests, extract schema.org Event JSON-LD (no selectors needed)
  2. If nothing found: render with Playwright (headless Chromium), retry JSON-LD
  3. If still nothing and CSS selectors are configured: CSS fallback
Dedupes against seen_events.json, emails a daily RTL HTML digest.

Usage:
    python event_agent.py             # scrape + email
    python event_agent.py --dry-run   # scrape + print, no email
    python event_agent.py --test-email

Requires: pip install requests beautifulsoup4 playwright
          playwright install chromium
"""

import argparse
import hashlib
import json
import re
import smtplib
import sys
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"
SEEN_PATH = BASE_DIR / "seen_events.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept-Language": "he-IL,he;q=0.9,en-US;q=0.8,en;q=0.7",
}


# ---------- config / state ----------

def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def load_seen():
    if SEEN_PATH.exists():
        with open(SEEN_PATH, encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_seen(seen):
    with open(SEEN_PATH, "w", encoding="utf-8") as f:
        json.dump(sorted(seen)[-5000:], f)


def event_id(site_name, title, date):
    return hashlib.sha256(f"{site_name}|{title}|{date}".encode()).hexdigest()[:16]


# ---------- extraction: JSON-LD ----------

def _fmt_date(iso):
    """'2026-07-20T21:00:00+03:00' -> '20/07/2026 21:00'"""
    if not iso:
        return ""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2}))?", str(iso))
    if not m:
        return str(iso)
    y, mo, d, hh, mm = m.groups()
    out = f"{d}/{mo}/{y}"
    if hh:
        out += f" {hh}:{mm}"
    return out


def _walk_ld(node, out):
    """Recursively collect schema.org Event objects from a JSON-LD structure."""
    if isinstance(node, list):
        for item in node:
            _walk_ld(item, out)
    elif isinstance(node, dict):
        t = node.get("@type", "")
        types = t if isinstance(t, list) else [t]
        if any("Event" in str(x) for x in types):
            out.append(node)
        for key in ("@graph", "itemListElement", "item", "subEvent", "events"):
            if key in node:
                _walk_ld(node[key], out)


def events_from_jsonld(html, site, base_url):
    soup = BeautifulSoup(html, "html.parser")
    raw_events = []
    for tag in soup.find_all("script", type=re.compile("ld\\+json")):
        try:
            data = json.loads(tag.string or tag.get_text() or "")
        except (json.JSONDecodeError, TypeError):
            continue
        _walk_ld(data, raw_events)

    events = []
    for ev in raw_events:
        name = str(ev.get("name", "")).strip()
        if not name:
            continue
        loc = ev.get("location", {})
        if isinstance(loc, list):
            loc = loc[0] if loc else {}
        loc_name = loc.get("name", "") if isinstance(loc, dict) else str(loc)
        url = ev.get("url", "") or base_url
        events.append({
            "site": site["name"],
            "title": name,
            "date": _fmt_date(ev.get("startDate", "")),
            "location": str(loc_name).strip(),
            "link": requests.compat.urljoin(base_url, url),
        })
    return events


# ---------- extraction: CSS fallback ----------

def _sel_text(el, selector):
    if not selector or selector.startswith("VERIFY"):
        return ""
    found = el.select_one(selector)
    return found.get_text(strip=True) if found else ""


def events_from_css(html, site, base_url):
    sel = site.get("item_selector", "")
    if not sel or sel.startswith("VERIFY"):
        return []
    soup = BeautifulSoup(html, "html.parser")
    events = []
    for item in soup.select(sel)[: site.get("max_items", 15)]:
        title = _sel_text(item, site.get("title_selector")) or item.get_text(strip=True)[:120]
        if not title:
            continue
        link = base_url
        a = item.select_one(site.get("link_selector", "a"))
        if a and a.get("href"):
            link = requests.compat.urljoin(base_url, a["href"])
        events.append({
            "site": site["name"],
            "title": title,
            "date": _sel_text(item, site.get("date_selector")),
            "location": _sel_text(item, site.get("location_selector")),
            "link": link,
        })
    return events


# ---------- fetching ----------

def fetch_static(url):
    resp = requests.get(url, headers=HEADERS, timeout=25)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding
    return resp.text


_PW = {"browser": None, "pw": None}


def fetch_rendered(url):
    """Playwright render; browser instance reused across sites."""
    from playwright.sync_api import sync_playwright
    if _PW["browser"] is None:
        _PW["pw"] = sync_playwright().start()
        _PW["browser"] = _PW["pw"].chromium.launch(headless=True)
    page = _PW["browser"].new_page(extra_http_headers={"Accept-Language": HEADERS["Accept-Language"]})
    try:
        page.goto(url, timeout=45000, wait_until="networkidle")
    except Exception:
        pass  # take whatever rendered before timeout
    html = page.content()
    page.close()
    return html


def close_browser():
    if _PW["browser"]:
        _PW["browser"].close()
        _PW["pw"].stop()


def scrape_site(site):
    url = site["url"]
    if not url.startswith("http"):
        print(f"[SKIP] {site['name']}: no valid URL")
        return []
    try:
        html = fetch_static(url)
        events = events_from_jsonld(html, site, url) or events_from_css(html, site, url)
        method = "static"
        if not events and site.get("render_js", True):
            html = fetch_rendered(url)
            events = events_from_jsonld(html, site, url) or events_from_css(html, site, url)
            method = "playwright"
        # dedupe within page, cap
        uniq, seen_titles = [], set()
        for ev in events:
            key = (ev["title"], ev["date"])
            if key not in seen_titles:
                seen_titles.add(key)
                uniq.append(ev)
        print(f"{site['name']}: {len(uniq)} found ({method})")
        return uniq[: site.get("max_items", 15)]
    except Exception as e:
        print(f"[WARN] {site['name']}: {e}", file=sys.stderr)
        return []


# ---------- email ----------

def build_html(events_by_site, cfg):
    today = datetime.now().strftime("%d/%m/%Y")
    direction = 'dir="rtl"' if cfg.get("email", {}).get("rtl", False) else ""
    parts = [f"""<html><body {direction} style="font-family:Segoe UI,Arial,sans-serif;
        max-width:640px;margin:auto;color:#222;">
        <h2 style="border-bottom:2px solid #4a6fa5;padding-bottom:8px;">
        {cfg['email'].get('subject_prefix', 'Events')} &mdash; {today}</h2>"""]
    total = sum(len(v) for v in events_by_site.values())
    for site_name, events in events_by_site.items():
        if not events:
            continue
        parts.append(f'<h3 style="color:#4a6fa5;margin-bottom:4px;">{site_name}</h3><ul style="padding-inline-start:20px;">')
        for ev in events:
            meta = " &middot; ".join(x for x in [ev["date"], ev["location"]] if x)
            meta_html = f'<br><span style="color:#777;font-size:13px;">{meta}</span>' if meta else ""
            parts.append(
                f'<li style="margin-bottom:10px;"><a href="{ev["link"]}" '
                f'style="color:#222;text-decoration:none;font-weight:600;">{ev["title"]}</a>{meta_html}</li>')
        parts.append("</ul>")
    parts.append('<p style="color:#aaa;font-size:12px;">Automated digest &mdash; event_agent.py</p></body></html>')
    return "".join(parts), total


def send_email(html, subject, cfg):
    em = cfg["email"]
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = em["from"]
    msg["To"] = ", ".join(em["to"])
    msg.attach(MIMEText(html, "html", "utf-8"))
    with smtplib.SMTP(em["smtp_host"], em["smtp_port"]) as server:
        server.starttls()
        server.login(em["smtp_user"], em["smtp_password"])
        server.sendmail(em["from"], em["to"], msg.as_string())


# ---------- main ----------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--test-email", action="store_true")
    parser.add_argument("--no-dedupe", action="store_true")
    args = parser.parse_args()

    cfg = load_config()

    if args.test_email:
        send_email("<p>Test email from event_agent.py</p>", "Event Agent — Test", cfg)
        print("Test email sent.")
        return

    seen = load_seen()
    events_by_site, new_ids = {}, []

    try:
        for site in cfg["sites"]:
            if not site.get("enabled", True):
                continue
            found = scrape_site(site)
            fresh = []
            for ev in found:
                eid = event_id(ev["site"], ev["title"], ev["date"])
                if args.no_dedupe or eid not in seen:
                    fresh.append(ev)
                    new_ids.append(eid)
            events_by_site[site["name"]] = fresh
            if fresh:
                print(f"  -> {len(fresh)} new")
    finally:
        close_browser()

    html, total = build_html(events_by_site, cfg)

    if args.dry_run:
        print(f"\n--- {total} new events (dry run) ---")
        for evs in events_by_site.values():
            for ev in evs:
                print(f"[{ev['site']}] {ev['title']} | {ev['date']} | {ev['link']}")
        return

    if total == 0 and not cfg["email"].get("send_when_empty", False):
        print("No new events — skipping email.")
    else:
        subject = f"{cfg['email'].get('subject_prefix', 'Events')} — {datetime.now():%d/%m} ({total})"
        send_email(html, subject, cfg)
        print(f"Email sent: {total} new events.")

    seen.update(new_ids)
    save_seen(seen)


if __name__ == "__main__":
    main()
