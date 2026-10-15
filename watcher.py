#!/usr/bin/env python3
"""Flight watcher — two-tier scan over Google Flights via fast-flights.

Modes:
  --mode full   : scan every day in each trip's window, record all fares,
                  cache the top-N cheapest, and send a weekly digest WhatsApp.
  --mode watch  : scan ONLY the top-N dates cached from the last full scan,
                  alert if any drops below its cached price by threshold.
  --mode test   : run test_run.py's flow (unconditional 5-trip WhatsApp).
"""
import argparse
import os
import random
import sqlite3
import statistics
import sys
import time
import urllib.parse
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
import yaml
from fast_flights import FlightQuery, Passengers, create_query, get_flights
from fast_flights import fetcher as _ff_fetcher
from fast_flights.fetcher import Client as _FFClient, URL as _FF_URL
from fast_flights.querying import Query as _FFQuery
try:
    from fast_flights import FlightsNotFound as _FFNotFound
except ImportError:
    _FFNotFound = type("FlightsNotFound", (Exception,), {})

INTER_CALL_SLEEP = 1.5  # seconds between google requests — avoid IP throttling

# fast-flights only parses the "Best" section of Google's results, which excludes
# budget carriers. Running a second query filtered to these airlines surfaces them.
BUDGET_AIRLINES = ["U2", "FR", "W6", "VY", "V7", "EW", "LS"]
# easyJet, Ryanair, Wizz, Vueling, Volotea, Eurowings, Jet2

# EU GDPR consent-wall bypass: preseeded cookie so European IPs don't get the consent page.
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


# ─── Config & DB ────────────────────────────────────────────────────────────

def load_config():
    return yaml.safe_load(Path(TRIPS_PATH).read_text())


def init_db():
    conn = sqlite3.connect(DB_PATH)
    # Migrate stale v1 schema if present (old columns: origin, dest).
    cur = conn.execute("PRAGMA table_info(fares)")
    existing_cols = {row[1] for row in cur.fetchall()}
    if existing_cols and ("scan_mode" not in existing_cols or "origin" in existing_cols):
        print(f"Migrating stale fares schema (had cols: {sorted(existing_cols)})")
        conn.execute("DROP TABLE fares")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS fares (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trip_id TEXT NOT NULL,
            depart_date TEXT NOT NULL,
            return_date TEXT NOT NULL,
            price_eur REAL NOT NULL,
            scan_mode TEXT NOT NULL,
            seen_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS top_dates (
            trip_id TEXT NOT NULL,
            depart_date TEXT NOT NULL,
            return_date TEXT NOT NULL,
            price_at_fullscan REAL NOT NULL,
            computed_at TEXT NOT NULL,
            PRIMARY KEY (trip_id, depart_date)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS alerts_sent (
            trip_id TEXT NOT NULL,
            depart_date TEXT NOT NULL,
            price_eur REAL NOT NULL,
            sent_at TEXT NOT NULL
        )
    """)
    conn.execute("CREATE TABLE IF NOT EXISTS api_calls (called_at TEXT NOT NULL)")
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
    first = date.today().replace(day=1).isoformat()
    return conn.execute(
        "SELECT COUNT(*) FROM api_calls WHERE called_at >= ?", (first,)
    ).fetchone()[0]


def log_call(conn):
    conn.execute(
        "INSERT INTO api_calls (called_at) VALUES (?)",
        (datetime.utcnow().isoformat(),),
    )
    conn.commit()


# ─── Fare fetching ──────────────────────────────────────────────────────────

class BudgetExceeded(Exception):
    pass


def _parse_price(p):
    if p is None:
        return None
    digits = "".join(c for c in str(p) if c.isdigit() or c == ".")
    try:
        return float(digits) if digits else None
    except ValueError:
        return None


def _query_single(from_a, to_a, dep_date, return_date=None, airlines=None, retries=1):
    """ONE Google Flights query (optionally airline-filtered). Returns min price or None."""
    out_leg = FlightQuery(date=dep_date, from_airport=from_a, to_airport=to_a, airlines=airlines)
    flights = [out_leg]
    trip_type = "one-way"
    if return_date:
        flights.append(FlightQuery(date=return_date, from_airport=to_a, to_airport=from_a, airlines=airlines))
        trip_type = "round-trip"

    attempt = 0
    while attempt <= retries:
        attempt += 1
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
            prices = [_parse_price(getattr(f, "price", None)) for f in items]
            prices = [p for p in prices if p is not None]
            if prices:
                return min(prices)
            return None
        except _FFNotFound:
            return None
        except TypeError:
            # No results for this airline filter — treat as empty, not an error
            return None
        except Exception as e:
            if attempt > retries:
                tag = f" (airlines={airlines})" if airlines else ""
                print(f"  gflights error {from_a}→{to_a} {dep_date}{tag} ({type(e).__name__}): {e}", file=sys.stderr)
                return None
            time.sleep(2 + random.random() * 2)
    return None


def _query_cheapest(from_a, to_a, dep_date, return_date=None):
    """Query default (big carriers) + budget filter, return min across both."""
    results = []
    p1 = _query_single(from_a, to_a, dep_date, return_date, airlines=None)
    if p1 is not None:
        results.append(p1)
    time.sleep(INTER_CALL_SLEEP)
    p2 = _query_single(from_a, to_a, dep_date, return_date, airlines=BUDGET_AIRLINES)
    if p2 is not None:
        results.append(p2)
    return min(results) if results else None


def search_pair(conn, cap, origin, dest, ret_from, depart, return_date):
    """Fetch cheapest total EUR fare for one (depart, return) pair.
    Each _query_cheapest = 2 underlying HTTP calls (default + budget filter).
    Round-trip = 2 real calls, open-jaw = 4."""
    if calls_this_month(conn) >= cap:
        raise BudgetExceeded(f"monthly cap {cap} reached")

    if ret_from == dest:
        log_call(conn); log_call(conn)
        price = _query_cheapest(origin, dest, depart, return_date)
        time.sleep(INTER_CALL_SLEEP)
        return price

    # Open-jaw: two one-ways summed
    log_call(conn); log_call(conn)
    outb = _query_cheapest(origin, dest, depart)
    time.sleep(INTER_CALL_SLEEP)
    if outb is None:
        return None
    if calls_this_month(conn) >= cap:
        raise BudgetExceeded(f"monthly cap {cap} reached mid-pair")
    log_call(conn); log_call(conn)
    inb = _query_cheapest(ret_from, origin, return_date)
    time.sleep(INTER_CALL_SLEEP)
    if inb is None:
        return None
    return outb + inb


# ─── Date generation ────────────────────────────────────────────────────────

def generate_pairs(trip):
    """Yield all (depart, return) ISO-date strings covering trip's window."""
    start = date.fromisoformat(str(trip["window_start"]))
    end = date.fromisoformat(str(trip["window_end"]))
    duration = int(trip["duration_days"])
    d = start
    while d <= end:
        ret = d + timedelta(days=duration - 1)
        yield d.isoformat(), ret.isoformat()
        d += timedelta(days=1)


def in_watch_window(trip, today):
    plan = date.fromisoformat(str(trip["planned_depart"]))
    days_out = (plan - today).days
    low, high = trip["watch_days_before"]
    return low <= days_out <= high


# ─── Dedup ──────────────────────────────────────────────────────────────────

def alerted_recently(conn, trip_id, depart, hours=24):
    cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
    return conn.execute(
        "SELECT 1 FROM alerts_sent WHERE trip_id = ? AND depart_date = ? AND sent_at >= ?",
        (trip_id, depart, cutoff),
    ).fetchone() is not None


def mark_alert(conn, trip_id, depart, price):
    conn.execute(
        "INSERT INTO alerts_sent (trip_id, depart_date, price_eur, sent_at) VALUES (?, ?, ?, ?)",
        (trip_id, depart, price, datetime.utcnow().isoformat()),
    )
    conn.commit()


# ─── Notify ─────────────────────────────────────────────────────────────────

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


# ─── Scan modes ─────────────────────────────────────────────────────────────

def _dow(iso_date):
    return ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][
        date.fromisoformat(iso_date).weekday()
    ]


def full_scan_trip(conn, cap, origin, trip, top_n):
    """Scan every (depart, return) pair in trip's window. Cache top-N, send digest."""
    trip_id = trip["id"]
    ret_from = trip.get("return_from") or trip["destinations"][0]
    dest = trip["destinations"][0]
    plan_depart = str(trip["planned_depart"])

    results = []
    for depart, ret in generate_pairs(trip):
        price = search_pair(conn, cap, origin, dest, ret_from, depart, ret)
        if price is None:
            continue
        results.append((depart, ret, price))
        conn.execute(
            "INSERT INTO fares (trip_id, depart_date, return_date, price_eur, scan_mode, seen_at) "
            "VALUES (?, ?, ?, ?, 'full', ?)",
            (trip_id, depart, ret, price, datetime.utcnow().isoformat()),
        )
        conn.commit()

    if not results:
        print(f"[{trip_id}] full scan: no fares returned")
        return

    # Compute top-N cheapest, rewrite top_dates cache
    results.sort(key=lambda r: r[2])
    top = results[:top_n]
    conn.execute("DELETE FROM top_dates WHERE trip_id = ?", (trip_id,))
    now = datetime.utcnow().isoformat()
    for depart, ret, price in top:
        conn.execute(
            "INSERT INTO top_dates (trip_id, depart_date, return_date, price_at_fullscan, computed_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (trip_id, depart, ret, price, now),
        )
    conn.commit()

    # Plan-date price for comparison
    plan_price = next((p for d, _, p in results if d == plan_depart), None)
    prices_only = [p for _, _, p in results]
    median = statistics.median(prices_only)

    lines = [f"✈️ {trip['label']} — weekly scan"]
    lines.append(f"Window: {trip['window_start']} → {trip['window_end']} ({len(results)} dates)")
    lines.append(f"Median €{median:.0f}")
    lines.append("")
    lines.append("Cheapest 5:")
    top5 = top[:5]
    for depart, ret, price in top5:
        tag = " 📌plan" if depart == plan_depart else ""
        if plan_price and depart != plan_depart:
            delta = price - plan_price
            if delta < 0:
                vs_plan = f" (save €{-delta:.0f})"
            else:
                vs_plan = f" (+€{delta:.0f})"
        else:
            vs_plan = ""
        lines.append(f"  {_dow(depart)} {depart}: €{price:.0f}{tag}{vs_plan}")
    # Always include plan date — show it below the top 5 if not already there
    if plan_price and not any(d == plan_depart for d, _, _ in top5):
        lines.append("")
        lines.append(f"Your plan: {_dow(plan_depart)} {plan_depart}: €{plan_price:.0f}")
    elif not plan_price:
        lines.append("")
        lines.append(f"Your plan: {_dow(plan_depart)} {plan_depart}: (no fare returned)")
    lines.append("")
    lines.append(f"Book cheapest: {gflights_url(origin, dest, top[0][0], top[0][1])}")

    notify_whatsapp("\n".join(lines))
    print(f"[{trip_id}] full scan done: {len(results)} dates, top €{top[0][2]:.0f}, digest sent")


def watch_scan_trip(conn, cap, origin, trip):
    """Re-check only the top-N dates from latest full scan. Alert on drop."""
    trip_id = trip["id"]
    ret_from = trip.get("return_from") or trip["destinations"][0]
    dest = trip["destinations"][0]
    threshold = trip.get("baseline_discount_pct", 20)

    rows = conn.execute(
        "SELECT depart_date, return_date, price_at_fullscan FROM top_dates "
        "WHERE trip_id = ? ORDER BY price_at_fullscan ASC",
        (trip_id,),
    ).fetchall()

    if not rows:
        print(f"[{trip_id}] watch: no cached top_dates yet (needs a full scan first)")
        return

    drops = []
    for depart, ret, prev_price in rows:
        new_price = search_pair(conn, cap, origin, dest, ret_from, depart, ret)
        if new_price is None:
            continue
        conn.execute(
            "INSERT INTO fares (trip_id, depart_date, return_date, price_eur, scan_mode, seen_at) "
            "VALUES (?, ?, ?, ?, 'watch', ?)",
            (trip_id, depart, ret, new_price, datetime.utcnow().isoformat()),
        )
        conn.commit()
        drop_pct = (1 - new_price / prev_price) * 100 if prev_price > 0 else 0
        print(f"[{trip_id}] {depart}: €{new_price:.0f} (was €{prev_price:.0f}, {drop_pct:+.0f}%)")
        if drop_pct >= threshold:
            drops.append((depart, ret, prev_price, new_price, drop_pct))

    for depart, ret, prev, new, pct in drops:
        if alerted_recently(conn, trip_id, depart):
            print(f"[{trip_id}] drop on {depart} already alerted recently")
            continue
        msg = (
            f"🔥 DEAL — {trip['label']}\n"
            f"{_dow(depart)} {depart} → {_dow(ret)} {ret}\n"
            f"€{new:.0f} (was €{prev:.0f}, −{pct:.0f}%)\n"
            f"Book: {gflights_url(origin, dest, depart, ret)}"
        )
        notify_whatsapp(msg)
        mark_alert(conn, trip_id, depart, new)
        print(f"[{trip_id}] ✅ alert sent for {depart}")


# ─── Budget ─────────────────────────────────────────────────────────────────

def budget_alert_sent(conn, month, level):
    return conn.execute(
        "SELECT 1 FROM budget_alerts WHERE month = ? AND level = ?", (month, level)
    ).fetchone() is not None


def mark_budget_alert(conn, month, level):
    conn.execute(
        "INSERT OR REPLACE INTO budget_alerts (month, level, sent_at) VALUES (?, ?, ?)",
        (month, level, datetime.utcnow().isoformat()),
    )
    conn.commit()


def check_budget(conn, cfg):
    cap = cfg.get("monthly_request_cap", 10000)
    warn_pct = cfg.get("warn_at_pct", 80)
    stop_pct = cfg.get("stop_at_pct", 90)
    used = calls_this_month(conn)
    pct = used / cap * 100 if cap else 0
    month = date.today().strftime("%Y-%m")
    print(f"Budget: {used}/{cap} ({pct:.0f}%) this month")

    if pct >= stop_pct and not budget_alert_sent(conn, month, "stop"):
        notify_whatsapp(
            f"🛑 Watcher STOPPED — scrape count {pct:.0f}% ({used}/{cap}) for {month}."
        )
        mark_budget_alert(conn, month, "stop")
    elif pct >= warn_pct and not budget_alert_sent(conn, month, "warn"):
        notify_whatsapp(
            f"⚠️ Watcher at {pct:.0f}% of monthly scrape budget ({used}/{cap}) for {month}."
        )
        mark_budget_alert(conn, month, "warn")

    return cap if pct < stop_pct else 0


# ─── Main ───────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["full", "watch"], default="watch")
    args = parser.parse_args()

    cfg = load_config()
    conn = init_db()
    today = date.today()
    top_n = cfg.get("default_top_n_watch", 5)
    print(f"=== Watcher run {today} mode={args.mode} ===")

    cap = check_budget(conn, cfg)
    if cap == 0:
        print("Hard-stop — skipping all trips.")
        conn.close()
        return

    for trip in cfg["trips"]:
        if not in_watch_window(trip, today):
            continue
        try:
            if args.mode == "full":
                full_scan_trip(conn, cap, cfg["origin"], trip, top_n)
            else:
                watch_scan_trip(conn, cap, cfg["origin"], trip)
        except BudgetExceeded as e:
            print(f"BUDGET: {e} — stopping.")
            break
        except Exception as e:
            print(f"[{trip['id']}] ERROR: {e}", file=sys.stderr)

    conn.close()


if __name__ == "__main__":
    main()
