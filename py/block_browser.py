#!/usr/bin/env python3
"""
Block accounts via Playwright + API calls from browser context.
Uses your Chrome cookies for auth + real Chrome's fingerprint via Playwright.
Much faster than UI clicks, and blocks actually stick.
"""

import json
import time
import sys
import argparse
import random
from pathlib import Path
import browser_cookie3
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

OUTPUT_DIR = Path(__file__).parent / "output"
DEFAULT_SCORES_FILE = OUTPUT_DIR / "farmer_scores.json"
DEFAULT_BLOCKED_LOG = OUTPUT_DIR / "blocked_log.json"

BEARER = "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"


def load_blocked(path):
    if path.exists():
        return json.loads(path.read_text())
    return {"blocked_ids": [], "failed_ids": []}


def save_blocked(state, path):
    with open(path, "w") as f:
        json.dump(state, f, indent=2)


def get_chrome_cookies():
    """Extract x.com / twitter.com cookies from Chrome. Returns (cookies_list, ct0)."""
    cookies = []
    ct0 = None
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


def human_delay(min_s=0.3, max_s=0.8):
    time.sleep(random.uniform(min_s, max_s))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--threshold", type=int, default=50)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--visible", action="store_true")
    parser.add_argument("--scores-file", default=str(DEFAULT_SCORES_FILE),
                        help="Scores JSON to consume (output of score_farmers.py or score_followers.py)")
    parser.add_argument("--blocked-log", default=str(DEFAULT_BLOCKED_LOG),
                        help="Where to write/read the resumable blocked-ids log")
    args = parser.parse_args()

    scores_file = Path(args.scores_file)
    blocked_log = Path(args.blocked_log)
    print(f"Scores file: {scores_file}")
    print(f"Blocked log: {blocked_log}")

    scored = json.loads(scores_file.read_text())
    all_targets = [s for s in scored if s["score"] >= args.threshold]
    print(f"Total accounts scored >= {args.threshold}: {len(all_targets)}")

    state = load_blocked(blocked_log)
    already = set(state["blocked_ids"])
    failed = set(state["failed_ids"])
    remaining = [s for s in all_targets if s["id"] not in already and s["id"] not in failed]
    if args.limit:
        remaining = remaining[: args.limit]

    print(f"Already blocked: {len(already)}")
    print(f"Previously failed: {len(failed)}")
    print(f"To process: {len(remaining)}")

    if not remaining:
        print("Nothing to do!")
        return

    print("\nLoading cookies from Chrome...")
    cookies, ct0 = get_chrome_cookies()
    if not ct0:
        print("ERROR: ct0 cookie not found. Are you logged into x.com in Chrome?")
        sys.exit(1)
    print(f"  Loaded {len(cookies)} cookies")

    api_headers = {
        "Authorization": f"Bearer {BEARER}",
        "x-csrf-token": ct0,
        "x-twitter-auth-type": "OAuth2Session",
        "x-twitter-active-user": "yes",
        "Referer": "https://x.com/",
        "Origin": "https://x.com",
    }

    with Stealth().use_sync(sync_playwright()) as p:
        browser = p.chromium.launch(
            headless=not args.visible,
            channel="chrome",
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            locale="en-US",
        )
        context.add_cookies(cookies)
        page = context.new_page()

        # Warm session — visit home
        print("\nVerifying session...")
        page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=30000)
        time.sleep(3)
        if "login" in page.url.lower() or "account/access" in page.url.lower():
            print(f"ERROR: Session invalid. Current URL: {page.url}")
            browser.close()
            sys.exit(1)
        print(f"  Session OK ({page.url})")

        # Block loop
        print(f"\nBlocking {len(remaining)} accounts via API...")
        success = 0
        not_found = 0
        errors = 0
        consecutive_errors = 0
        block_url = "https://x.com/i/api/1.1/blocks/create.json"

        for i, account in enumerate(remaining, 1):
            try:
                resp = context.request.post(
                    block_url,
                    form={"user_id": account["id"]},
                    headers=api_headers,
                    timeout=20000,
                )
            except Exception as e:
                print(f"  [{i}/{len(remaining)}] @{account['username']}: EXCEPTION {type(e).__name__}", flush=True)
                state["failed_ids"].append(account["id"])
                errors += 1
                consecutive_errors += 1
                if consecutive_errors >= 20:
                    print("  Too many consecutive errors. Stopping.", flush=True)
                    break
                continue

            status = resp.status
            if status == 200:
                state["blocked_ids"].append(account["id"])
                success += 1
                consecutive_errors = 0
            elif status == 404:
                state["failed_ids"].append(account["id"])
                not_found += 1
                consecutive_errors = 0
            elif status == 429:
                reset = resp.headers.get("x-rate-limit-reset")
                wait = max(int(reset) - int(time.time()), 30) if reset else 900
                print(f"  Rate limited. Waiting {wait}s...", flush=True)
                save_blocked(state, blocked_log)
                time.sleep(wait + 5)
                # Retry this account
                try:
                    resp2 = context.request.post(block_url, form={"user_id": account["id"]},
                                                  headers=api_headers, timeout=20000)
                    if resp2.status == 200:
                        state["blocked_ids"].append(account["id"])
                        success += 1
                    else:
                        state["failed_ids"].append(account["id"])
                        errors += 1
                except Exception:
                    state["failed_ids"].append(account["id"])
                    errors += 1
            elif status in (401, 403):
                print(f"  AUTH FAILED at #{i} (status {status}). Session killed.", flush=True)
                save_blocked(state, blocked_log)
                browser.close()
                sys.exit(2)
            else:
                state["failed_ids"].append(account["id"])
                errors += 1
                consecutive_errors += 1
                print(f"  [{i}] @{account['username']}: status {status}", flush=True)

            if i % 10 == 0 or i == len(remaining):
                rl = resp.headers.get("x-rate-limit-remaining", "?") if status != 429 else "0"
                print(f"  [{i}/{len(remaining)}] @{account['username']} (s={account['score']}) | "
                      f"ok: {success} nf: {not_found} err: {errors} | rl: {rl}", flush=True)

            if i % 25 == 0:
                save_blocked(state, blocked_log)

            # Periodically re-warm the session by visiting home
            if i % 100 == 0:
                try:
                    page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=20000)
                    time.sleep(2)
                except Exception:
                    pass

            # Small polite delay
            human_delay(0.3, 0.7)

        save_blocked(state, blocked_log)
        browser.close()

        print(f"\n{'─' * 60}")
        print(f"DONE. blocked: {success} | not_found: {not_found} | errors: {errors}")


if __name__ == "__main__":
    main()
