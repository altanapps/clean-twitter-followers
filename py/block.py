#!/usr/bin/env python3
"""
Block accounts whose score is >= threshold in a scores JSON file.

Consumes the JSON output of py/score_farmers.py or py/score_followers.py.
Resumable — won't re-block accounts already processed.

Flags:
  --auth-token TOK     X auth_token cookie value
  --ct0 TOK            X ct0 cookie value
  --scores-file PATH   Scored accounts JSON (default: output/farmer_scores.json)
  --blocked-log PATH   Where to write progress (default: output/blocked_log.json)
  --threshold N        Minimum score to block (default: 50)
"""

import json
import time
import sys
import argparse
from pathlib import Path
import requests

OUTPUT_DIR = Path(__file__).parent / "output"
OUTPUT_DIR.mkdir(exist_ok=True)
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--auth-token", required=True)
    parser.add_argument("--ct0", required=True)
    parser.add_argument("--scores-file", type=Path, default=DEFAULT_SCORES_FILE)
    parser.add_argument("--blocked-log", type=Path, default=DEFAULT_BLOCKED_LOG)
    parser.add_argument("--threshold", type=int, default=50)
    args = parser.parse_args()

    if not args.scores_file.exists():
        print(f"Missing {args.scores_file}. Run py/score_farmers.py or py/score_followers.py first.")
        sys.exit(1)

    # Load scored accounts
    scored = json.loads(args.scores_file.read_text())
    to_block = [s for s in scored if s["score"] >= args.threshold]
    print(f"Total accounts scored >= {args.threshold}: {len(to_block)}")

    # Load previous progress
    state = load_blocked(args.blocked_log)
    already = set(state["blocked_ids"])
    failed = set(state["failed_ids"])
    remaining = [s for s in to_block if s["id"] not in already and s["id"] not in failed]
    print(f"Already blocked: {len(already)}")
    print(f"Previously failed: {len(failed)}")
    print(f"To process: {len(remaining)}")

    if not remaining:
        print("Nothing to do!")
        return

    # Setup session
    s = requests.Session()
    s.cookies.set("auth_token", args.auth_token, domain=".x.com")
    s.cookies.set("ct0", args.ct0, domain=".x.com")
    s.headers.update({
        "Authorization": f"Bearer {BEARER}",
        "x-csrf-token": args.ct0,
        "x-twitter-auth-type": "OAuth2Session",
        "x-twitter-active-user": "yes",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Referer": "https://x.com/",
        "Origin": "https://x.com",
    })

    url = "https://x.com/i/api/1.1/blocks/create.json"
    success = 0
    fail = 0

    def do_block(uid, retries=3):
        """Block with retries on connection errors + rate limit handling."""
        for attempt in range(retries):
            try:
                r = s.post(url, data={"user_id": uid}, timeout=30)
            except (requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout) as e:
                print(f"    Connection error ({e}), retrying in 10s...", flush=True)
                time.sleep(10)
                continue

            if r.status_code == 429:
                reset = r.headers.get("x-rate-limit-reset")
                wait = max(int(reset) - int(time.time()), 30) if reset else 900
                print(f"  Rate limited. Waiting {wait}s...", flush=True)
                save_blocked(state, args.blocked_log)
                time.sleep(wait + 5)
                continue

            return r
        return None

    for i, account in enumerate(remaining, 1):
        resp = do_block(account["id"])
        if resp is None:
            print(f"  [{i}/{len(remaining)}] @{account['username']}: GAVE UP after retries", flush=True)
            state["failed_ids"].append(account["id"])
            fail += 1
            continue

        if resp.status_code == 200:
            state["blocked_ids"].append(account["id"])
            success += 1
            if i % 10 == 0 or i == len(remaining):
                remaining_rl = resp.headers.get("x-rate-limit-remaining", "?")
                print(f"  [{i}/{len(remaining)}] @{account['username']} (score {account['score']}) | "
                      f"blocked: {success} | failed: {fail} | rl: {remaining_rl}",
                      flush=True)
        else:
            state["failed_ids"].append(account["id"])
            fail += 1
            print(f"  [{i}/{len(remaining)}] @{account['username']}: FAILED {resp.status_code} {resp.text[:100]}",
                  flush=True)

        # Save progress every 25 blocks
        if i % 25 == 0:
            save_blocked(state, args.blocked_log)

        # Small delay between requests — be polite
        time.sleep(0.3)

    save_blocked(state, args.blocked_log)
    print(f"\n{'─' * 60}")
    print(f"DONE. Blocked: {success} | Failed: {fail}")
    print(f"Log: {args.blocked_log}")


if __name__ == "__main__":
    main()
