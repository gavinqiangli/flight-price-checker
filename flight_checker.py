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
import subprocess
import schedule
import requests
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path
from dotenv import load_dotenv
from colorama import init, Fore, Style

init(autoreset=True)

# ── Configuration ─────────────────────────────────────────────────────────────

ORIGIN      = "PEK"          # Beijing Capital International Airport
DESTINATION = "ARN"          # Stockholm Arlanda Airport
DEPART_DATE = "2026-06-16"   # Outbound
RETURN_DATE = "2026-09-12"   # Return leg
ADULTS      = 1
CURRENCY    = "SEK"
PRICE_LIMIT = 8000           # SEK – alert threshold
CHECK_EVERY_HOURS = 6        # How often to check

HISTORY_FILE = Path(__file__).parent / "price_history.json"
STATUS_FILE  = Path(__file__).parent / "status.json"
POWERSHELL   = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"

# ── Logging ───────────────────────────────────────────────────────────────────

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
    """Query Amadeus for direct round-trip offers, return simplified list."""
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
        price    = float(offer["price"]["grandTotal"])
        airlines = list({
            seg["carrierCode"]
            for itin in offer["itineraries"]
            for seg in itin["segments"]
        })
        offers.append({"price": price, "airlines": airlines})

    return sorted(offers, key=lambda x: x["price"])


# ── Notifications ─────────────────────────────────────────────────────────────

def _notify_send(title: str, message: str) -> bool:
    """Try notify-send (works with WSLg / X11 display)."""
    try:
        env = os.environ.copy()
        env.setdefault("DISPLAY", ":0")
        env.setdefault("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/1000/bus")
        r = subprocess.run(
            ["notify-send", "-u", "critical", "-t", "30000", title, message],
            env=env, capture_output=True, timeout=5,
        )
        return r.returncode == 0
    except Exception:
        return False


def _notify_powershell(title: str, message: str) -> bool:
    """Windows Toast notification via PowerShell (reliable in WSL2)."""
    if not os.path.exists(POWERSHELL):
        return False
    t = title.replace("'", "''")
    m = message.replace("'", "''").replace("\n", " | ")
    ps = (
        "[Windows.UI.Notifications.ToastNotificationManager, "
        "Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null; "
        "$xml = [Windows.UI.Notifications.ToastNotificationManager]::"
        "GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02); "
        f"$xml.GetElementsByTagName('text')[0].InnerText = '{t}'; "
        f"$xml.GetElementsByTagName('text')[1].InnerText = '{m}'; "
        "$toast = [Windows.UI.Notifications.ToastNotification]::new($xml); "
        "[Windows.UI.Notifications.ToastNotificationManager]::"
        "CreateToastNotifier('Flight Price Checker').Show($toast)"
    )
    try:
        subprocess.run(
            [POWERSHELL, "-NonInteractive", "-Command", ps],
            capture_output=True, timeout=10,
        )
        return True
    except Exception:
        return False


def desktop_notify(title: str, message: str) -> None:
    """Send desktop notification – try WSLg first, fall back to PowerShell."""
    if not _notify_send(title, message):
        if not _notify_powershell(title, message):
            log.warning("All notification methods failed.")


# ── Email notification ────────────────────────────────────────────────────────

def email_notify(subject: str, body: str) -> None:
    smtp_email    = os.getenv("SMTP_EMAIL")
    smtp_password = os.getenv("SMTP_PASSWORD")
    notify_email  = os.getenv("NOTIFY_EMAIL")

    if not all([smtp_email, smtp_password, notify_email]):
        return

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


# ── Price history & status ────────────────────────────────────────────────────

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


def save_status(data: dict) -> None:
    with open(STATUS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def load_status() -> dict:
    if STATUS_FILE.exists():
        with open(STATUS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


# ── Main check ────────────────────────────────────────────────────────────────

def run_check() -> dict | None:
    """Run a price check. Returns result dict (usable by web app) or None."""
    client_id     = os.getenv("AMADEUS_CLIENT_ID")
    client_secret = os.getenv("AMADEUS_CLIENT_SECRET")

    if not client_id or not client_secret:
        log.error("AMADEUS_CLIENT_ID and AMADEUS_CLIENT_SECRET must be set in .env")
        return None

    log.info("-" * 60)
    log.info("Checking DIRECT flights %s -> %s -> %s", ORIGIN, DESTINATION, ORIGIN)
    log.info("Outbound: %s  |  Return: %s", DEPART_DATE, RETURN_DATE)

    try:
        offers = search_flights(client_id, client_secret)
    except requests.HTTPError as exc:
        err = f"API error: {exc.response.status_code}"
        log.error("%s - %s", err, exc.response.text[:300])
        save_status({"error": err, "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M")})
        return None
    except Exception as exc:
        log.error("Unexpected error: %s", exc)
        save_status({"error": str(exc), "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M")})
        return None

    if not offers:
        log.warning("No direct flight offers returned for these dates.")
        save_status({"error": "No direct flights found", "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M")})
        return None

    cheapest = offers[0]
    price    = cheapest["price"]
    airlines = ", ".join(cheapest["airlines"])
    now_str  = datetime.now().strftime("%Y-%m-%d %H:%M")
    is_deal  = price < PRICE_LIMIT

    history_entry = {"timestamp": now_str, "price_sek": price, "airlines": airlines}
    save_history(history_entry)

    result = {
        "timestamp":    now_str,
        "price_sek":    price,
        "airlines":     airlines,
        "is_deal":      is_deal,
        "all_offers":   offers[:5],
        "error":        None,
    }
    save_status(result)

    # Terminal output
    print()
    print(f"{Fore.CYAN}{'-'*60}")
    print(f"  {Fore.WHITE}Route   : {ORIGIN} <-> {DESTINATION}  (direct only)")
    print(f"  {Fore.WHITE}Dates   : {DEPART_DATE}  ->  {RETURN_DATE}")
    print(f"  {Fore.WHITE}Checked : {now_str}")
    print(f"{'-'*60}")
    for i, o in enumerate(offers[:5], 1):
        color = Fore.GREEN if o["price"] < PRICE_LIMIT else Fore.YELLOW
        tag   = " << CHEAPEST" if i == 1 else ""
        print(f"  {color}#{i}  {o['price']:>8.0f} SEK  |  "
              f"Airlines: {', '.join(o['airlines'])}  |  Non-stop{tag}")
    print(f"{Fore.CYAN}{'-'*60}{Style.RESET_ALL}")
    print()

    log.info("Cheapest direct offer: %.0f SEK (airlines: %s)", price, airlines)

    if is_deal:
        alert_title = f"Flight Deal! {price:.0f} SEK (< {PRICE_LIMIT} SEK)"
        alert_body  = (
            f"DIRECT Flight {ORIGIN} <-> {DESTINATION}\n"
            f"Price: {price:.0f} SEK  (threshold: {PRICE_LIMIT} SEK)\n"
            f"Airlines: {airlines}\n"
            f"Outbound: {DEPART_DATE} | Return: {RETURN_DATE}\n"
            f"Non-stop both ways. Book now!"
        )
        log.info("PRICE ALERT: %.0f SEK is below threshold of %d SEK!", price, PRICE_LIMIT)
        print(f"{Fore.GREEN}{'='*60}")
        print(f"  *** PRICE ALERT!  {price:.0f} SEK  (limit: {PRICE_LIMIT} SEK) ***")
        print(f"{'='*60}{Style.RESET_ALL}")
        desktop_notify(alert_title, alert_body)
        email_notify(alert_title, alert_body)
    else:
        log.info("Price %.0f SEK is above threshold %d SEK - no alert.", price, PRICE_LIMIT)

    return result


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    load_dotenv(Path(__file__).parent / ".env")

    print(f"{Fore.CYAN}")
    print("=" * 56)
    print("    FLIGHT PRICE CHECKER  (DIRECT / NON-STOP ONLY)")
    print(f"    {ORIGIN} <-> {DESTINATION}  |  Out {DEPART_DATE}  Ret {RETURN_DATE}")
    print(f"    Alert below: {PRICE_LIMIT} SEK  |  Check every {CHECK_EVERY_HOURS}h")
    print("=" * 56)
    print(Style.RESET_ALL)

    run_check()

    schedule.every(CHECK_EVERY_HOURS).hours.do(run_check)
    log.info("Scheduler running - next check in %d hours. Ctrl+C to stop.", CHECK_EVERY_HOURS)

    try:
        while True:
            schedule.run_pending()
            time.sleep(30)
    except KeyboardInterrupt:
        log.info("Stopped by user.")


if __name__ == "__main__":
    main()
