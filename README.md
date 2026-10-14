# TravelinEurope — Flight Watcher

Automated fare monitor for the TravelinEurope 2-year trip plan.
Runs every 6h on GitHub Actions, scrapes Google Flights via `fast-flights`, alerts via WhatsApp when a fare drops ≥X% below the rolling 30-day median. **No API keys, no signup, no payment.**

## How it works

1. `trips.yaml` defines each trip (route, dates, watch window, discount threshold).
2. `watcher.py` runs on schedule:
   - Skips any trip outside its `watch_days_before` window.
   - Fetches the cheapest Google Flights fare for the exact dates.
   - Writes it to `fares.db` (SQLite).
   - Computes rolling 30-day median → if the new fare is ≥ threshold% below, sends a WhatsApp alert.
3. `fares.db` is committed back to the repo each run, so history survives between runs.
4. Alerts are deduped per trip to a max of 1 per 24h.
5. A monthly call-budget guard warns at 80% and hard-stops at 90% (defense-in-depth; nothing here can bill you).

## Setup (one-time)

1. **Push this repo to GitHub.**
2. **GitHub repo secrets** (Settings → Secrets and variables → Actions):
   - `CMB_PHONE` — your WhatsApp number with + prefix, e.g. `+33...`
   - `CMB_KEY` — your CallMeBot API key
3. **Enable Actions** on the repo. Run the workflow once manually (Actions tab → watch → Run workflow) to confirm it works.
4. **Belt-and-braces:** GitHub → Settings → Billing → Spending limits → set Actions to **$0**.

## Local test

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export CMB_PHONE=+33...
export CMB_KEY=...
python watcher.py
```

## Caveats

- **`fast-flights` scrapes Google Flights** — it's unofficial. If Google changes their page structure, the library may need an update. Watch the library's GitHub for issues.
- **Rate limits:** running every 6 hours with ~5 trips in-window = ~10 lookups per run = ~40/day. Well under what gets flagged. If Google starts returning empty results consistently, dial the cron back to every 12h.
- **Baseline needs ≥3 fare samples** before any alert can fire — expect ~a day of silent data collection after first deploy.
- **Airport codes in `trips.yaml` are guesses** for some destinations; verify (especially regional airports like RVN, ASR, TIV).
- **Open-jaw pricing** is approximated as two one-ways summed — real multi-city fares via a single ticket can be cheaper; this watcher won't catch those.

## Tuning

- Lower `baseline_discount_pct` → more alerts, more noise.
- Narrow `watch_days_before` → fewer lookups, less history.
- Change cron in `.github/workflows/watch.yml` (currently every 6h).

## Why not Amadeus?

Amadeus decommissioned their free Self-Service portal on 2026-07-17. The only remaining amadeus.com option is their Enterprise sales track (business accounts only). Google Flights via `fast-flights` is the next-best free source, and happens to cover low-cost carriers (easyJet, Ryanair, Vueling) better than Amadeus did anyway — which is exactly what you need from GVA.
