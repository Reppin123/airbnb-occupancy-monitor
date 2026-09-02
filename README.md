# Delhi Airbnb Occupancy Monitor

GitHub: [Reppin123/airbnb-occupancy-monitor](https://github.com/Reppin123/airbnb-occupancy-monitor) (private).

Daily-refreshable occupancy intelligence for a ~1,249-listing Delhi-NCR
Airbnb portfolio (Chandigarh / Mohali / Panchkula / Zirakpur area). Pulls
each listing's forward availability calendar straight from Airbnb's own
public web GraphQL endpoint (no paid scraping service, no proxy token),
turns it into a clean panel dataset, and renders a self-contained HTML
heatmap dashboard for browsing occupancy at a glance.

## Why this exists

Apify-style scraping services charge per run and rate-limit hard. This
pipeline instead rides a **real, already-logged-in Chrome tab** on
`airbnb.co.in` via AppleScript — same-origin requests carry the browser's
own cookies, which sidesteps Airbnb's Akamai IP-based rate limiting
entirely. Zero ongoing cost, zero third-party token.

## Pipeline

```
listing_ids.txt (1,249 Airbnb listing IDs, Delhi portfolio)
        |
        v
[1] FETCH   — fetch/run.py
    Live Chrome session -> Airbnb's StaysPdpAtomicAvailabilityCalendarQuery
    GraphQL endpoint -> raw per-listing JSON, one file per listing,
    written to ~/Downloads/airbnb_snapshots/<snapshot_date>/<id>.json
        |
        v
[2] NORMALIZE — scripts/normalize.py
    Reads that day's raw JSON + data/listing_metadata_v2.tsv (title,
    price, rating, host, amenities-derived facts, room counts, URL — see
    Data dictionary below) and produces:
      - data/normalized/panel_<date>.csv   (flat day-by-day panel table)
      - dashboard/data.json                (dashboard-ready rollup)
      - dashboard/index.html               (data.json inlined into
                                             dashboard/template.html —
                                             a single double-clickable file)
        |
        v
[3] VIEW — open dashboard/index.html
    Heatmap of listings x next 90 days, click any row for the detail
    drawer (occupancy curve, rating, host, direct Airbnb link).
```

The **fetch** step (step 1) now ships in this repo under `fetch/`
(`run.py`, `listing_ids.txt`), so anyone who clones this repo can run
the exact same 1,249-listing scrape end to end with no separate setup —
just Chrome, no Apify account, no API token. It's also kept as a
reusable Apprentice workflow locally, at:
```
~/Library/Application Support/Apprentice-dev/workflows/airbnb-calendar-fetch/
```
That local copy is the day-to-day one Apprentice runs on a schedule;
`fetch/` in this repo is the public, reproducible copy. This repo's
`scripts/normalize.py` onward is the project-specific downstream half:
turning those raw snapshots into something a person actually looks at.

## How to run it

**1. Fetch today's calendars:**
```bash
cd fetch
uv run --script run.py \
    --ids-file listing_ids.txt \
    --out-dir ~/Downloads/airbnb_snapshots \
    --months 3 --pace 1.0
```
Requires Google Chrome open with "Allow JavaScript from Apple Events"
enabled (System Settings > Privacy & Security > Automation). No Airbnb
login or token needed — the script rides Chrome's own same-origin
session. Resumable — safe to re-run; it skips any listing ID already
fetched for today. Full 1,249-listing run takes roughly 30–60 minutes
at the default pace. Swap in your own `listing_ids.txt` (one Airbnb
listing ID per line) to point this at a different portfolio.

**2. Normalize + rebuild the dashboard:**
```bash
cd ~/Documents/Airbnb-Occupancy-Monitor
/opt/homebrew/bin/uv run --script scripts/normalize.py
```
Defaults to today's snapshot date and `~/Downloads/airbnb_snapshots/<today>`
as the raw source. Pass `--snapshot-date YYYY-MM-DD` to (re)process a
different day.

**3. Open `dashboard/index.html`** in any browser. It's fully
self-contained (data is inlined at build time), so it opens straight off
disk with no local server needed.

## Refreshing listing metadata

`data/listing_metadata_v2.tsv` (title, price, rating, host, room facts,
Airbnb URL — one row per listing) is pulled from the master Apify export
workbook via a live-Excel AppleScript dump (`dump_meta_v2.applescript`,
kept alongside the fetch workflow's scratch files since it only needs to
run again if the source workbook is refreshed with a new Apify batch —
listing calendar data itself is refreshed daily, metadata is not).

## Data dictionary

**`data/listing_metadata_v2.tsv`** (tab-separated, no header row):
| # | Field | Notes |
|---|---|---|
| 1 | listing_id | |
| 2 | title | |
| 3 | price (total) | Total price Airbnb quoted for whatever stay window was active at scrape time — **not a nightly rate**. |
| 4 | price_qualifier | e.g. `"total"` |
| 5 | base_price_description | e.g. `"5 nights x $55.62"` — normalize.py parses the per-night figure out of this into `nightly_price`. |
| 6 | property_type | e.g. `"Entire condo"` |
| 7 | room_type | Entire home/apt, Private room, Shared room |
| 8 | location | Full location string, e.g. `"Chandigarh, India"` |
| 9 | rating | Guest-satisfaction score (Airbnb's overall/sub-scores were identical in every sampled listing) |
| 10 | reviews | Review count |
| 11–14 | sub-description items | Free text Airbnb renders under the title — guest/bedroom/bed/bath counts, parsed by normalize.py's `_parse_room_facts` |
| 15 | is_superhost | `"true"`/`"false"` |
| 16 | host_name | |
| 17 | url | Airbnb room URL, query params stripped in normalize.py so it doesn't point at a stale date range |

**Raw calendar JSON** (`~/Downloads/airbnb_snapshots/<date>/<id>.json`):
Airbnb's native `StaysPdpAtomicAvailabilityCalendarQuery` response —
`data.merlin.pdpAvailabilityCalendar.calendarMonths[].days[]`, each day:
`calendarDate`, `available`, `bookable`, `availableForCheckin`,
`availableForCheckout`, `minNights`, `maxNights`.

**`data/normalized/panel_<date>.csv`**: `listing_id, snapshot_date,
stay_date, available, bookable, available_for_checkin,
available_for_checkout, min_nights, max_nights` — one row per
listing-per-day. This is the durable table every future day-over-day
diff (booking/cancellation inference) reads from.

## Known caveats

- **Price is a total, not nightly.** `nightly_price` (parsed from the
  base-price description) is the best per-night estimate we have from a
  single snapshot; it reflects whatever length-of-stay Airbnb defaulted
  to at scrape time and can include weekly/monthly discounts.
- **`available: false` conflates multiple states** — guest booking, host
  block, maintenance, and minimum-stay constraints all show up the same
  way. Confirmed it correctly distinguishes checkout-only days
  (`availableForCheckout: true`, `availableForCheckin: false`) though,
  which the simplified page UI does not surface as clearly.
- Full run on 2026-09-01: 1,244 of 1,249 listings fetched cleanly, 5
  failed (see `_failed.txt` in that day's snapshot folder) — worth a
  retry pass before trusting portfolio-wide aggregates.

## Not yet built (roadmap)

- **Delta / pickup-event engine** — diff consecutive days' panels to
  flag available→unavailable (likely booking) and the reverse (likely
  cancellation) transitions. Needs ≥2 days of snapshots to exist.
- **Scheduled daily fetch** — via Apprentice's `schedule` tool (not
  cron/launchd) once the pipeline's been run manually a few more days.
- **Richer per-listing detail** — Airbnb's page exposes far more than
  we currently pull: full photo gallery, complete amenities list,
  house rules, cancellation policy, and actual guest review text all
  sit behind different page sections/queries than the calendar one.
  Day-level *pricing* (as opposed to total-at-scrape-time) is plausible
  too — Airbnb's own date-picker UI shows a price per selectable day —
  but the exact query shape for that hasn't been confirmed yet.

## Requirements

- Google Chrome, signed into nothing in particular but open with a tab
  reachable (the fetch script opens one on `airbnb.co.in` if none exists)
  and "Allow JavaScript from Apple Events" enabled.
- Microsoft Excel open with the source Apify workbook, only needed when
  re-pulling `listing_metadata_v2.tsv` after a fresh Apify export.
- `uv` (invoked via its absolute path, `/opt/homebrew/bin/uv`, since
  scheduled/background shells may not have Homebrew on PATH).
- No other dependencies — every script is pure Python stdlib, pinned via
  `uv run --script` inline metadata headers.
