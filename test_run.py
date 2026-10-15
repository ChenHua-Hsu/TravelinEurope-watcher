#!/usr/bin/env python3
"""Test mode: picks 5 trips, runs a mini full_scan on each, sends ONE combined WhatsApp.
Does NOT touch dedup state.
"""
import os
import sys
from pathlib import Path
import yaml

from watcher import (
    _scan_trip_all_dates, init_db, notify_whatsapp, _dow,
)

TEST_TRIP_IDS = [
    "italy_2028_03",
    "czech_hungary_2026_12",
    "iceland_2028_01",
    "spain_2026_10",
    "croatia_2027_09",
    "finland_2027_03",
]


def main():
    if "CMB_PHONE" not in os.environ or "CMB_KEY" not in os.environ:
        print("ERROR: set CMB_PHONE and CMB_KEY env vars first.", file=sys.stderr)
        sys.exit(1)

    cfg = yaml.safe_load(Path("trips.yaml").read_text())
    origin = cfg["origin"]
    conn = init_db()
    cap = 999_999

    trips_by_id = {t["id"]: t for t in cfg["trips"]}
    lines = ["✈️ TravelinEurope watcher — test run", ""]

    for tid in TEST_TRIP_IDS:
        trip = trips_by_id.get(tid)
        if not trip:
            lines.append(f"❌ {tid}: not in trips.yaml")
            continue
        print(f"\n--- {trip['label']} ---")
        try:
            pairs = _scan_trip_all_dates(conn, cap, origin, trip, cfg)
        except Exception as e:
            lines.append(f"❌ {trip['label']}: {e}")
            continue
        if not pairs:
            lines.append(f"⚠️ {trip['label']}: no fares returned")
            continue
        pairs.sort(key=lambda r: r[2])
        best_d, _, best_p = pairs[0]
        plan = str(trip["planned_depart"])
        plan_p = next((p for d, _, p in pairs if d == plan), None)
        line = f"✅ {trip['label']}: cheapest {_dow(best_d)} {best_d} €{best_p:.0f}"
        if plan_p and best_d != plan:
            line += f" (plan €{plan_p:.0f}, save €{plan_p - best_p:.0f})"
        lines.append(line)
        print(f"  → cheapest: {best_d} €{best_p:.0f}")

    conn.close()
    msg = "\n".join(lines)
    print("\n--- Sending WhatsApp ---")
    print(msg)
    notify_whatsapp(msg)
    print("\n✅ Message sent.")


if __name__ == "__main__":
    main()
