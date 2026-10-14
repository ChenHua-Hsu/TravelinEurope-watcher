#!/usr/bin/env python3
"""One-shot test: fetches fares for 5 trips (whether in watch window or not)
and sends a single WhatsApp summary message. Does NOT touch the baseline logic
or deduplication tables — safe to run as many times as you want.

Run:
    export CMB_PHONE=+33...     # your WhatsApp number with +
    export CMB_KEY=1663364      # your CallMeBot key
    python test_run.py
"""
import os
import sys
import urllib.parse

import yaml
from pathlib import Path

from watcher import search_fare, notify_whatsapp, init_db, gflights_url

# Pick 5 trips to test — mix of near-term and far-out, mix of locked and flexible.
TEST_TRIP_IDS = [
    "italy_2026_11",            # ~36 days out, in watch window
    "czech_hungary_2026_12",    # ~69 days out, in watch window
    "iceland_2027_01",          # ~90 days out, long-haul
    "spain_2026_10",            # 8 days out — likely spike zone, interesting data point
    "croatia_2027_09",          # ~340 days out — way outside window, pushing fast-flights
]


def main():
    if "CMB_PHONE" not in os.environ or "CMB_KEY" not in os.environ:
        print("ERROR: set CMB_PHONE and CMB_KEY env vars first.", file=sys.stderr)
        sys.exit(1)

    cfg = yaml.safe_load(Path("trips.yaml").read_text())
    origin = cfg["origin"]
    conn = init_db()
    # Effectively no cap for the test (still logs the calls)
    cap = 999_999

    trips_by_id = {t["id"]: t for t in cfg["trips"]}
    lines = ["✈️ TravelinEurope watcher — test run", ""]

    for tid in TEST_TRIP_IDS:
        trip = trips_by_id.get(tid)
        if not trip:
            lines.append(f"❌ {tid}: not found in trips.yaml")
            continue

        depart = str(trip["depart_window"][0])
        ret = str(trip["return_window"][0]) if trip.get("return_window") else None
        dest = trip["destinations"][0]
        ret_from = trip.get("return_from") or dest

        print(f"\n--- {trip['label']} ({depart} → {ret}) ---")
        try:
            if ret_from == dest:
                price = search_fare(conn, cap, origin, dest, depart, ret)
                route = f"{origin}↔{dest}"
            else:
                outb = search_fare(conn, cap, origin, dest, depart)
                inb = search_fare(conn, cap, ret_from, origin, ret) if ret else None
                price = (outb + inb) if (outb and inb) else None
                route = f"{origin}→{dest}/{ret_from}→{origin}"
        except Exception as e:
            print(f"  exception: {e}", file=sys.stderr)
            lines.append(f"❌ {trip['label']}: {e}")
            continue

        if price is None:
            lines.append(f"⚠️ {trip['label']} ({route}): no fares returned")
            print("  no fares")
        else:
            lines.append(f"✅ {trip['label']} ({route} {depart}): €{price:.0f}")
            print(f"  €{price:.0f}")

    conn.close()

    msg = "\n".join(lines)
    print("\n--- Sending WhatsApp ---")
    print(msg)
    notify_whatsapp(msg)
    print("\n✅ Message sent. Check WhatsApp.")


if __name__ == "__main__":
    main()
