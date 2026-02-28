"""
Flight Price Checker  (DIRECT FLIGHTS ONLY)
Beijing (PEK) -> Stockholm (ARN) -> Beijing (PEK)
Departure: 2026-06-16 | Return: 2026-09-12
Notifies when price drops below 8000 SEK
"""

import os
import sys
import time
import json
import logging
import smtplib
import schedule
import requests
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path
from dotenv import load_dotenv
from colorama import init, Fore, Style

init(autoreset=True)

# ── Configuration ────────────────────────────────────────────────────────────

ORIGIN      = "PEK"          # Beijing Capital International Airport
DESTINATION = "ARN"          # Stockholm Arlanda Airport
DEPART_DATE = "2026-06-16"   # Outbound
RETURN_DATE = "2026-09-12"   # Return
ADULTS      = 1
CURRENCY    = "SEK"
PRICE_LIMIT = 8000           # SEK – alert threshold
CHECK_EVERY_HOURS = 6        # How often to check (hours)

HISTORY_FILE = Path(__file__).parent / "price_history.json"
STATUS_FILE  = Path(__file__).parent / "status.json"

# ── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(Path(__file__).parent / "flight_checker.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

# ── Amadeus token cache ───────────────────────────────────────────────────────

_token_cache: dict = {"token": None, "expires_at": 0}


def get_amadeus_token(client_id: str, client_secret: str) -> str:
    """Fetch (or reuse) an Amadeus OAuth2 access token."""
    if time.time() < _token_cache["expires_at"] - 60:
        return _token_cache["token"]

    resp = requests.post(
        "https://test.api.amadeus.com/v1/security/oauth2/token",
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    _token_cache["token"] = data["access_token"]
    _token_cache["expires_at"] = time.time() + data["expires_in"]
    return _token_cache["token"]


# ── Flight search ─────────────────────────────────────────────────────────────

def search_flights(client_id: str, client_secret: str) -> list[dict]:
    """Query Amadeus for round-trip offers and return a simplified list."""
    token = get_amadeus_token(client_id, client_secret)
    headers = {"Authorization": f"Bearer {token}"}

    params = {
        "originLocationCode":      ORIGIN,
        "destinationLocationCode": DESTINATION,
        "departureDate":           DEPART_DATE,
        "returnDate":              RETURN_DATE,
        "adults":                  ADULTS,
        "currencyCode":            CURRENCY,
        "nonStop":                 "true",   # DIRECT FLIGHTS ONLY
        "max":                     10,
    }

    resp = requests.get(
        "https://test.api.amadeus.com/v2/shopping/flight-offers",
        headers=headers,
        params=params,
        timeout=20,
    )
    resp.raise_for_status()
    raw = resp.json()

    offers = []
    for offer in raw.get("data", []):
        price = float(offer["price"]["grandTotal"])
        airlines = list({
            seg["carrierCode"]
            for itin in offer["itineraries"]
            for seg in itin["segments"]
        })
        stops_out = len(offer["itineraries"][0]["segments"]) - 1
        stops_ret = len(offer["itineraries"][1]["segments"]) - 1 if len(offer["itineraries"]) > 1 else 0
        offers.append({
            "price": price,
            "airlines": airlines,
            "stops_outbound": stops_out,
            "stops_return": stops_ret,
        })

    return sorted(offers, key=lambda x: x["price"])


# ── Desktop notification ──────────────────────────────────────────────────────

def desktop_notify(title: str, message: str) -> None:
    try:
        from plyer import notification
        notification.notify(
            title=title,
            message=message,
            app_name="Flight Price Checker",
            timeout=30,
        )
    except Exception as exc:
        log.warning("Desktop notification failed: %s", exc)


# ── Email notification ────────────────────────────────────────────────────────

def email_notify(subject: str, body: str) -> None:
    smtp_email   = os.getenv("SMTP_EMAIL")
    smtp_password = os.getenv("SMTP_PASSWORD")
    notify_email = os.getenv("NOTIFY_EMAIL")

    if not all([smtp_email, smtp_password, notify_email]):
        return  # email not configured, skip silently

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = smtp_email
        msg["To"]      = notify_email
        msg.attach(MIMEText(body, "plain", "utf-8"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as server:
            server.login(smtp_email, smtp_password)
            server.sendmail(smtp_email, notify_email, msg.as_string())
        log.info("Email notification sent to %s", notify_email)
    except Exception as exc:
        log.warning("Email notification failed: %s", exc)


# ── Price history ─────────────────────────────────────────────────────────────

def load_history() -> list:
    if HISTORY_FILE.exists():
        with open(HISTORY_FILE, encoding="utf-8") as f:
            return json.load(f)
    return []


def save_history(entry: dict) -> None:
    history = load_history()
    history.append(entry)
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2, ensure_ascii=False)


# ── Status (latest check result) ──────────────────────────────────────────────

def load_status() -> dict:
    if STATUS_FILE.exists():
        with open(STATUS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_status(data: dict) -> None:
    with open(STATUS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ── Main check ────────────────────────────────────────────────────────────────

def run_check() -> dict | None:
    client_id     = os.getenv("AMADEUS_CLIENT_ID")
    client_secret = os.getenv("AMADEUS_CLIENT_SECRET")

    if not client_id or not client_secret:
        log.error("AMADEUS_CLIENT_ID and AMADEUS_CLIENT_SECRET must be set in .env")
        return None

    log.info("─" * 60)
    log.info("Checking DIRECT flights %s → %s → %s", ORIGIN, DESTINATION, ORIGIN)
    log.info("Outbound: %s  |  Return: %s", DEPART_DATE, RETURN_DATE)

    try:
        offers = search_flights(client_id, client_secret)
    except requests.HTTPError as exc:
        log.error("API error: %s – %s", exc.response.status_code, exc.response.text[:300])
        return None
    except Exception as exc:
        log.error("Unexpected error: %s", exc)
        return None

    if not offers:
        log.warning("No flight offers returned.")
        return None

    cheapest = offers[0]
    price    = cheapest["price"]
    airlines = ", ".join(cheapest["airlines"])
    now_str  = datetime.now().strftime("%Y-%m-%d %H:%M")
    is_deal  = price < PRICE_LIMIT

    # Build result dict
    result = {
        "timestamp":  now_str,
        "price_sek":  price,
        "airlines":   airlines,
        "is_deal":    is_deal,
        "all_offers": offers[:5],
        "error":      None,
    }

    # Save to history and status
    save_history({"timestamp": now_str, "price_sek": price, "airlines": airlines})
    save_status(result)

    # Print results
    print()
    print(f"{Fore.CYAN}{'─'*60}")
    print(f"  {Fore.WHITE}Route   : {ORIGIN} ↔ {DESTINATION}")
    print(f"  {Fore.WHITE}Dates   : {DEPART_DATE}  →  {RETURN_DATE}")
    print(f"  {Fore.WHITE}Checked : {now_str}")
    print(f"{'─'*60}")
    for i, o in enumerate(offers[:5], 1):
        color = Fore.GREEN if o["price"] < PRICE_LIMIT else Fore.YELLOW
        tag   = " ◀ CHEAPEST" if i == 1 else ""
        print(f"  {color}#{i}  {o['price']:>8.0f} SEK  |  "
              f"Airlines: {', '.join(o['airlines'])}  |  "
              f"Direct{tag}")
    print(f"{Fore.CYAN}{'─'*60}{Style.RESET_ALL}")
    print()

    log.info("Cheapest offer: %.0f SEK (airlines: %s)", price, airlines)

    # Threshold alert
    if is_deal:
        msg = (
            f"DIRECT Flight {ORIGIN}↔{DESTINATION} is NOW {price:.0f} SEK!\n"
            f"Threshold: {PRICE_LIMIT} SEK\n"
            f"Airlines: {airlines}\n"
            f"Outbound {DEPART_DATE} | Return {RETURN_DATE}\n"
            f"Non-stop both ways. Book quickly!"
        )
        title = f"✈ Flight Deal! {price:.0f} SEK (< {PRICE_LIMIT} SEK)"
        log.info("🚨 PRICE ALERT: %.0f SEK is below threshold of %d SEK!", price, PRICE_LIMIT)
        print(f"{Fore.GREEN}{'='*60}")
        print(f"  🚨  PRICE ALERT!  {price:.0f} SEK  (limit: {PRICE_LIMIT} SEK)")
        print(f"{'='*60}{Style.RESET_ALL}")
        desktop_notify(title, msg)
        email_notify(title, msg)
    else:
        log.info("Price %.0f SEK is above threshold %d SEK. No alert.", price, PRICE_LIMIT)

    return result


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    load_dotenv(Path(__file__).parent / ".env")

    print(f"{Fore.CYAN}")
    print("╔══════════════════════════════════════════════════════╗")
    print("║      ✈  FLIGHT PRICE CHECKER (DIRECT ONLY)  ✈       ║")
    print(f"║  {ORIGIN} ↔ {DESTINATION}  |  Depart {DEPART_DATE}  Return {RETURN_DATE}  ║")
    print(f"║  Alert threshold: {PRICE_LIMIT} SEK  |  Non-stop only            ║")
    print(f"║  Checking every {CHECK_EVERY_HOURS} hours                            ║")
    print("╚══════════════════════════════════════════════════════╝")
    print(Style.RESET_ALL)

    # Run once immediately
    run_check()

    # Then schedule
    schedule.every(CHECK_EVERY_HOURS).hours.do(run_check)
    log.info("Scheduler started – next check in %d hours. Press Ctrl+C to stop.", CHECK_EVERY_HOURS)

    try:
        while True:
            schedule.run_pending()
            time.sleep(30)
    except KeyboardInterrupt:
        log.info("Stopped by user.")


if __name__ == "__main__":
    main()
