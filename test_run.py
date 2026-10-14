#!/usr/bin/env python3
"""One-shot test: picks 5 trips, scans a FEW dates in each window (not full),
sends a single WhatsApp preview. Does not touch dedup / baseline state.

Run via GitHub Actions: workflow_dispatch → mode: test
"""
import os
import sys
from datetime import date, timedelta
from pathlib import Path

import yaml

from watcher import (
    search_pair,
    notify_whatsapp,
    init_db,
    gflights_url,
    generate_pairs,
    _dow,
)

# Pick 5 trips to test — mix of near-term and far-out.
TEST_TRIP_IDS = [
    "italy_2026_11",
    "czech_hungary_2026_12",
    "iceland_2027_01",
    "spain_2026_10",
    "croatia_2027_09",
]
# Max dates to probe per trip in test mode (keep cheap)
TEST_MAX_DATES_PER_TRIP = 5


def main():
    if "CMB_PHONE" not in os.environ or "CMB_KEY" not in os.environ:
        print("ERROR: set CMB_PHONE and CMB_KEY env vars first.", file=sys.stderr)
        sys.exit(1)

    cfg = yaml.safe_load(Path("trips.yaml").read_text())
    origin = cfg["origin"]
    conn = init_db()
    cap = 999_999  # no safety cap for test

    trips_by_id = {t["id"]: t for t in cfg["trips"]}
    lines = ["✈️ TravelinEurope watcher — test run", ""]

    for tid in TEST_TRIP_IDS:
        trip = trips_by_id.get(tid)
        if not trip:
            lines.append(f"❌ {tid}: not in trips.yaml")
            continue

        dest = trip["destinations"][0]
        ret_from = trip.get("return_from") or dest
        print(f"\n--- {trip['label']} ---")

        # Sample a few evenly spaced dates in the window (not full scan)
        pairs = list(generate_pairs(trip))
        if len(pairs) > TEST_MAX_DATES_PER_TRIP:
            step = len(pairs) // TEST_MAX_DATES_PER_TRIP
            pairs = pairs[::step][:TEST_MAX_DATES_PER_TRIP]

        results = []
        for depart, ret in pairs:
            try:
                price = search_pair(conn, cap, origin, dest, ret_from, depart, ret)
            except Exception as e:
                print(f"  {depart}: error {e}")
                continue
            if price is None:
                print(f"  {depart}: no fares")
                continue
            results.append((depart, ret, price))
            print(f"  {_dow(depart)} {depart}: €{price:.0f}")

        if not results:
            lines.append(f"⚠️ {trip['label']}: no fares returned for any sampled date")
            continue

        results.sort(key=lambda r: r[2])
        best_depart, best_ret, best_price = results[0]
        plan = str(trip["planned_depart"])
        plan_price = next((p for d, _, p in results if d == plan), None)

        line = f"✅ {trip['label']}: cheapest {_dow(best_depart)} {best_depart} €{best_price:.0f}"
        if plan_price:
            delta = (1 - best_price / plan_price) * 100
            line += f" (plan {plan} €{plan_price:.0f}, {delta:+.0f}% vs cheapest)"
        lines.append(line)

    conn.close()

    msg = "\n".join(lines)
    print("\n--- Sending WhatsApp ---")
    print(msg)
    notify_whatsapp(msg)
    print("\n✅ Message sent.")


if __name__ == "__main__":
    main()
