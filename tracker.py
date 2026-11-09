"""
Quick Commerce Price Tracker
- Reads products from products.json
- Fetches each product page and extracts the price
- Sends ONE combined Gmail alert for products at/below target
- Remembers what it already alerted (state.json) so you don't get spammed

Usage:
    python tracker.py               # normal run
    python tracker.py --test-email  # only send a test email
"""

import argparse
import json
import os
import re
import smtplib
import sys
import time
from datetime import datetime, timezone
from email.mime.text import MIMEText

import requests
from bs4 import BeautifulSoup

PRODUCTS_FILE = "products.json"
STATE_FILE = "state.json"

SENDER_EMAIL = os.environ.get("SENDER_EMAIL")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")
RECEIVER_EMAIL = os.environ.get("RECEIVER_EMAIL") or SENDER_EMAIL

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-IN,en;q=0.9",
}


# ---------------------------------------------------------------- price parsing
def parse_price(value):
    """Turn '₹1,750', '1750.00' or 1750 into a float. Takes the FIRST number only."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    match = re.search(r"\d[\d,]*\.?\d*", str(value))
    if not match:
        return None
    try:
        number = float(match.group().replace(",", ""))
    except ValueError:
        return None
    return number if number > 0 else None


def price_from_jsonld(node):
    """Search JSON-LD data for offers -> price."""
    if isinstance(node, list):
        for item in node:
            found = price_from_jsonld(item)
            if found:
                return found
    elif isinstance(node, dict):
        offers = node.get("offers")
        if offers:
            for offer in offers if isinstance(offers, list) else [offers]:
                if isinstance(offer, dict):
                    for key in ("price", "lowPrice"):
                        found = parse_price(offer.get(key))
                        if found:
                            return found
        for value in node.values():
            if isinstance(value, (dict, list)):
                found = price_from_jsonld(value)
                if found:
                    return found
    return None


def extract_price(soup, selector=None):
    """Try several reliable methods. Returns (price, method) or (None, None).
    Deliberately does NOT guess 'first ₹ on the page' (that causes false alerts)."""
    # 1. Custom CSS selector from products.json (most precise)
    if selector:
        element = soup.select_one(selector)
        if element:
            price = parse_price(element.get("content") or element.get_text(" ", strip=True))
            if price:
                return price, f"selector '{selector}'"

    # 2. JSON-LD structured data
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        price = price_from_jsonld(data)
        if price:
            return price, "JSON-LD"

    # 3. Meta tags / microdata
    for attrs in (
        {"property": "product:price:amount"},
        {"property": "og:price:amount"},
        {"itemprop": "price"},
    ):
        tag = soup.find(attrs=attrs)
        if tag:
            price = parse_price(tag.get("content") or tag.get_text(" ", strip=True))
            if price:
                return price, f"meta {list(attrs.values())[0]}"

    return None, None


def fetch_price(product):
    url = product["url"]
    try:
        response = requests.get(url, headers=HEADERS, timeout=20)
    except requests.RequestException as error:
        print(f"  ! Request failed: {error}")
        return None, None

    if response.status_code != 200:
        print(f"  ! HTTP {response.status_code} (site may be blocking or URL is wrong)")
        return None, None

    soup = BeautifulSoup(response.text, "html.parser")
    price, method = extract_price(soup, product.get("selector"))
    if price is None:
        print("  ! No price found in the page HTML (page may need JavaScript/pincode)")
    return price, method


# ---------------------------------------------------------------- email
def send_email(subject, body):
    if not SENDER_EMAIL or not GMAIL_APP_PASSWORD:
        print("ERROR: SENDER_EMAIL / GMAIL_APP_PASSWORD not set.")
        return False

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = SENDER_EMAIL
    msg["To"] = RECEIVER_EMAIL

    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as server:
            server.starttls()
            server.login(SENDER_EMAIL, GMAIL_APP_PASSWORD)
            server.send_message(msg)
        print("Email sent.")
        return True
    except Exception as error:
        print(f"ERROR sending email: {error}")
        return False


def build_alert(deals):
    lines = ["Price drop detected!\n"]
    for deal in deals:
        lines.append(
            f"- {deal['name']} ({deal['site']})\n"
            f"  Now: Rs {deal['price']:g}  |  Target: Rs {deal['target']:g}\n"
            f"  {deal['url']}\n"
        )
    subject = (
        f"Price alert: {deals[0]['name']} is Rs {deals[0]['price']:g}"
        if len(deals) == 1
        else f"Price alert: {len(deals)} products below target"
    )
    return subject, "\n".join(lines)


# ---------------------------------------------------------------- files
def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as error:
        print(f"ERROR: {path} is not valid JSON: {error}")
        sys.exit(1)


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------- main
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--test-email", action="store_true", help="send a test email and exit")
    args = parser.parse_args()

    if args.test_email:
        ok = send_email("Price Tracker test", "Test email from your price tracker. It works!")
        sys.exit(0 if ok else 1)

    products = load_json(PRODUCTS_FILE, [])
    if not products:
        print(f"No products found in {PRODUCTS_FILE}.")
        sys.exit(1)

    state = load_json(STATE_FILE, {})
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    deals, failures = [], 0

    for index, product in enumerate(products):
        name = product.get("name", product["url"])
        target = float(product["target"])
        print(f"Checking: {name} [{product.get('site', '?')}]")

        price, method = fetch_price(product)
        if price is None:
            failures += 1
            continue

        print(f"  Price: Rs {price:g} (via {method}) | Target: Rs {target:g}")
        entry = state.setdefault(product["url"], {})
        entry["name"] = name
        entry["last_price"] = price
        entry["checked_at"] = now

        if price <= target:
            last_alerted = entry.get("last_alerted_price")
            if last_alerted is None or price < last_alerted:
                deals.append({**product, "name": name, "price": price, "target": target})
            else:
                print("  Already alerted at this price. Skipping.")
        else:
            entry.pop("last_alerted_price", None)  # re-arm once price goes back up

        if index < len(products) - 1:
            time.sleep(2)  # be polite between requests

    email_failed = False
    if deals:
        subject, body = build_alert(deals)
        if send_email(subject, body):
            for deal in deals:
                state[deal["url"]]["last_alerted_price"] = deal["price"]
        else:
            email_failed = True

    save_json(STATE_FILE, state)

    print(f"\nDone. Checked {len(products)}, failed {failures}, alerts {len(deals)}.")
    # Exit non-zero so the GitHub run shows RED when something is really broken
    if failures == len(products) or email_failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
