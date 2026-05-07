#!/usr/bin/env python3
"""
Find + delete tweets in your X archive that match regex patterns.

Two-step flow (so you always review before deleting):

  Step 1 — find matches:
    python3 py/delete_tweets_by_pattern.py \\
        --tweets /path/to/archive/data/tweets.js \\
        --patterns examples/crypto_patterns.json \\
        --output output/matched.csv

  Step 2 — manually edit output/matched.csv:
    Set approve_delete=1 on rows you want gone.

  Step 3 — delete the approved rows:
    python3 py/delete_tweets_by_pattern.py --apply --csv output/matched.csv

The patterns file is JSON: a list of [regex, tag] pairs (regex matched
case-insensitively against tweet text).

Calls X's v1.1 statuses/destroy/{id}.json which works for tweets and
retweets alike (deleting a retweet's ID = un-retweet).

Resumable: writes output/deleted_tweets_log.json, skips already-processed.
"""

import argparse
import csv
import json
import re
import sys
import time
from pathlib import Path

OUTPUT_DIR = Path(__file__).parent / "output"
OUTPUT_DIR.mkdir(exist_ok=True)
DEFAULT_LOG = OUTPUT_DIR / "deleted_tweets_log.json"

BEARER = "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"


def load_tweets(path):
    raw = path.read_text()
    return json.loads(raw[raw.find("["):])


def find_matches(tweets, patterns):
    """patterns: list of (compiled_regex, tag). Returns list of dicts."""
    out = []
    for t in tweets:
        tw = t.get("tweet", t)
        text = tw.get("full_text", "")
        low = text.lower()
        hits = [tag for rx, tag in patterns if rx.search(low)]
        if not hits:
            continue
        out.append({
            "id": tw.get("id_str") or tw.get("id"),
            "created_at": tw.get("created_at", ""),
            "tags": ",".join(sorted(set(hits))),
            "text": text.replace("\n", " ")[:300],
            "approve_delete": "",
        })
    return out


def cmd_find(args):
    if not args.tweets.exists():
        print(f"Missing {args.tweets}")
        sys.exit(1)
    if not args.patterns.exists():
        print(f"Missing {args.patterns}")
        sys.exit(1)

    raw_patterns = json.loads(args.patterns.read_text())
    compiled = [(re.compile(p[0], re.IGNORECASE), p[1]) for p in raw_patterns]
    print(f"Loaded {len(compiled)} patterns")

    tweets = load_tweets(args.tweets)
    print(f"Scanning {len(tweets):,} tweets...")
    matches = find_matches(tweets, compiled)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["id", "created_at", "tags", "text", "approve_delete"])
        w.writeheader()
        w.writerows(matches)
    print(f"\n{len(matches):,} matches → {args.output}")
    print(f"Edit the CSV: set approve_delete=1 on rows you want to delete.")
    print(f"Then re-run with --apply --csv {args.output}")


def cmd_delete(args):
    import browser_cookie3
    from playwright.sync_api import sync_playwright
    from playwright_stealth import Stealth

    if not args.csv.exists():
        print(f"Missing {args.csv}. Run without --apply first to generate the matched CSV.")
        sys.exit(1)

    with open(args.csv) as f:
        rows = [r for r in csv.DictReader(f) if str(r.get("approve_delete", "")).strip() == "1"]
    print(f"Approved for delete: {len(rows)}")

    log = json.loads(DEFAULT_LOG.read_text()) if DEFAULT_LOG.exists() else {
        "deleted_ids": [], "failed_ids": [], "not_found_ids": []
    }
    done = set(log["deleted_ids"]) | set(log["failed_ids"]) | set(log["not_found_ids"])
    pending = [r for r in rows if r["id"] not in done]
    if args.limit:
        pending = pending[:args.limit]
    print(f"Already processed: {len(done)} | Pending: {len(pending)}")
    if not pending:
        print("Nothing to do.")
        return

    cookies, ct0 = [], None
    for domain in (".x.com", ".twitter.com"):
        try:
            for c in browser_cookie3.chrome(domain_name=domain):
                cookies.append({
                    "name": c.name, "value": c.value, "domain": c.domain,
                    "path": c.path or "/", "secure": bool(c.secure),
                    "httpOnly": False,
                    "expires": c.expires if c.expires else -1,
                    "sameSite": "None" if c.secure else "Lax",
                })
                if c.name == "ct0" and domain == ".x.com":
                    ct0 = c.value
        except Exception as e:
            print(f"  Warning: {e}")
    if not ct0:
        print("ERROR: ct0 cookie not found. Log into x.com in Chrome first.")
        sys.exit(1)

    with Stealth().use_sync(sync_playwright()) as p:
        browser = p.chromium.launch(
            headless=not args.visible,
            channel="chrome",
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = browser.new_context(viewport={"width": 1280, "height": 900}, locale="en-US")
        ctx.add_cookies(cookies)
        page = ctx.new_page()
        page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=30000)
        time.sleep(3)
        if "login" in page.url.lower():
            print("ERROR: session invalid")
            browser.close()
            sys.exit(1)

        fetch_js = """
        async (tid) => {
          try {
            const ct0 = document.cookie.split('; ').find(c => c.startsWith('ct0='))?.slice(4) || '';
            const r = await fetch(`https://x.com/i/api/1.1/statuses/destroy/${tid}.json`, {
              method: 'POST',
              credentials: 'include',
              headers: {
                'Authorization': 'Bearer %s',
                'x-csrf-token': ct0,
                'x-twitter-auth-type': 'OAuth2Session',
                'x-twitter-active-user': 'yes',
                'Content-Type': 'application/x-www-form-urlencoded',
              },
              referrer: 'https://x.com/home',
            });
            return { status: r.status, body: await r.text() };
          } catch (e) { return { status: -1, body: String(e) }; }
        }
        """ % BEARER

        ok = nf = err = 0
        mode = "DRY-RUN" if args.dry_run else "LIVE"
        print(f"\n[{mode}] Deleting {len(pending)} tweets...")
        for i, row in enumerate(pending, 1):
            tid = row["id"]
            preview = row["text"][:60]
            if args.dry_run:
                print(f"  [DRY {i}/{len(pending)}] would delete {tid}: {preview}")
                ok += 1
                continue
            resp = page.evaluate(fetch_js, tid)
            status = resp.get("status", 0)
            if status == 200:
                log["deleted_ids"].append(tid); ok += 1
            elif status in (404, 144):
                log["not_found_ids"].append(tid); nf += 1
            elif status == 429:
                print("  Rate limited — sleeping 60s")
                DEFAULT_LOG.write_text(json.dumps(log, indent=2))
                time.sleep(60)
                continue
            else:
                log["failed_ids"].append(tid); err += 1
                if err <= 5:
                    print(f"  [{i}] {tid}: status {status} — {resp.get('body','')[:200]}")
            if i % 10 == 0:
                print(f"  [{i}/{len(pending)}] ok={ok} nf={nf} err={err}", flush=True)
            if i % 25 == 0:
                DEFAULT_LOG.write_text(json.dumps(log, indent=2))
            time.sleep(0.5)

        DEFAULT_LOG.write_text(json.dumps(log, indent=2))
        browser.close()
        print(f"\nDONE [{mode}]. ok={ok} not_found={nf} errors={err}")
        print(f"Log: {DEFAULT_LOG}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true",
                        help="Actually delete (without this, run in find-mode)")
    # find mode
    parser.add_argument("--tweets", type=Path,
                        help="(find) Path to archive's data/tweets.js")
    parser.add_argument("--patterns", type=Path,
                        help="(find) JSON file: list of [regex, tag] pairs")
    parser.add_argument("--output", type=Path, default=OUTPUT_DIR / "matched.csv",
                        help="(find) Where to write matches CSV")
    # delete mode
    parser.add_argument("--csv", type=Path, default=OUTPUT_DIR / "matched.csv",
                        help="(delete) CSV with approve_delete=1 rows to delete")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--visible", action="store_true")
    args = parser.parse_args()

    if args.apply:
        cmd_delete(args)
    else:
        if not args.tweets or not args.patterns:
            parser.error("find-mode requires --tweets PATH --patterns PATH")
        cmd_find(args)


if __name__ == "__main__":
    main()
