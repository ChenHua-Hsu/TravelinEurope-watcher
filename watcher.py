#!/usr/bin/env python3
"""Flight watcher — scans Google Flights via fast-flights for deals on trips.yaml,
alerts via WhatsApp when a fare drops below the rolling 30-day median."""
import os
import sqlite3
import statistics
import sys
import urllib.parse
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
import yaml
from fast_flights import FlightQuery, Passengers, create_query, get_flights
from fast_flights import fetcher as _ff_fetcher
from fast_flights.fetcher import Client as _FFClient, URL as _FF_URL, parse as _ff_parse
from fast_flights.querying import Query as _FFQuery

# Bypass Google's EU GDPR consent wall by preseeding the CONSENT cookie.
# Without this, European-IP requests get a consent page instead of the flights page.
_CONSENT_COOKIES = {"CONSENT": "YES+cb", "SOCS": "CAISHAgBEhJnd3NfMjAyNDAxMDQtMF9SQzIaAmVuIAEaBgiA_LyvBg"}


def _patched_fetch_html(q, *, proxy=None, fetch_integration=None):
    if fetch_integration is not None:
        return fetch_integration.fetch_html(q)
    client = _FFClient(
        impersonate="chrome_145",
        impersonate_os="macos",
        referer=True,
        proxy=proxy,
        cookie_store=True,
        cookies=_CONSENT_COOKIES,
    )
    params = q.params() if isinstance(q, _FFQuery) else {"q": q}
    return client.get(_FF_URL, params=params).text


_ff_fetcher.fetch_flights_html = _patched_fetch_html

DB_PATH = "fares.db"
TRIPS_PATH = "trips.yaml"


def load_config():
    return yaml.safe_load(Path(TRIPS_PATH).read_text())


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS fares (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trip_id TEXT NOT NULL,
            origin TEXT NOT NULL,
            dest TEXT NOT NULL,
            depart_date TEXT NOT NULL,
            return_date TEXT,
            price_eur REAL NOT NULL,
            seen_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS alerts_sent (
            trip_id TEXT NOT NULL,
            price_eur REAL NOT NULL,
            sent_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS api_calls (
            called_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS budget_alerts (
            month TEXT NOT NULL,
            level TEXT NOT NULL,
            sent_at TEXT NOT NULL,
            PRIMARY KEY (month, level)
        )
    """)
    conn.commit()
    return conn


def calls_this_month(conn):
    first_of_month = date.today().replace(day=1).isoformat()
    cur = conn.execute(
        "SELECT COUNT(*) FROM api_calls WHERE called_at >= ?", (first_of_month,)
    )
    return cur.fetchone()[0]


def log_call(conn):
    conn.execute(
        "INSERT INTO api_calls (called_at) VALUES (?)",
        (datetime.utcnow().isoformat(),),
    )
    conn.commit()


def budget_alert_sent(conn, month, level):
    cur = conn.execute(
        "SELECT 1 FROM budget_alerts WHERE month = ? AND level = ?", (month, level)
    )
    return cur.fetchone() is not None


def mark_budget_alert(conn, month, level):
    conn.execute(
        "INSERT OR REPLACE INTO budget_alerts (month, level, sent_at) VALUES (?, ?, ?)",
        (month, level, datetime.utcnow().isoformat()),
    )
    conn.commit()


class BudgetExceeded(Exception):
    pass


def _parse_price(price_str):
    """fast-flights returns prices like '€123' or '$140' — pull the number."""
    if price_str is None:
        return None
    digits = "".join(c for c in str(price_str) if c.isdigit() or c == ".")
    try:
        return float(digits) if digits else None
    except ValueError:
        return None


def search_fare(conn, cap, origin, dest, depart_date, return_date=None):
    """Return cheapest total EUR price for a route, or None. Scrapes Google Flights."""
    if calls_this_month(conn) >= cap:
        raise BudgetExceeded(f"monthly cap {cap} reached — refusing to call API")
    log_call(conn)

    flights = [FlightQuery(date=depart_date, from_airport=origin, to_airport=dest)]
    if return_date:
        flights.append(
            FlightQuery(date=return_date, from_airport=dest, to_airport=origin)
        )
        trip_type = "round-trip"
    else:
        trip_type = "one-way"

    try:
        q = create_query(
            flights=flights,
            trip=trip_type,
            seat="economy",
            passengers=Passengers(adults=1),
            currency="EUR",
        )
        result = get_flights(q)
        items = getattr(result, "flights", None) or list(result or [])
        if not items:
            return None
        prices = [_parse_price(getattr(f, "price", None)) for f in items]
        prices = [p for p in prices if p is not None]
        return min(prices) if prices else None
    except Exception as e:
        print(f"  gflights error {origin}->{dest} {depart_date}: {e}", file=sys.stderr)
        return None


def rolling_baseline(conn, trip_id, days=30):
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    cur = conn.execute(
        "SELECT price_eur FROM fares WHERE trip_id = ? AND seen_at >= ?",
        (trip_id, cutoff),
    )
    prices = [r[0] for r in cur.fetchall()]
    return statistics.median(prices) if len(prices) >= 3 else None


def alerted_recently(conn, trip_id, hours=24):
    cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
    cur = conn.execute(
        "SELECT 1 FROM alerts_sent WHERE trip_id = ? AND sent_at >= ?",
        (trip_id, cutoff),
    )
    return cur.fetchone() is not None


def gflights_url(origin, dest, depart, return_date=None):
    q = f"Flights from {origin} to {dest} on {depart}"
    if return_date:
        q += f" through {return_date}"
    return "https://www.google.com/travel/flights?q=" + urllib.parse.quote(q)


def notify_whatsapp(message):
    r = requests.get(
        "https://api.callmebot.com/whatsapp.php",
        params={
            "phone": os.environ["CMB_PHONE"],
            "text": message,
            "apikey": os.environ["CMB_KEY"],
        },
        timeout=20,
    )
    r.raise_for_status()


def in_watch_window(trip, today):
    depart = date.fromisoformat(str(trip["depart_window"][0]))
    days_out = (depart - today).days
    low, high = trip["watch_days_before"]
    return low <= days_out <= high


def process_trip(conn, cap, origin, trip, today):
    if not in_watch_window(trip, today):
        return
    depart = str(trip["depart_window"][0])
    ret = str(trip["return_window"][0]) if trip.get("return_window") else None
    dest = trip["destinations"][0]
    ret_from = trip.get("return_from") or dest

    if ret_from == dest:
        price = search_fare(conn, cap, origin, dest, depart, ret)
        route = f"{origin}↔{dest}"
    else:
        outb = search_fare(conn, cap, origin, dest, depart)
        inb = search_fare(conn, cap, ret_from, origin, ret) if ret else None
        price = (outb + inb) if (outb and inb) else None
        route = f"{origin}→{dest} / {ret_from}→{origin}"

    if price is None:
        print(f"[{trip['id']}] no fares returned")
        return

    now = datetime.utcnow().isoformat()
    conn.execute(
        "INSERT INTO fares (trip_id, origin, dest, depart_date, return_date, price_eur, seen_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (trip["id"], origin, dest, depart, ret, price, now),
    )
    conn.commit()

    base = rolling_baseline(conn, trip["id"])
    threshold = trip.get("baseline_discount_pct", 20)
    if base:
        print(f"[{trip['id']}] €{price:.0f} vs baseline €{base:.0f} ({(1-price/base)*100:+.0f}%)")
    else:
        print(f"[{trip['id']}] €{price:.0f} (no baseline yet)")

    if base and price <= base * (1 - threshold / 100):
        if alerted_recently(conn, trip["id"]):
            print("  (alert sent in last 24h — skipping)")
            return
        url = gflights_url(origin, dest, depart, ret)
        msg = (
            f"✈️ DEAL — {trip['label']}\n"
            f"{route} {depart}" + (f" → {ret}" if ret else "") + "\n"
            f"Now: €{price:.0f} (baseline €{base:.0f}, -{(1-price/base)*100:.0f}%)\n"
            f"Book: {url}"
        )
        notify_whatsapp(msg)
        conn.execute(
            "INSERT INTO alerts_sent (trip_id, price_eur, sent_at) VALUES (?, ?, ?)",
            (trip["id"], price, now),
        )
        conn.commit()
        print("  ✅ ALERT SENT")


def check_budget(conn, cfg):
    cap = cfg.get("monthly_request_cap", 1800)
    warn_pct = cfg.get("warn_at_pct", 80)
    stop_pct = cfg.get("stop_at_pct", 90)
    used = calls_this_month(conn)
    pct = used / cap * 100 if cap else 0
    month = date.today().strftime("%Y-%m")
    print(f"Budget: {used}/{cap} ({pct:.0f}%) this month")

    if pct >= stop_pct and not budget_alert_sent(conn, month, "stop"):
        notify_whatsapp(
            f"🛑 Flight watcher STOPPED — call budget {pct:.0f}% ({used}/{cap}) "
            f"for {month}. No more lookups until next month."
        )
        mark_budget_alert(conn, month, "stop")
    elif pct >= warn_pct and not budget_alert_sent(conn, month, "warn"):
        notify_whatsapp(
            f"⚠️ Flight watcher at {pct:.0f}% of monthly call budget "
            f"({used}/{cap}) for {month}. Will auto-stop at {stop_pct}%."
        )
        mark_budget_alert(conn, month, "warn")

    return cap if pct < stop_pct else 0


def main():
    cfg = load_config()
    conn = init_db()
    today = date.today()
    print(f"=== Flight watcher run {today} ===")

    effective_cap = check_budget(conn, cfg)
    if effective_cap == 0:
        print("Hard-stop in effect — skipping all trip checks this run.")
        conn.close()
        return

    for trip in cfg["trips"]:
        try:
            process_trip(conn, effective_cap, cfg["origin"], trip, today)
        except BudgetExceeded as e:
            print(f"BUDGET: {e} — stopping run.")
            break
        except Exception as e:
            print(f"[{trip['id']}] ERROR: {e}", file=sys.stderr)
    conn.close()


if __name__ == "__main__":
    main()
