#!/usr/bin/env python3
"""
Resolve account IDs -> handles + follower counts + bio via X's internal
GraphQL UserByRestId endpoint. Use this to enrich a list of follow IDs
parsed from your X archive (import_archive.py output).

Strategy:
  1. Launch Playwright with Chrome cookies.
  2. Capture the current rotating UserByRestId query ID from x.com traffic.
  3. Loop the input IDs calling that endpoint, in-page, with real browser
     fingerprint headers inherited from the live session.

Inputs (defaults overridable via flags):
  --input PATH         Newline-separated ID list (default: output/ids.txt)
  --output PATH        JSON output (default: output/resolved.json)
  --csv PATH           CSV of resolved profiles (default: output/resolved.csv)
  --max-followers N    Also write a CSV of profiles BELOW this follower count
                       to output/unfollow_candidates.csv (default: 50000)
"""

import csv
import json
import sys
import time
import urllib.parse
from pathlib import Path

import browser_cookie3
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

BASE = Path(__file__).parent
OUTPUT_DIR = BASE / "output"
OUTPUT_DIR.mkdir(exist_ok=True)
DEFAULT_INPUT = OUTPUT_DIR / "ids.txt"
DEFAULT_RESOLVED_JSON = OUTPUT_DIR / "resolved.json"
DEFAULT_RESOLVED_CSV = OUTPUT_DIR / "resolved.csv"
DEFAULT_CANDIDATES_CSV = OUTPUT_DIR / "unfollow_candidates.csv"

BEARER = "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"


def get_chrome_cookies():
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


import re as _re
_GQL_RE = _re.compile(r"/graphql/([^/]+)/([^?]+)")


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_RESOLVED_JSON)
    parser.add_argument("--csv", type=Path, default=DEFAULT_RESOLVED_CSV)
    parser.add_argument("--candidates-csv", type=Path, default=DEFAULT_CANDIDATES_CSV)
    parser.add_argument("--max-followers", type=int, default=50_000,
                        help="Profiles below this follower count are written to candidates-csv")
    args = parser.parse_args()

    if not args.input.exists():
        print(f"Missing {args.input}. Pass --input or run import_archive.py first.")
        sys.exit(1)

    ids = [line.strip() for line in args.input.read_text().splitlines() if line.strip()]
    print(f"Loaded {len(ids):,} IDs from {args.input}")

    cookies, ct0 = get_chrome_cookies()
    if not ct0:
        print("ERROR: ct0 cookie not found. Log into x.com in Chrome first.")
        sys.exit(1)
    print(f"Loaded {len(cookies)} cookies")

    resolved = {}
    with Stealth().use_sync(sync_playwright()) as p:
        browser = p.chromium.launch(
            headless=True,
            channel="chrome",
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            viewport={"width": 1280, "height": 900}, locale="en-US"
        )
        context.add_cookies(cookies)
        page = context.new_page()

        captured_queries = {}
        captured_features = {}

        def on_request(req):
            url = req.url
            if "/graphql/" not in url:
                return
            m = _GQL_RE.search(url)
            if not m:
                return
            qid, qname = m.group(1), m.group(2)
            captured_queries[qname] = qid
            if "features=" in url:
                try:
                    qs = urllib.parse.urlparse(url).query
                    params = urllib.parse.parse_qs(qs)
                    if "features" in params:
                        captured_features[qname] = params["features"][0]
                except Exception:
                    pass

        page.on("request", on_request)

        print("Verifying session...")
        page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=30000)
        time.sleep(3)
        if "login" in page.url.lower():
            print("ERROR: session invalid")
            browser.close()
            sys.exit(1)
        print(f"  Session OK ({page.url})")

        # Trigger UserByRestId by visiting a profile via ID redirect
        print("Harvesting GraphQL query IDs...")
        seed_id = ids[0]
        try:
            page.goto(f"https://x.com/i/user/{seed_id}", wait_until="domcontentloaded", timeout=30000)
            time.sleep(5)
        except Exception as e:
            print(f"  seed profile nav warn: {e}")

        # Try a few extra pages to expand captured queries
        try:
            page.goto("https://x.com/elonmusk", wait_until="domcontentloaded", timeout=30000)
            time.sleep(4)
        except Exception:
            pass

        print(f"  captured {len(captured_queries)} distinct queries")
        ubri = captured_queries.get("UserByRestId")
        if not ubri:
            print("  UserByRestId not captured. Dumping all keys:")
            for k, v in sorted(captured_queries.items()):
                print(f"    {k} -> {v}")
            browser.close()
            sys.exit(2)
        print(f"  UserByRestId query id = {ubri}")

        features_json = captured_features.get("UserByRestId") or json.dumps({
            "hidden_profile_subscriptions_enabled": True,
            "profile_label_improvements_pcf_label_in_post_enabled": True,
            "rweb_tipjar_consumption_enabled": True,
            "verified_phone_label_enabled": False,
            "subscriptions_verification_info_is_identity_verified_enabled": True,
            "subscriptions_verification_info_verified_since_enabled": True,
            "highlights_tweets_tab_ui_enabled": True,
            "responsive_web_twitter_article_notes_tab_enabled": True,
            "subscriptions_feature_can_gift_premium": True,
            "creator_subscriptions_tweet_preview_api_enabled": True,
            "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
            "responsive_web_graphql_timeline_navigation_enabled": True,
        })
        field_toggles_json = json.dumps({"withAuxiliaryUserLabels": True})

        base_url = f"https://x.com/i/api/graphql/{ubri}/UserByRestId"

        # Run the fetch inside the page context so all auth + fingerprint
        # headers are inherited. The page JS x.com injected already sets
        # Authorization / x-csrf-token / x-client-transaction-id etc.
        fetch_js = """
        async (url) => {
          try {
            const r = await fetch(url, {
              credentials: 'include',
              headers: {
                'Authorization': 'Bearer %s',
                'x-csrf-token': document.cookie.split('; ').find(c => c.startsWith('ct0='))?.slice(4) || '',
                'x-twitter-auth-type': 'OAuth2Session',
                'x-twitter-active-user': 'yes',
                'x-twitter-client-language': 'en',
                'Content-Type': 'application/json',
                'Accept': '*/*',
              },
              referrer: 'https://x.com/home',
            });
            const text = await r.text();
            return { status: r.status, body: text };
          } catch (e) {
            return { status: -1, body: String(e) };
          }
        }
        """ % BEARER

        total = len(ids)
        ok = 0
        missing = 0
        errors = 0
        t0 = time.time()
        for i, aid in enumerate(ids, 1):
            variables = json.dumps({
                "userId": aid,
                "withSafetyModeUserFields": True,
            })
            params = {
                "variables": variables,
                "features": features_json,
                "fieldToggles": field_toggles_json,
            }
            url = base_url + "?" + urllib.parse.urlencode(params)
            try:
                resp = page.evaluate(fetch_js, url)
            except Exception as e:
                errors += 1
                print(f"  [{i}] {aid}: EXC {type(e).__name__} {e}")
                time.sleep(2)
                continue

            status = resp.get("status", 0)
            body = resp.get("body", "")

            if status == 200:
                try:
                    data = json.loads(body)
                    user = (
                        data.get("data", {})
                        .get("user", {})
                        .get("result", {})
                    )
                    if not user or user.get("__typename") == "UserUnavailable":
                        missing += 1
                    else:
                        legacy = user.get("legacy", {}) or {}
                        core = user.get("core", {}) or {}
                        resolved[aid] = {
                            "id": aid,
                            "username": core.get("screen_name") or legacy.get("screen_name", ""),
                            "name": core.get("name") or legacy.get("name", ""),
                            "followers": legacy.get("followers_count", 0),
                            "following": legacy.get("friends_count", 0),
                            "tweets": legacy.get("statuses_count", 0),
                            "verified": user.get("is_blue_verified", False) or legacy.get("verified", False),
                            "protected": legacy.get("protected", False),
                            "created_at": legacy.get("created_at", ""),
                            "bio": (legacy.get("description") or "").replace("\n", " ").strip()[:200],
                        }
                        ok += 1
                except Exception as e:
                    errors += 1
                    print(f"  [{i}] {aid}: parse error {e}")
            elif status == 429:
                print("  Rate limited — sleeping 60s")
                args.output.write_text(json.dumps(resolved, indent=2))
                time.sleep(60)
                continue
            else:
                errors += 1
                if errors <= 5:
                    print(f"  [{i}] {aid}: status {status} — {body[:200]}")

            if i % 25 == 0 or i == total:
                elapsed = time.time() - t0
                rate = i / elapsed if elapsed else 0
                eta = (total - i) / rate if rate else 0
                print(
                    f"  [{i}/{total}] ok={ok} missing={missing} err={errors} "
                    f"| {rate:.1f}/s eta {eta:.0f}s",
                    flush=True,
                )
            if i % 100 == 0:
                args.output.write_text(json.dumps(resolved, indent=2))

            time.sleep(0.3)

        browser.close()

    args.output.write_text(json.dumps(resolved, indent=2))
    print(f"\nResolved {len(resolved):,}/{len(ids):,} IDs → {args.output}")

    if not resolved:
        print("Nothing resolved — aborting CSV write")
        return

    rows = sorted(resolved.values(), key=lambda r: r["followers"])
    fieldnames = list(rows[0].keys())
    with open(args.csv, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)
    print(f"Wrote {args.csv}")

    small = [r for r in rows if r["followers"] < args.max_followers]
    with open(args.candidates_csv, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(small)
    print(f"Wrote {args.candidates_csv} ({len(small):,} accounts < {args.max_followers:,} followers)")


if __name__ == "__main__":
    main()
