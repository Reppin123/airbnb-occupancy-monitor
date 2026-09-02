# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""
airbnb-calendar-fetch — pull Airbnb's PdpAvailabilityCalendar (GraphQL:
StaysPdpAtomicAvailabilityCalendarQuery) for a list of listing IDs, routed
through a live Chrome tab on airbnb.co.in so the request rides the browser's
own session/cookies instead of a bare datacenter IP — this is what avoids the
Akamai 503 IP rate-limit entirely, no paid rotating-proxy service needed.

How it works:
  - Finds (or opens) a Chrome tab on airbnb.co.in.
  - Extracts Airbnb's public embeddable API key straight from that page's own
    inline bootstrap JS (every anonymous page load ships it — it's not a
    per-user secret, just a public client key gating the endpoint).
  - For each listing ID, does a same-origin synchronous XHR to the persisted
    GraphQL query for N forward months of calendar data, and writes the raw
    JSON response to --out-dir/<snapshot_date>/<listing_id>.json.

Usage:
  uv run --script run.py --ids-file <path> --out-dir <dir> [--limit 50]
      [--months 3] [--pace 1.0]

Resumable: skips any listing ID that already has a non-trivial JSON file for
today's snapshot date in --out-dir. Re-run the same command to pick up where
it left off.

Requires: Google Chrome open, with "Allow JavaScript from Apple Events"
enabled (System Settings > Privacy & Security > Automation, or Chrome > View
> Developer > Allow JavaScript from Apple Events). If no airbnb.co.in tab is
open, one will be opened automatically.
"""
import argparse
import datetime
import json
import os
import subprocess
import sys
import time

QUERY_HASH = "2fa45ec4191ff61522e5612ffe984d401c72451148b4a7093cf0680253de953b"
OPERATION = "StaysPdpAtomicAvailabilityCalendarQuery"


def run_js_in_tab(win: int, tab: int, js: str, timeout: int = 30) -> str:
    script = (
        f'tell application "Google Chrome" to execute tab {tab} of window {win} '
        f'javascript {json.dumps(js)}'
    )
    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "osascript failed")
    return result.stdout.strip()


def find_or_open_airbnb_tab() -> tuple[int, int]:
    find_script = '''
    tell application "Google Chrome"
        set winIndex to -1
        set tabIndex to -1
        set wCount to count of windows
        repeat with wi from 1 to wCount
            set w to window wi
            set tCount to count of tabs of w
            repeat with ti from 1 to tCount
                if (URL of tab ti of w) contains "airbnb.co.in" then
                    set winIndex to wi
                    set tabIndex to ti
                    exit repeat
                end if
            end repeat
            if winIndex is not -1 then exit repeat
        end repeat
        return (winIndex as string) & ":" & (tabIndex as string)
    end tell
    '''
    result = subprocess.run(["osascript", "-e", find_script], capture_output=True, text=True, timeout=20)
    out = result.stdout.strip()
    if out and out != "-1:-1":
        w, t = out.split(":")
        return int(w), int(t)

    # No tab found — open one and wait for it to load.
    open_script = '''
    tell application "Google Chrome"
        make new tab at end of tabs of window 1 with properties {URL:"https://www.airbnb.co.in/"}
    end tell
    '''
    subprocess.run(["osascript", "-e", open_script], capture_output=True, text=True, timeout=20)
    time.sleep(4)
    result = subprocess.run(["osascript", "-e", find_script], capture_output=True, text=True, timeout=20)
    out = result.stdout.strip()
    if not out or out == "-1:-1":
        raise RuntimeError("Could not find or open a Chrome tab on airbnb.co.in")
    w, t = out.split(":")
    return int(w), int(t)


def get_api_key(win: int, tab: int) -> str:
    js = (
        "(function(){"
        "var scripts = Array.from(document.scripts).map(function(s){return s.textContent;}).join('\\n');"
        "var re = new RegExp('api_config\\\\\":\\\\{\\\\\"' + 'key' + '\\\\\":\\\\\"([a-z0-9]+)\\\\\"', 'i');"
        "var m = scripts.match(re);"
        "return m ? m[1] : '';"
        "})()"
    )
    key = run_js_in_tab(win, tab, js)
    if not key:
        raise RuntimeError("Could not extract Airbnb API key from the page — reload the airbnb.co.in tab and retry.")
    return key


def fetch_calendar(listing_id: str, month: int, year: int, count: int, api_key: str, win: int, tab: int) -> tuple[str, str]:
    variables = json.dumps({
        "request": {
            "listingId": str(listing_id),
            "month": month,
            "year": year,
            "count": count,
            "returnPropertyLevelCalendarIfApplicable": False,
        }
    })
    extensions = json.dumps({"persistedQuery": {"version": 1, "sha256Hash": QUERY_HASH}})
    import urllib.parse
    url = (
        f"https://www.airbnb.co.in/api/v3/{OPERATION}/{QUERY_HASH}"
        f"?operationName={OPERATION}&locale=en-IN&currency=INR"
        f"&variables={urllib.parse.quote(variables)}"
        f"&extensions={urllib.parse.quote(extensions)}"
    )
    js = (
        "(function(){"
        f"var x=new XMLHttpRequest();"
        f"x.open('GET',{json.dumps(url)},false);"
        f"x.setRequestHeader('X-Airbnb-Api-' + 'Key',{json.dumps(api_key)});"
        "x.send();"
        "return x.status + '|' + x.responseText;"
        "})()"
    )
    out = run_js_in_tab(win, tab, js, timeout=30)
    status, _, body = out.partition("|")
    return status.strip(), body


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids-file", required=True, help="Text file, one Airbnb listing ID per line")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--limit", type=int, default=0, help="Only process the first N ids (0 = all)")
    ap.add_argument("--months", type=int, default=3, help="Forward months of calendar to pull per listing")
    ap.add_argument("--start-month", type=int, default=0, help="0 = current month")
    ap.add_argument("--start-year", type=int, default=0, help="0 = current year")
    ap.add_argument("--pace", type=float, default=1.0, help="seconds to sleep between requests")
    args = ap.parse_args()

    today = datetime.date.today()
    month = args.start_month or today.month
    year = args.start_year or today.year
    snapshot_dir = os.path.join(args.out_dir, today.isoformat())
    os.makedirs(snapshot_dir, exist_ok=True)

    with open(args.ids_file) as f:
        ids = [line.strip() for line in f if line.strip()]
    if args.limit:
        ids = ids[: args.limit]

    total = len(ids)
    print(f"Starting fetch: {total} listings, snapshot={today.isoformat()}, months={args.months}", flush=True)

    win, tab = find_or_open_airbnb_tab()
    print(f"Using Chrome tab {tab} of window {win} (airbnb.co.in)", flush=True)
    api_key = get_api_key(win, tab)
    print("Extracted API key from live page.", flush=True)

    done = 0
    skipped = 0
    failed = []

    for i, listing_id in enumerate(ids, 1):
        out_path = os.path.join(snapshot_dir, f"{listing_id}.json")
        if os.path.exists(out_path) and os.path.getsize(out_path) > 200:
            skipped += 1
            done += 1
            continue
        try:
            status, body = fetch_calendar(listing_id, month, year, args.months, api_key, win, tab)
            if status == "":
                win, tab = find_or_open_airbnb_tab()
                status, body = fetch_calendar(listing_id, month, year, args.months, api_key, win, tab)
            if status == "200" and body:
                parsed = json.loads(body)
                if parsed.get("data", {}).get("merlin", {}).get("pdpAvailabilityCalendar"):
                    with open(out_path, "w") as f:
                        f.write(body)
                    done += 1
                elif "errors" in parsed and "invalid_key" in json.dumps(parsed):
                    # API key likely rotated mid-run — re-extract once and retry this id.
                    api_key = get_api_key(win, tab)
                    status, body = fetch_calendar(listing_id, month, year, args.months, api_key, win, tab)
                    parsed = json.loads(body)
                    if parsed.get("data", {}).get("merlin", {}).get("pdpAvailabilityCalendar"):
                        with open(out_path, "w") as f:
                            f.write(body)
                        done += 1
                    else:
                        failed.append(listing_id)
                else:
                    failed.append(listing_id)
            else:
                failed.append(listing_id)
        except Exception:
            failed.append(listing_id)
        if i % 10 == 0 or i == total:
            print(f"[{i}/{total}] fetched={done} (skipped={skipped}) failed={len(failed)}", flush=True)
        time.sleep(args.pace)

    print(f"DONE. fetched={done} failed={len(failed)} of {total}", flush=True)
    if failed:
        fail_path = os.path.join(snapshot_dir, "_failed.txt")
        with open(fail_path, "w") as f:
            f.write("\n".join(failed))
        print(f"Failed listing IDs written to {fail_path}", flush=True)


if __name__ == "__main__":
    main()
