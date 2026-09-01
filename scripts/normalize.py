# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""
normalize.py — turn a day's raw Airbnb calendar snapshots (one JSON per
listing, as fetched by the airbnb-calendar-fetch workflow) into:

  1. data/normalized/panel_<snapshot_date>.csv
     Flat panel: listing_id, snapshot_date, stay_date, available, bookable,
     available_for_checkin, available_for_checkout, min_nights, max_nights.
     This is the durable, append-only table every future analysis (pickup
     detection, occupancy curves, day-over-day diffs) reads from.

  2. dashboard/data.json
     A compact, dashboard-ready rollup: one entry per listing with its
     metadata (title/price/room type/location, joined from
     data/listing_metadata.tsv), its next-90-day availability array, and
     a precomputed occupancy curve (% unavailable within 7/14/30/45/60/90
     days out). This is the ONLY file the static dashboard HTML reads.

Usage:
  uv run --script normalize.py [--raw-dir DIR] [--snapshot-date YYYY-MM-DD]
      [--project-dir DIR]

Defaults: raw snapshots come from ~/Downloads/airbnb_snapshots/<today>,
project dir is ~/Documents/Airbnb-Occupancy-Monitor (this repo's parent).

Resumable/idempotent: safe to re-run any time; it always rebuilds
dashboard/data.json fresh and re-writes (not appends to) the panel CSV for
that specific snapshot date, so a partial or re-run fetch won't double-count.
"""
import argparse
import csv
import datetime
import json
import os
import sys


import re

_NIGHTLY_RE = re.compile(r"x\s*\$([\d,]+(?:\.\d+)?)")
_COUNT_RE = re.compile(r"(\d+)\s*(guest|bedroom|bed|bath)")


def _parse_room_facts(sub_items):
    """subDescription items are free text like '7 guests','3 bedrooms','3 beds',
    '3 baths' or sometimes 'Private attached bathroom' with no leading number.
    Pull out whatever counts are present; anything unmatched is left None."""
    facts = {"guests": None, "bedrooms": None, "beds": None, "baths": None}
    for item in sub_items:
        if not item:
            continue
        m = _COUNT_RE.search(item.lower())
        if m:
            n, kind = m.groups()
            key = {"guest": "guests", "bedroom": "bedrooms", "bed": "beds", "bath": "baths"}[kind]
            facts[key] = int(n)
        elif "bath" in item.lower() and facts["baths"] is None:
            facts["baths"] = item.strip()  # e.g. "Private attached bathroom"
    return facts


def clean_room_url(url):
    """Strip check_in/check_out/locale query params so the link always points
    at the listing itself, not a stale date range from scrape time."""
    if not url:
        return ""
    base = url.split("?")[0]
    return base


def load_metadata(path):
    """listing_id -> enriched metadata dict, parsed from the 17-column
    dump_meta_v2.applescript TSV (id, title, price_total, price_qualifier,
    base_price_desc, property_type, room_type, location, rating, reviews,
    sub0..sub3, is_superhost, host_name, url)."""
    meta = {}
    if not os.path.exists(path):
        return meta
    with open(path, encoding="utf-8") as f:
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 17:
                parts = parts + [""] * (17 - len(parts))
            (listing_id, title, price_total, price_qual, base_desc, prop_type,
             room_type, location, rating, reviews, sub0, sub1, sub2, sub3,
             is_super, host_name, url) = parts[:17]
            listing_id = listing_id.strip()
            if not listing_id:
                continue
            m = _NIGHTLY_RE.search(base_desc)
            nightly = f"${m.group(1)}" if m else None
            facts = _parse_room_facts([sub0, sub1, sub2, sub3])
            try:
                rating_f = round(float(rating), 2) if rating.strip() else None
            except ValueError:
                rating_f = None
            try:
                reviews_i = int(float(reviews)) if reviews.strip() else None
            except ValueError:
                reviews_i = None
            meta[listing_id] = {
                "title": title.strip() or "(untitled)",
                "price": price_total.strip(),
                "price_qualifier": price_qual.strip(),
                "nightly_price": nightly,
                "property_type": prop_type.strip(),
                "room_type": room_type.strip(),
                "location": location.strip(),
                "rating": rating_f,
                "reviews": reviews_i,
                "guests": facts["guests"],
                "bedrooms": facts["bedrooms"],
                "beds": facts["beds"],
                "baths": facts["baths"],
                "is_superhost": is_super.strip().lower() == "true",
                "host_name": host_name.strip(),
                "url": clean_room_url(url.strip()),
            }
    return meta


def parse_calendar_json(path):
    """Return sorted list of day dicts: {date, available, bookable, checkin, checkout, min_nights, max_nights}."""
    with open(path, encoding="utf-8") as f:
        payload = json.load(f)
    try:
        months = payload["data"]["merlin"]["pdpAvailabilityCalendar"]["calendarMonths"]
    except (KeyError, TypeError):
        return []
    days = []
    for m in months:
        for d in m.get("days", []):
            days.append({
                "date": d.get("calendarDate"),
                "available": bool(d.get("available")),
                "bookable": bool(d.get("bookable")),
                "checkin": bool(d.get("availableForCheckin")),
                "checkout": bool(d.get("availableForCheckout")),
                "min_nights": d.get("minNights"),
                "max_nights": d.get("maxNights"),
            })
    days.sort(key=lambda x: x["date"] or "")
    return days


def occupancy_curve(days, snapshot_date, horizons=(7, 14, 30, 45, 60, 90)):
    """% of days within each forward horizon that are unavailable."""
    curve = {}
    for h in horizons:
        cutoff = (snapshot_date + datetime.timedelta(days=h)).isoformat()
        window = [d for d in days if d["date"] and snapshot_date.isoformat() <= d["date"] < cutoff]
        if not window:
            curve[str(h)] = None
            continue
        unavailable = sum(1 for d in window if not d["available"])
        curve[str(h)] = round(100 * unavailable / len(window), 1)
    return curve


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project-dir", default=os.path.expanduser("~/Documents/Airbnb-Occupancy-Monitor"))
    ap.add_argument("--raw-dir", default=None, help="Folder of <listing_id>.json files. Default: ~/Downloads/airbnb_snapshots/<snapshot-date>")
    ap.add_argument("--snapshot-date", default=datetime.date.today().isoformat())
    args = ap.parse_args()

    snapshot_date = datetime.date.fromisoformat(args.snapshot_date)
    raw_dir = args.raw_dir or os.path.expanduser(f"~/Downloads/airbnb_snapshots/{args.snapshot_date}")
    project_dir = args.project_dir
    meta_path = os.path.join(project_dir, "data", "listing_metadata_v2.tsv")
    panel_dir = os.path.join(project_dir, "data", "normalized")
    dashboard_dir = os.path.join(project_dir, "dashboard")
    os.makedirs(panel_dir, exist_ok=True)
    os.makedirs(dashboard_dir, exist_ok=True)

    if not os.path.isdir(raw_dir):
        print(f"ERROR: raw snapshot dir not found: {raw_dir}", file=sys.stderr)
        sys.exit(1)

    metadata = load_metadata(meta_path)
    print(f"Loaded metadata for {len(metadata)} listings from {meta_path}", flush=True)

    json_files = sorted(f for f in os.listdir(raw_dir) if f.endswith(".json") and not f.startswith("_"))
    print(f"Found {len(json_files)} raw calendar files in {raw_dir}", flush=True)

    panel_path = os.path.join(panel_dir, f"panel_{args.snapshot_date}.csv")
    dashboard_listings = []
    total_panel_rows = 0
    parse_failures = []

    with open(panel_path, "w", newline="", encoding="utf-8") as panel_f:
        writer = csv.writer(panel_f)
        writer.writerow([
            "listing_id", "snapshot_date", "stay_date", "available", "bookable",
            "available_for_checkin", "available_for_checkout", "min_nights", "max_nights",
        ])

        for fname in json_files:
            listing_id = fname[:-5]
            fpath = os.path.join(raw_dir, fname)
            try:
                days = parse_calendar_json(fpath)
            except Exception:
                parse_failures.append(listing_id)
                continue
            if not days:
                parse_failures.append(listing_id)
                continue

            for d in days:
                if not d["date"]:
                    continue
                writer.writerow([
                    listing_id, args.snapshot_date, d["date"], d["available"], d["bookable"],
                    d["checkin"], d["checkout"], d["min_nights"], d["max_nights"],
                ])
                total_panel_rows += 1

            m = metadata.get(listing_id, {})
            next90 = [d for d in days if d["date"] and args.snapshot_date <= d["date"] < (snapshot_date + datetime.timedelta(days=90)).isoformat()]
            dashboard_listings.append({
                "id": listing_id,
                "title": m.get("title", "(unknown)"),
                "price": m.get("price", ""),
                "price_qualifier": m.get("price_qualifier", ""),
                "nightly_price": m.get("nightly_price"),
                "property_type": m.get("property_type", ""),
                "room_type": m.get("room_type", ""),
                "location": m.get("location", ""),
                "rating": m.get("rating"),
                "reviews": m.get("reviews"),
                "guests": m.get("guests"),
                "bedrooms": m.get("bedrooms"),
                "beds": m.get("beds"),
                "baths": m.get("baths"),
                "is_superhost": m.get("is_superhost", False),
                "host_name": m.get("host_name", ""),
                "url": m.get("url", ""),
                "days": [{"date": d["date"], "available": d["available"], "checkout": d["checkout"]} for d in next90],
                "occupancy_curve": occupancy_curve(days, snapshot_date),
            })

    dashboard_payload = {
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "snapshot_date": args.snapshot_date,
        "listing_count": len(dashboard_listings),
        "listings": dashboard_listings,
    }
    dashboard_json_path = os.path.join(dashboard_dir, "data.json")
    with open(dashboard_json_path, "w", encoding="utf-8") as f:
        json.dump(dashboard_payload, f)

    # Inline the JSON straight into the HTML template so dashboard/index.html
    # is one self-contained, double-clickable file (no local server, no CORS
    # issues from file:// fetch()).
    template_path = os.path.join(dashboard_dir, "template.html")
    index_path = os.path.join(dashboard_dir, "index.html")
    if os.path.exists(template_path):
        with open(template_path, encoding="utf-8") as f:
            template = f.read()
        # data.json has no "</script>" sequences (plain data), safe to inline raw.
        html = template.replace("__AIRBNB_DATA_JSON__", json.dumps(dashboard_payload))
        with open(index_path, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"Dashboard HTML: {index_path}", flush=True)

    print(f"Panel CSV: {panel_path} ({total_panel_rows} rows)", flush=True)
    print(f"Dashboard JSON: {dashboard_json_path} ({len(dashboard_listings)} listings)", flush=True)
    if parse_failures:
        print(f"Skipped {len(parse_failures)} unparsable files (likely still fetching): {parse_failures[:5]}...", flush=True)


if __name__ == "__main__":
    main()
