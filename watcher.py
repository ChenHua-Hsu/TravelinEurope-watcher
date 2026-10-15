#!/usr/bin/env python3
"""Flight watcher — two-tier scan over Google Flights via `fli` (SearchDates).

fli's SearchDates returns an entire date range's prices in ONE HTTP call and
natively includes budget carriers (easyJet, Ryanair, etc.) — unlike fast-flights
which required per-date queries and airline filters.

Modes:
  --mode full   : scan each trip's window, cache top-N cheapest, send digest.
  --mode watch  : re-check only the cached top-N dates, alert on drops.
"""
import argparse
import os
import sqlite3
import statistics
import sys
import time
import urllib.parse

# Seconds to wait between consecutive fli SearchDates calls.
# fli hits Google back-to-back aggressively and gets rate-limited after ~1 call.
# 15s empirically works consistently; shorter is inconsistent.
INTER_CALL_SLEEP = 15
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml
from fli.search import SearchDates
from fli.models import (
    DateSearchFilters, FlightSegment, PassengerInfo, Airport, TripType,
)


def _utcnow_iso():
    return datetime.now(timezone.utc).isoformat()


DB_PATH = "fares.db"
TRIPS_PATH = "trips.yaml"


# ─── Config & DB ────────────────────────────────────────────────────────────

def load_config():
    return yaml.safe_load(Path(TRIPS_PATH).read_text())


def init_db():
    conn = sqlite3.connect(DB_PATH)
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
    conn.execute("INSERT INTO api_calls (called_at) VALUES (?)", (_utcnow_iso(),))
    conn.commit()


# ─── Fare fetching via fli SearchDates ──────────────────────────────────────

class BudgetExceeded(Exception):
    pass


def _airport(code):
    """Convert IATA code string to fli Airport enum."""
    return getattr(Airport, code)


def _do_one_search(from_code, to_code, date_from, date_to):
    """Single fli SearchDates call. Returns dict[iso_date → price] or {} on empty."""
    filters = DateSearchFilters(
        trip_type=TripType.ONE_WAY,
        passenger_info=PassengerInfo(adults=1),
        flight_segments=[
            FlightSegment(
                departure_airport=[[_airport(from_code), 0]],
                arrival_airport=[[_airport(to_code), 0]],
                travel_date=date_from,
            ),
        ],
        from_date=date_from,
        to_date=date_to,
    )
    results = SearchDates().search(filters, currency="EUR")
    if not results:
        return {}
    out = {}
    for r in results:
        d = r.date[0] if isinstance(r.date, tuple) else r.date
        if hasattr(d, "date"):
            d = d.date()
        out[d.isoformat()] = float(r.price)
    return out


def search_leg_dates(conn, cap, from_code, to_code, date_from, date_to, retries=2):
    """Return dict[iso_date → price] for a one-way leg across a date range.
    Google rate-limiting is flaky; retry with longer sleep on empty response."""
    if calls_this_month(conn) >= cap:
        raise BudgetExceeded(f"monthly cap {cap} reached")

    for attempt in range(retries + 1):
        log_call(conn)
        try:
            out = _do_one_search(from_code, to_code, date_from, date_to)
        except Exception as e:
            print(f"  fli error {from_code}→{to_code} {date_from}..{date_to} "
                  f"({type(e).__name__}): {e}", file=sys.stderr)
            out = {}
        if out:
            return out
        if attempt < retries:
            wait = INTER_CALL_SLEEP * (attempt + 2)
            print(f"  {from_code}→{to_code} {date_from}..{date_to}: empty, retrying in {wait}s "
                  f"(attempt {attempt + 2}/{retries + 1})")
            time.sleep(wait)
    print(f"  {from_code}→{to_code} {date_from}..{date_to}: gave up after {retries + 1} attempts")
    return {}


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
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    return conn.execute(
        "SELECT 1 FROM alerts_sent WHERE trip_id = ? AND depart_date = ? AND sent_at >= ?",
        (trip_id, depart, cutoff),
    ).fetchone() is not None


def mark_alert(conn, trip_id, depart, price):
    conn.execute(
        "INSERT INTO alerts_sent (trip_id, depart_date, price_eur, sent_at) VALUES (?, ?, ?, ?)",
        (trip_id, depart, price, _utcnow_iso()),
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
        params={"phone": os.environ["CMB_PHONE"], "text": message,
                "apikey": os.environ["CMB_KEY"]},
        timeout=20,
    )
    r.raise_for_status()


# ─── Scan modes ─────────────────────────────────────────────────────────────

def _dow(iso_date):
    return ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][
        date.fromisoformat(iso_date).weekday()
    ]


def _build_pair_prices(trip, origin, out_prices, in_prices):
    """Combine outbound and inbound date-price dicts into (depart, return, total)."""
    duration = int(trip["duration_days"])
    pairs = []
    for depart_iso, out_price in out_prices.items():
        ret_date = date.fromisoformat(depart_iso) + timedelta(days=duration - 1)
        ret_iso = ret_date.isoformat()
        in_price = in_prices.get(ret_iso)
        if in_price is None:
            continue
        pairs.append((depart_iso, ret_iso, out_price + in_price))
    return pairs


def _scan_trip_all_dates(conn, cap, origin, trip):
    """Shared helper: do 2 SearchDates calls (outbound, inbound) → list of
    (depart_iso, return_iso, total_price). Round-trip collapses to same airport."""
    dest = trip["destinations"][0]
    ret_from = trip.get("return_from") or dest
    duration = int(trip["duration_days"])

    out_from = str(trip["window_start"])
    out_to = str(trip["window_end"])
    in_from = (date.fromisoformat(out_from) + timedelta(days=duration - 1)).isoformat()
    in_to = (date.fromisoformat(out_to) + timedelta(days=duration - 1)).isoformat()

    out_prices = search_leg_dates(conn, cap, origin, dest, out_from, out_to)
    time.sleep(INTER_CALL_SLEEP)
    in_prices = search_leg_dates(conn, cap, ret_from, origin, in_from, in_to)
    return _build_pair_prices(trip, origin, out_prices, in_prices)


def full_scan_trip(conn, cap, origin, trip, top_n):
    """Full scan of trip's window — cache top-N, send weekly digest."""
    trip_id = trip["id"]
    dest = trip["destinations"][0]
    plan_depart = str(trip["planned_depart"])

    pairs = _scan_trip_all_dates(conn, cap, origin, trip)
    if not pairs:
        print(f"[{trip_id}] full scan: no fares returned")
        return

    for depart, ret, price in pairs:
        conn.execute(
            "INSERT INTO fares (trip_id, depart_date, return_date, price_eur, scan_mode, seen_at) "
            "VALUES (?, ?, ?, ?, 'full', ?)",
            (trip_id, depart, ret, price, _utcnow_iso()),
        )
    conn.commit()

    pairs.sort(key=lambda r: r[2])
    top = pairs[:top_n]
    conn.execute("DELETE FROM top_dates WHERE trip_id = ?", (trip_id,))
    for depart, ret, price in top:
        conn.execute(
            "INSERT INTO top_dates (trip_id, depart_date, return_date, price_at_fullscan, computed_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (trip_id, depart, ret, price, _utcnow_iso()),
        )
    conn.commit()

    plan_price = next((p for d, _, p in pairs if d == plan_depart), None)
    prices_only = [p for _, _, p in pairs]
    median = statistics.median(prices_only)

    lines = [f"✈️ {trip['label']} — weekly scan"]
    lines.append(f"Window: {trip['window_start']} → {trip['window_end']} ({len(pairs)} dates)")
    lines.append(f"Median €{median:.0f}")
    lines.append("")
    lines.append("Cheapest 5:")
    for depart, ret, price in top[:5]:
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
    if plan_price and not any(d == plan_depart for d, _, _ in top[:5]):
        lines.append("")
        lines.append(f"Your plan: {_dow(plan_depart)} {plan_depart}: €{plan_price:.0f}")
    elif not plan_price:
        lines.append("")
        lines.append(f"Your plan: {_dow(plan_depart)} {plan_depart}: (no fare returned)")
    lines.append("")
    lines.append(f"Book cheapest: {gflights_url(origin, dest, top[0][0], top[0][1])}")

    notify_whatsapp("\n".join(lines))
    print(f"[{trip_id}] full scan done: {len(pairs)} dates, top €{top[0][2]:.0f}, digest sent")


def watch_scan_trip(conn, cap, origin, trip):
    """Re-fetch today's prices for cached top-N dates; alert on drops."""
    trip_id = trip["id"]
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

    # Build fresh pair prices covering just these cached dates.
    # Simplest: redo a focused scan on the min/max of cached dates.
    cached_departs = [r[0] for r in rows]
    out_from = min(cached_departs)
    out_to = max(cached_departs)
    # To match the inbound dates we need a window too
    duration = int(trip["duration_days"])
    in_from = (date.fromisoformat(out_from) + timedelta(days=duration - 1)).isoformat()
    in_to = (date.fromisoformat(out_to) + timedelta(days=duration - 1)).isoformat()

    ret_from_code = trip.get("return_from") or dest
    out_prices = search_leg_dates(conn, cap, origin, dest, out_from, out_to)
    time.sleep(INTER_CALL_SLEEP)
    in_prices = search_leg_dates(conn, cap, ret_from_code, origin, in_from, in_to)
    fresh = {d: p for d, _, p in _build_pair_prices(trip, origin, out_prices, in_prices)}

    drops = []
    for depart, ret, prev_price in rows:
        new_price = fresh.get(depart)
        if new_price is None:
            continue
        conn.execute(
            "INSERT INTO fares (trip_id, depart_date, return_date, price_eur, scan_mode, seen_at) "
            "VALUES (?, ?, ?, ?, 'watch', ?)",
            (trip_id, depart, ret, new_price, _utcnow_iso()),
        )
        drop_pct = (1 - new_price / prev_price) * 100 if prev_price > 0 else 0
        print(f"[{trip_id}] {depart}: €{new_price:.0f} (was €{prev_price:.0f}, {drop_pct:+.0f}%)")
        if drop_pct >= threshold:
            drops.append((depart, ret, prev_price, new_price, drop_pct))
    conn.commit()

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
        (month, level, _utcnow_iso()),
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
        notify_whatsapp(f"🛑 Watcher STOPPED — scrape count {pct:.0f}% ({used}/{cap}) for {month}.")
        mark_budget_alert(conn, month, "stop")
    elif pct >= warn_pct and not budget_alert_sent(conn, month, "warn"):
        notify_whatsapp(f"⚠️ Watcher at {pct:.0f}% of monthly scrape budget ({used}/{cap}) for {month}.")
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

    first = True
    for trip in cfg["trips"]:
        if not in_watch_window(trip, today):
            continue
        if not first:
            time.sleep(INTER_CALL_SLEEP)  # gap between trips to avoid rate-limit
        first = False
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
