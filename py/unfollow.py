#!/usr/bin/env python3
"""
Bulk unfollow accounts from a CSV. Uses your Chrome cookies + Playwright
to call X's v1.1 friendships/destroy.json endpoint via page-context fetch
(inherits real browser fingerprint).

Input CSV must have at least these columns: id, username, followers
(matches the output of resolve_handles.py).

Resumable: writes successes/failures to output/unfollowed_log.json and
skips anything already processed on re-run.

Flags:
  --csv PATH     CSV of unfollow targets (default: output/unfollow_candidates.csv)
  --dry-run      Do not actually unfollow; just print what would happen.
  --limit N      Only process the first N pending accounts.
  --visible      Run the browser head-ful so you can watch it.
"""

import argparse
import csv
import json
import sys
import time
from pathlib import Path

import browser_cookie3
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

BASE = Path(__file__).parent
OUTPUT_DIR = BASE / "output"
DEFAULT_CSV = OUTPUT_DIR / "unfollow_candidates.csv"
LOG_FILE = OUTPUT_DIR / "unfollowed_log.json"

BEARER = "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"


def get_chrome_cookies():
    cookies, ct0 = [], None
    for domain in (".x.com", ".twitter.com"):
        try:
            for c in browser_cookie3.chrome(domain_name=domain):
                cookies.append({
                    "name": c.name,
                    "value": c.value,
                    "domain": c.domain,
                    "path": c.path or "/",
                    "secure": bool(c.secure),
                    "httpOnly": False,
                    "expires": c.expires if c.expires else -1,
                    "sameSite": "None" if c.secure else "Lax",
                })
                if c.name == "ct0" and domain == ".x.com":
                    ct0 = c.value
        except Exception as e:
            print(f"  Warning: could not load cookies for {domain}: {e}")
    return cookies, ct0


def load_log():
    if LOG_FILE.exists():
        return json.loads(LOG_FILE.read_text())
    return {"unfollowed_ids": [], "failed_ids": [], "not_found_ids": []}


def save_log(state):
    LOG_FILE.write_text(json.dumps(state, indent=2))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV,
                        help=f"CSV of unfollow targets (default: {DEFAULT_CSV})")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--visible", action="store_true")
    args = parser.parse_args()

    if not args.csv.exists():
        print(f"Missing {args.csv}. Run resolve_handles.py first.")
        sys.exit(1)

    OUTPUT_DIR.mkdir(exist_ok=True)
    with open(args.csv) as f:
        all_targets = list(csv.DictReader(f))
    print(f"Candidates in CSV: {len(all_targets)}")

    state = load_log()
    done = set(state["unfollowed_ids"]) | set(state["failed_ids"]) | set(state["not_found_ids"])
    pending = [t for t in all_targets if t["id"] not in done]
    print(f"Already processed: {len(done)} | Pending: {len(pending)}")
    if args.limit:
        pending = pending[: args.limit]
        print(f"Limited to first {len(pending)}")
    if not pending:
        print("Nothing to do.")
        return

    cookies, ct0 = get_chrome_cookies()
    if not ct0:
        print("ERROR: ct0 cookie not found. Log into x.com in Chrome first.")
        sys.exit(1)

    with Stealth().use_sync(sync_playwright()) as p:
        browser = p.chromium.launch(
            headless=not args.visible,
            channel="chrome",
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(viewport={"width": 1280, "height": 900}, locale="en-US")
        context.add_cookies(cookies)
        page = context.new_page()

        print("Verifying session...")
        page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=30000)
        time.sleep(3)
        if "login" in page.url.lower():
            print("ERROR: session invalid")
            browser.close()
            sys.exit(1)
        print(f"  Session OK ({page.url})")

        # POST to friendships/destroy via page.evaluate — inherits real browser fingerprint
        fetch_js = """
        async (userId) => {
          try {
            const ct0 = document.cookie.split('; ').find(c => c.startsWith('ct0='))?.slice(4) || '';
            const body = new URLSearchParams({ user_id: userId }).toString();
            const r = await fetch('https://x.com/i/api/1.1/friendships/destroy.json', {
              method: 'POST',
              credentials: 'include',
              headers: {
                'Authorization': 'Bearer %s',
                'x-csrf-token': ct0,
                'x-twitter-auth-type': 'OAuth2Session',
                'x-twitter-active-user': 'yes',
                'x-twitter-client-language': 'en',
                'Content-Type': 'application/x-www-form-urlencoded',
                'Accept': '*/*',
              },
              referrer: 'https://x.com/home',
              body,
            });
            const text = await r.text();
            return { status: r.status, body: text };
          } catch (e) {
            return { status: -1, body: String(e) };
          }
        }
        """ % BEARER

        ok = nf = err = 0
        t0 = time.time()
        consecutive_err = 0
        total = len(pending)
        mode = "DRY-RUN" if args.dry_run else "LIVE"
        print(f"\n[{mode}] Unfollowing {total} accounts...")

        for i, t in enumerate(pending, 1):
            uid = t["id"]
            uname = t["username"]
            followers = t["followers"]

            if args.dry_run:
                print(f"  [DRY {i}/{total}] would unfollow @{uname} ({followers} followers)")
                ok += 1
                continue

            try:
                resp = page.evaluate(fetch_js, uid)
            except Exception as e:
                err += 1
                consecutive_err += 1
                print(f"  [{i}] @{uname}: EXC {type(e).__name__} {e}")
                state["failed_ids"].append(uid)
                if consecutive_err >= 15:
                    print("  Too many consecutive errors, stopping.")
                    break
                time.sleep(3)
                continue

            status = resp.get("status", 0)
            body = resp.get("body", "")

            if status == 200:
                state["unfollowed_ids"].append(uid)
                ok += 1
                consecutive_err = 0
            elif status == 404:
                # already not following or user gone
                state["not_found_ids"].append(uid)
                nf += 1
                consecutive_err = 0
            elif status == 429:
                print("  Rate limited — sleeping 60s")
                save_log(state)
                time.sleep(60)
                continue
            elif status in (502, 503, 504):
                # transient overload — retry once after a short delay
                print(f"  [{i}] @{uname}: transient {status}, retrying in 3s")
                time.sleep(3)
                try:
                    resp2 = page.evaluate(fetch_js, uid)
                    if resp2.get("status") == 200:
                        state["unfollowed_ids"].append(uid)
                        ok += 1
                        consecutive_err = 0
                        continue
                except Exception:
                    pass
                # If retry also failed, log and move on (don't poison failed_ids;
                # a later run with a fresh log entry can pick it up)
                err += 1
                continue
            elif status in (401, 403):
                print(f"  AUTH FAILED status={status} at #{i}. Session killed.")
                print(f"  Body: {body[:300]}")
                save_log(state)
                browser.close()
                sys.exit(2)
            else:
                err += 1
                consecutive_err += 1
                state["failed_ids"].append(uid)
                if err <= 5:
                    print(f"  [{i}] @{uname}: status {status} — {body[:200]}")
                if consecutive_err >= 15:
                    print("  Too many consecutive errors, stopping.")
                    save_log(state)
                    break

            if i % 10 == 0 or i == total:
                elapsed = time.time() - t0
                rate = i / elapsed if elapsed else 0
                eta = (total - i) / rate if rate else 0
                print(
                    f"  [{i}/{total}] @{uname:<20} ok={ok} nf={nf} err={err} "
                    f"| {rate:.1f}/s eta {eta:.0f}s",
                    flush=True,
                )
            if i % 25 == 0:
                save_log(state)
            time.sleep(0.5)

        save_log(state)
        browser.close()
        print(f"\n{'─' * 60}")
        print(f"DONE [{mode}]. ok={ok} not_found={nf} errors={err}")
        print(f"Log: {LOG_FILE}")


if __name__ == "__main__":
    main()
