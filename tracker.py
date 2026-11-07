"""Flipkart Price Tracker: runs daily from GitHub Actions.

Flow: fetch ACTIVE/ALERT_SENT rows -> scrape price (BeautifulSoup, then Groq (LLM)
fallback) -> email if price <= target -> update Supabase.
"""
from __future__ import annotations

import html
import json
import logging
import os
import random
import re
import smtplib
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

import requests
from bs4 import BeautifulSoup
from supabase import Client, create_client

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tracker")

# ---------------------------------------------------------------- config
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "qwen/qwen3.8-27b")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
EMAIL_USER = os.environ.get("EMAIL_USER", "")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD", "")
EMAIL_TO = os.environ.get("EMAIL_TO") or EMAIL_USER  # optional separate recipient

TABLE = "price_trackers"
REQUEST_TIMEOUT = 25
MAX_LLM_CHARS = 12_000

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
    "Referer": "https://www.google.com/",
    "Upgrade-Insecure-Requests": "1",
}


@dataclass
class Product:
    title: Optional[str]
    price: Optional[float]


# ---------------------------------------------------------------- helpers
def parse_price(text: object) -> Optional[float]:
    """'₹1,29,999' -> 129999.0"""
    if text is None:
        return None
    cleaned = re.sub(r"[^\d.]", "", str(text).replace(",", ""))
    try:
        value = float(cleaned)
    except ValueError:
        return None
    return value if value > 0 else None


def fetch_html(url: str) -> str:
    resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.text


# ---------------------------------------------------------------- scraper 1: BeautifulSoup
def _from_json_ld(soup: BeautifulSoup) -> Product:
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict) or item.get("@type") != "Product":
                continue
            offers = item.get("offers")
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            price = parse_price((offers or {}).get("price"))
            title = item.get("name")
            if price or title:
                return Product(title, price)
    return Product(None, None)


# Flipkart obfuscates class names and changes them often; keep several candidates.
PRICE_SELECTORS = ["div.Nx9bqj.CxhGGd", "div.Nx9bqj", "div._30jeq3._16Jk6d", "div._30jeq3"]
TITLE_SELECTORS = ["span.VU-ZEz", "span.B_NuCI", "h1.yhB1nd", "h1"]


def scrape_with_bs4(page: str) -> Product:
    soup = BeautifulSoup(page, "lxml")

    product = _from_json_ld(soup)
    title, price = product.title, product.price

    if price is None:
        for sel in PRICE_SELECTORS:
            node = soup.select_one(sel)
            if node and (p := parse_price(node.get_text())):
                price = p
                break

    if not title:
        for sel in TITLE_SELECTORS:
            node = soup.select_one(sel)
            if node and node.get_text(strip=True):
                title = node.get_text(" ", strip=True)
                break

    if price is None:  # last resort: "₹12,999" inside meta description / title
        meta = soup.find("meta", attrs={"name": "Description"}) or soup.find(
            "meta", attrs={"name": "description"}
        )
        blob = " ".join(filter(None, [meta.get("content") if meta else "", soup.title.string if soup.title else ""]))
        m = re.search(r"₹\s?([\d,]+(?:\.\d+)?)", blob)
        if m:
            price = parse_price(m.group(1))

    return Product(title, price)


# ---------------------------------------------------------------- scraper 2: Groq fallback
def _condense_for_llm(page: str) -> str:
    """Strip scripts/styles but keep JSON-LD and visible text, trimmed for tokens."""
    soup = BeautifulSoup(page, "lxml")
    ld = [t.string.strip() for t in soup.find_all("script", type="application/ld+json") if t.string]
    for t in soup(["script", "style", "noscript", "svg", "img", "link"]):
        t.decompose()
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    head = soup.title.string.strip() if soup.title and soup.title.string else ""
    combined = f"PAGE TITLE: {head}\nJSON-LD: {' '.join(ld)[:3000]}\nVISIBLE TEXT: {text}"
    return combined[:MAX_LLM_CHARS]


def extract_with_groq(page: str) -> Product:
    if not GROQ_API_KEY:
        log.warning("GROQ_API_KEY not set; skipping Groq fallback")
        return Product(None, None)

    payload = {
        "model": GROQ_MODEL,
        "temperature": 0,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You extract product data from Flipkart page text. Return the main product's "
                    "title and its current selling price in INR (not MRP, EMI, exchange or "
                    "related-product prices). Respond ONLY with a JSON object of the form "
                    '{"product_title": string, "price": number or null}. '
                    "The price must be numeric only (no currency symbol or commas). "
                    "If the price is not present, use null."
                ),
            },
            {"role": "user", "content": _condense_for_llm(page)},
        ],
        "response_format": {"type": "json_object"},
    }
    try:
        resp = requests.post(
            GROQ_URL,
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json=payload,
            timeout=60,
        )
        resp.raise_for_status()
        data = json.loads(resp.json()["choices"][0]["message"]["content"])
        return Product(data.get("product_title") or None, parse_price(data.get("price")))
    except Exception as exc:  # noqa: BLE001
        log.error("Groq extraction failed: %s", exc)
        return Product(None, None)


def get_product(url: str) -> Product:
    page = fetch_html(url)
    product = scrape_with_bs4(page)
    if product.price is not None:
        log.info("BeautifulSoup OK: %s @ %s", (product.title or "?")[:50], product.price)
        return product

    log.info("BeautifulSoup could not find a price; trying Groq fallback")
    fallback = extract_with_groq(page)
    return Product(fallback.title or product.title, fallback.price)


# ---------------------------------------------------------------- email
def build_email_html(title: str, url: str, current: float, target: float, initial: Optional[float]) -> str:
    saved_vs_target = target - current
    drop_line = ""
    if initial and initial > current:
        drop_line = (
            f'<p style="margin:12px 0 0;color:#475569">Down ₹{initial - current:,.0f} '
            f"from ₹{initial:,.0f} when tracking started.</p>"
        )
    return f"""\
<div style="font-family:Segoe UI,Arial,sans-serif;max-width:560px;margin:auto;border:1px solid #e2e8f0;border-radius:12px;overflow:hidden">
  <div style="background:#2874f0;color:#fff;padding:18px 24px;font-size:18px;font-weight:700">Price drop: your budget is met</div>
  <div style="padding:24px">
    <h2 style="margin:0 0 16px;font-size:18px;color:#0f172a">{html.escape(title)}</h2>
    <table style="width:100%;border-collapse:collapse;font-size:15px">
      <tr><td style="padding:8px 0;color:#64748b">Current price</td>
          <td style="padding:8px 0;text-align:right;font-weight:700;color:#15803d;font-size:22px">₹{current:,.0f}</td></tr>
      <tr><td style="padding:8px 0;color:#64748b">Your budget</td>
          <td style="padding:8px 0;text-align:right">₹{target:,.0f}</td></tr>
      <tr><td style="padding:8px 0;color:#64748b">Under budget by</td>
          <td style="padding:8px 0;text-align:right;font-weight:600">₹{saved_vs_target:,.0f}</td></tr>
    </table>
    {drop_line}
    <a href="{html.escape(url, quote=True)}" style="display:inline-block;margin-top:20px;background:#ffe500;color:#111;
       padding:12px 22px;border-radius:8px;font-weight:700;text-decoration:none">Open on Flipkart</a>
    <p style="margin:20px 0 0;font-size:12px;color:#94a3b8">Open your dashboard and tap the tick to mark this tracker resolved.</p>
  </div>
</div>"""


def send_email(title: str, url: str, current: float, target: float, initial: Optional[float]) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Price drop: {title[:60]} is now ₹{current:,.0f}"
    msg["From"] = EMAIL_USER
    msg["To"] = EMAIL_TO
    msg.attach(MIMEText(f"{title}\nNow ₹{current:,.0f} (budget ₹{target:,.0f})\n{url}", "plain", "utf-8"))
    msg.attach(MIMEText(build_email_html(title, url, current, target, initial), "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
        server.login(EMAIL_USER, EMAIL_PASSWORD)
        server.sendmail(EMAIL_USER, [EMAIL_TO], msg.as_string())
    log.info("Alert email sent for %s", title[:50])


# ---------------------------------------------------------------- main workflow
def process_row(db: Client, row: dict) -> None:
    row_id, url = row["id"], row["product_url"]
    target = float(row["target_price"])
    log.info("Checking %s", url)

    product = get_product(url)
    if product.price is None:
        log.warning("No price found for %s; leaving row untouched", url)
        return

    current = product.price
    title = product.title or row.get("product_title") or "Flipkart product"
    update: dict = {
        "latest_price": current,
        "product_title": title,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if row.get("initial_price") is None:
        update["initial_price"] = current

    # Email only on the ACTIVE -> ALERT_SENT transition, so you aren't emailed daily.
    if current <= target and row["status"] == "ACTIVE":
        try:
            send_email(title, url, current, target, row.get("initial_price") or current)
            update["status"] = "ALERT_SENT"
        except Exception as exc:  # noqa: BLE001
            log.error("Email failed (row stays ACTIVE, will retry tomorrow): %s", exc)

    db.table(TABLE).update(update).eq("id", row_id).execute()


def main() -> int:
    missing = [k for k, v in {"SUPABASE_URL": SUPABASE_URL, "SUPABASE_KEY": SUPABASE_KEY,
                              "EMAIL_USER": EMAIL_USER, "EMAIL_PASSWORD": EMAIL_PASSWORD}.items() if not v]
    if missing:
        log.error("Missing environment variables: %s", ", ".join(missing))
        return 1

    db = create_client(SUPABASE_URL, SUPABASE_KEY)
    rows = db.table(TABLE).select("*").in_("status", ["ACTIVE", "ALERT_SENT"]).execute().data or []
    log.info("Found %d tracker(s)", len(rows))

    failures = 0
    for row in rows:
        try:
            process_row(db, row)
        except Exception as exc:  # noqa: BLE001  (one bad URL must not stop the rest)
            failures += 1
            log.error("Failed for %s: %s", row.get("product_url"), exc)
        time.sleep(random.uniform(2, 5))  # be polite to Flipkart

    log.info("Done. %d ok, %d failed", len(rows) - failures, failures)
    return 0


if __name__ == "__main__":
    sys.exit(main())
