#!/usr/bin/env python3
"""
Local Events Email Agent
Scrapes configured websites for events, dedupes against previously seen items,
and sends a daily HTML digest email.

Usage:
    python event_agent.py            # normal run (scrape + email)
    python event_agent.py --dry-run  # scrape + print, no email
    python event_agent.py --test-email  # send test email only

Requires: pip install requests beautifulsoup4
"""

import argparse
import hashlib
import json
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


def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def load_seen():
    if SEEN_PATH.exists():
        with open(SEEN_PATH, encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_seen(seen):
    # Keep the file from growing forever: cap at 5000 most recent hashes
    with open(SEEN_PATH, "w", encoding="utf-8") as f:
        json.dump(list(seen)[-5000:], f)


def event_id(site_name, title, link):
    return hashlib.sha256(f"{site_name}|{title}|{link}".encode()).hexdigest()[:16]


def text_of(el, selector):
    if not selector:
        return ""
    found = el.select_one(selector)
    return found.get_text(strip=True) if found else ""


def scrape_site(site):
    """Scrape one site definition. Returns list of event dicts."""
    events = []
    try:
        resp = requests.get(site["url"], headers=HEADERS, timeout=20)
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding
        soup = BeautifulSoup(resp.text, "html.parser")

        for item in soup.select(site["item_selector"])[: site.get("max_items", 15)]:
            title = text_of(item, site.get("title_selector")) or item.get_text(strip=True)[:120]
            if not title:
                continue

            link = ""
            link_el = item.select_one(site.get("link_selector", "a"))
            if link_el and link_el.get("href"):
                link = requests.compat.urljoin(site["url"], link_el["href"])

            events.append({
                "site": site["name"],
                "title": title,
                "date": text_of(item, site.get("date_selector")),
                "location": text_of(item, site.get("location_selector")),
                "link": link or site["url"],
            })
    except Exception as e:
        print(f"[WARN] {site['name']}: {e}", file=sys.stderr)
    return events


def build_html(events_by_site, cfg):
    today = datetime.now().strftime("%d/%m/%Y")
    rtl = cfg.get("email", {}).get("rtl", False)
    direction = 'dir="rtl"' if rtl else ""

    parts = [f"""<html><body {direction} style="font-family:Segoe UI,Arial,sans-serif;
        max-width:640px;margin:auto;color:#222;">
        <h2 style="border-bottom:2px solid #4a6fa5;padding-bottom:8px;">
        {cfg.get('email', {}).get('subject_prefix', 'Local Events Digest')} &mdash; {today}</h2>"""]

    total = sum(len(v) for v in events_by_site.values())
    if total == 0:
        parts.append("<p>No new events found today.</p>")
    else:
        for site_name, events in events_by_site.items():
            if not events:
                continue
            parts.append(f'<h3 style="color:#4a6fa5;margin-bottom:4px;">{site_name}</h3><ul style="padding-inline-start:20px;">')
            for ev in events:
                meta = " &middot; ".join(x for x in [ev["date"], ev["location"]] if x)
                meta_html = f'<br><span style="color:#777;font-size:13px;">{meta}</span>' if meta else ""
                parts.append(
                    f'<li style="margin-bottom:10px;">'
                    f'<a href="{ev["link"]}" style="color:#222;text-decoration:none;font-weight:600;">'
                    f'{ev["title"]}</a>{meta_html}</li>'
                )
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Scrape and print, no email")
    parser.add_argument("--test-email", action="store_true", help="Send a test email")
    parser.add_argument("--no-dedupe", action="store_true", help="Include previously seen events")
    args = parser.parse_args()

    cfg = load_config()

    if args.test_email:
        send_email("<p>Test email from event_agent.py — SMTP config works.</p>",
                   "Event Agent — Test", cfg)
        print("Test email sent.")
        return

    seen = load_seen()
    events_by_site = {}
    new_ids = []

    for site in cfg["sites"]:
        if not site.get("enabled", True):
            continue
        found = scrape_site(site)
        fresh = []
        for ev in found:
            eid = event_id(ev["site"], ev["title"], ev["link"])
            if args.no_dedupe or eid not in seen:
                fresh.append(ev)
                new_ids.append(eid)
        events_by_site[site["name"]] = fresh
        print(f"{site['name']}: {len(found)} found, {len(fresh)} new")

    html, total = build_html(events_by_site, cfg)

    if args.dry_run:
        print(f"\n--- {total} new events (dry run, no email) ---")
        for site_name, evs in events_by_site.items():
            for ev in evs:
                print(f"[{site_name}] {ev['title']} | {ev['date']} | {ev['link']}")
        return

    if total == 0 and not cfg.get("email", {}).get("send_when_empty", False):
        print("No new events — skipping email.")
    else:
        today = datetime.now().strftime("%d/%m")
        subject = f"{cfg['email'].get('subject_prefix', 'Local Events')} — {today} ({total} new)"
        send_email(html, subject, cfg)
        print(f"Email sent: {total} new events.")

    seen.update(new_ids)
    save_seen(seen)


if __name__ == "__main__":
    main()
