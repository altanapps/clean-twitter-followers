#!/usr/bin/env python3
"""
Score followers (and non-mutual follows) for airdrop-farmer / yapper signals.
Defaults are tuned for Kaito-style farming, but the pattern dict is editable
either inline below or via --patterns examples/farmer_patterns.json.

No API key needed — uses your X session cookies passed as --auth-token / --ct0
(extract from your browser; see py/README.md).

Output: output/farmer_scores.json (and .csv). Feed into py/block.py to act on.
"""

import json
import time
import sys
import csv
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
import requests

DATA_DIR = Path(__file__).parent / "data"
OUTPUT_DIR = Path(__file__).parent / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

TWITTER_EPOCH = 1288834974657
BEARER = "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
GRAPHQL_USER = "https://x.com/i/api/graphql/xf3jd90KKBCUxdlI_tNHZw/UserByRestId"
GRAPHQL_FEATURES = json.dumps({
    "hidden_profile_subscriptions_enabled": True,
    "rweb_tipjar_consumption_enabled": True,
    "responsive_web_graphql_exclude_directive_enabled": True,
    "verified_phone_label_enabled": False,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "responsive_web_graphql_timeline_navigation_enabled": True,
})

# ── Farmer Scoring ─────────────────────────────────────────────────────────
# Bio keyword → score weight. Edit inline or override with --patterns FILE
# (see examples/farmer_patterns.json for the format).

FARMER_BIO_KEYWORDS = {
    "kaito": 40, "yapper": 40, "yappers": 40,
    "$kaito": 50, "kaito ai": 50, "kaito yap": 50,
    "airdrop": 35, "airdrops": 35,
    "airdrop hunter": 45, "airdrop farmer": 45,
    "testnet": 25, "node runner": 20, "early adopter": 15,
    "web3 enthusiast": 20, "crypto enthusiast": 20,
    "blockchain enthusiast": 20, "defi enthusiast": 20,
    "nft enthusiast": 20, "crypto lover": 25,
    "web3": 10, "crypto trader": 10, "degen": 15,
    "wagmi": 15, "ngmi": 10, "hodl": 10,
    "to the moon": 15, "100x": 20, "1000x": 25,
    "alpha caller": 20, "alpha hunter": 20,
    "gem hunter": 25, "gem finder": 25,
    "gm": 8, "lfg": 8,
    "ai x crypto": 20, "ai & crypto": 20, "depin": 15, "ai agent": 10,
    "follow back": 30, "follow4follow": 40, "f4f": 35,
    "followback": 30, "follow me": 15,
    "dm for collab": 25, "open for collab": 15,
}
FARMER_EMOJIS = ["🚀", "💎", "🔥", "⚡", "🌐", "🤖", "🧠", "💰", "📈", "🎯"]


def id_to_date(twitter_id):
    ms = (int(twitter_id) >> 22) + TWITTER_EPOCH
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def load_ids_from(data_dir, filename, key):
    with open(data_dir / filename) as f:
        raw = f.read()
    return set(item[key]["accountId"] for item in json.loads(raw[raw.index("["):]))


def score_profile(legacy, rest_id):
    score = 0
    reasons = []

    bio = (legacy.get("description") or "").lower()
    name = (legacy.get("name") or "").lower()
    username = (legacy.get("screen_name") or "").lower()
    followers = legacy.get("followers_count", 0)
    following = legacy.get("friends_count", 0)
    tweet_count = legacy.get("statuses_count", 0)
    created_at = legacy.get("created_at", "")
    default_img = legacy.get("default_profile_image", False)
    verified = legacy.get("verified", False)

    for keyword, weight in FARMER_BIO_KEYWORDS.items():
        if keyword in bio:
            score += weight
            reasons.append(f"bio:'{keyword}' (+{weight})")
        if keyword in name:
            score += weight // 2
            reasons.append(f"name:'{keyword}' (+{weight // 2})")

    raw_bio = legacy.get("description") or ""
    emoji_count = sum(1 for e in FARMER_EMOJIS if e in raw_bio)
    if emoji_count >= 3:
        score += 15; reasons.append(f"farmer_emojis:{emoji_count} (+15)")
    elif emoji_count >= 2:
        score += 8; reasons.append(f"farmer_emojis:{emoji_count} (+8)")

    if followers > 0:
        ratio = following / followers
        if ratio > 10:
            score += 30; reasons.append(f"follow_ratio:{ratio:.1f} (+30)")
        elif ratio > 5:
            score += 20; reasons.append(f"follow_ratio:{ratio:.1f} (+20)")
        elif ratio > 3:
            score += 10; reasons.append(f"follow_ratio:{ratio:.1f} (+10)")
    elif following > 100:
        score += 25; reasons.append(f"zero_followers:{following}flng (+25)")

    if following > 5000:
        score += 15; reasons.append(f"mass_following:{following} (+15)")
    elif following > 2000:
        score += 8; reasons.append(f"high_following:{following} (+8)")

    if created_at:
        try:
            created = datetime.strptime(created_at, "%a %b %d %H:%M:%S %z %Y")
            age_days = (datetime.now(timezone.utc) - created).days
            if age_days < 180:
                score += 25; reasons.append(f"very_new:{age_days}d (+25)")
            elif age_days < 365:
                score += 15; reasons.append(f"new:{age_days}d (+15)")
            elif age_days < 730 and created.year >= 2024:
                score += 8; reasons.append(f"kaito_era:{created.year} (+8)")
        except (ValueError, TypeError):
            pass

    if tweet_count < 50 and following > 500:
        score += 20; reasons.append(f"low_tweets:{tweet_count} (+20)")
    elif tweet_count < 100 and following > 1000:
        score += 15; reasons.append(f"low_tweets:{tweet_count} (+15)")

    if default_img:
        score += 20; reasons.append("default_avatar (+20)")

    digit_ratio = sum(c.isdigit() for c in username) / max(len(username), 1)
    if digit_ratio > 0.5:
        score += 15; reasons.append(f"numeric_handle (+15)")
    if username.count("_") >= 3:
        score += 10; reasons.append("multi_underscore (+10)")

    if verified:
        score -= 30; reasons.append("verified (-30)")
    if followers > 10000:
        score -= 20; reasons.append(f"high_followers:{followers} (-20)")
    elif followers > 5000:
        score -= 10; reasons.append(f"decent_followers:{followers} (-10)")

    return {
        "id": rest_id,
        "username": legacy.get("screen_name"),
        "name": legacy.get("name"),
        "bio": (legacy.get("description") or "")[:200],
        "followers": followers,
        "following": following,
        "tweets": tweet_count,
        "created": created_at,
        "score": max(score, 0),
        "reasons": reasons,
        "profile_url": f"https://x.com/{legacy.get('screen_name')}",
    }


class TwitterSession:
    def __init__(self, auth_token, ct0):
        self.session = requests.Session()
        self.session.cookies.set("auth_token", auth_token, domain=".x.com")
        self.session.cookies.set("ct0", ct0, domain=".x.com")
        self.session.headers.update({
            "Authorization": f"Bearer {BEARER}",
            "x-csrf-token": ct0,
            "x-twitter-auth-type": "OAuth2Session",
            "x-twitter-active-user": "yes",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Referer": "https://x.com/",
        })
        self._rate_remaining = 500
        self._rate_reset = 0

    def lookup_user(self, user_id):
        variables = json.dumps({"userId": user_id, "withSafetyModeUserFields": True})
        for attempt in range(3):
            resp = self.session.get(GRAPHQL_USER,
                                   params={"variables": variables, "features": GRAPHQL_FEATURES})

            # Track rate limits
            remaining = resp.headers.get("x-rate-limit-remaining")
            reset = resp.headers.get("x-rate-limit-reset")
            if remaining:
                self._rate_remaining = int(remaining)
            if reset:
                self._rate_reset = int(reset)

            if resp.status_code == 429:
                wait = max(self._rate_reset - int(time.time()), 10)
                print(f"\n  Rate limited. Waiting {wait}s...", flush=True)
                time.sleep(wait)
                continue
            if resp.status_code != 200:
                return None

            data = resp.json()
            result = data.get("data", {}).get("user", {}).get("result", {})
            if result.get("__typename") == "UserUnavailable":
                return None  # suspended/deleted
            legacy = result.get("legacy")
            if not legacy:
                return None
            return score_profile(legacy, user_id)
        return None

    def block_user(self, user_id):
        url = "https://x.com/i/api/1.1/blocks/create.json"
        for attempt in range(3):
            resp = self.session.post(url, data={"user_id": user_id})
            if resp.status_code == 429:
                reset = resp.headers.get("x-rate-limit-reset")
                wait = max(int(reset) - int(time.time()), 10) if reset else 60
                print(f"\n  Block rate limited. Waiting {wait}s...", flush=True)
                time.sleep(wait)
                continue
            return resp.status_code == 200
        return False

    @property
    def rate_remaining(self):
        return self._rate_remaining


def save_progress(all_scored, checked_ids):
    with open(OUTPUT_DIR / "progress.json", "w") as f:
        json.dump({"scored": all_scored, "checked_ids": list(checked_ids),
                    "ts": datetime.now(timezone.utc).isoformat()}, f, default=str)


def load_progress():
    p = OUTPUT_DIR / "progress.json"
    if p.exists():
        data = json.loads(p.read_text())
        return data["scored"], set(data["checked_ids"])
    return [], set()


def main():
    parser = argparse.ArgumentParser(description="Score followers for farmer/yapper signals")
    parser.add_argument("--auth-token", required=True)
    parser.add_argument("--ct0", required=True)
    parser.add_argument("--archive-data", type=Path, default=DATA_DIR,
                        help="Path to your X archive's data/ folder (with follower.js, following.js, block.js)")
    parser.add_argument("--patterns", type=Path, default=None,
                        help="JSON file mapping bio keyword -> score weight (overrides built-in defaults)")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--min-year", type=int, default=None)
    parser.add_argument("--workers", type=int, default=5, help="Concurrent requests")
    args = parser.parse_args()

    if args.patterns:
        global FARMER_BIO_KEYWORDS
        FARMER_BIO_KEYWORDS = json.loads(args.patterns.read_text())
        print(f"Loaded {len(FARMER_BIO_KEYWORDS)} bio patterns from {args.patterns}")

    tw = TwitterSession(args.auth_token, args.ct0)

    # ── Verify auth ──
    print("Verifying auth...", flush=True)
    test = tw.lookup_user("44196397")  # elon
    if not test:
        print("ERROR: Auth failed. Check your cookies.")
        sys.exit(1)
    print(f"  Auth OK (tested lookup of @{test['username']})", flush=True)

    # ── Load archive ──
    archive_dir = args.archive_data
    if not archive_dir.exists():
        print(f"ERROR: archive data dir not found: {archive_dir}")
        print("Pass --archive-data /path/to/your/twitter-archive/data")
        sys.exit(1)
    print(f"Loading archive from {archive_dir}...", flush=True)
    followers = load_ids_from(archive_dir, "follower.js", "follower")
    following = load_ids_from(archive_dir, "following.js", "following")
    blocked = load_ids_from(archive_dir, "block.js", "blocking") if (archive_dir / "block.js").exists() else set()
    candidates = followers - following - blocked
    print(f"  {len(followers)} followers, {len(following)} following, "
          f"{len(blocked)} blocked, {len(candidates)} candidates", flush=True)

    if args.min_year:
        before = len(candidates)
        candidates = {c for c in candidates if id_to_date(int(c)).year >= args.min_year}
        print(f"  Filtered to >= {args.min_year}: {len(candidates)} (was {before})", flush=True)

    # ── Resume ──
    all_scored = []
    checked_ids = set()
    if args.resume:
        all_scored, checked_ids = load_progress()
        print(f"  Resuming: {len(checked_ids)} done, {len(all_scored)} scored", flush=True)

    remaining = sorted(candidates - checked_ids, key=lambda x: int(x), reverse=True)

    if not remaining:
        print("All candidates already checked!", flush=True)
    else:
        print(f"\nScanning {len(remaining)} profiles ({args.workers} workers)...", flush=True)

        done = 0
        farmers = sum(1 for s in all_scored if s["score"] >= 50)
        suspended = 0

        # Process in chunks to manage rate limits
        # 500 requests per 15 min = ~33/min, with 5 workers that's fine
        chunk_size = min(450, len(remaining))  # stay under rate limit
        for chunk_start in range(0, len(remaining), chunk_size):
            chunk = remaining[chunk_start:chunk_start + chunk_size]

            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                futures = {}
                for uid in chunk:
                    futures[pool.submit(tw.lookup_user, uid)] = uid

                for future in as_completed(futures):
                    uid = futures[future]
                    checked_ids.add(uid)
                    done += 1

                    try:
                        result = future.result()
                    except Exception as e:
                        result = None

                    if result:
                        all_scored.append(result)
                        if result["score"] >= 50:
                            farmers += 1
                    else:
                        suspended += 1

                    if done % 50 == 0:
                        print(f"  {done}/{len(remaining)} | "
                              f"farmers: {farmers} | suspended/deleted: {suspended} | "
                              f"rate remaining: {tw.rate_remaining}", flush=True)
                        save_progress(all_scored, checked_ids)

            # If more chunks remain, check rate limit
            if chunk_start + chunk_size < len(remaining):
                if tw.rate_remaining < 100:
                    wait = max(tw._rate_reset - int(time.time()), 30)
                    print(f"  Rate limit low ({tw.rate_remaining}). Waiting {wait}s...", flush=True)
                    time.sleep(wait)

    # ── Save results ──
    all_scored.sort(key=lambda x: x["score"], reverse=True)
    save_progress(all_scored, checked_ids)

    with open(OUTPUT_DIR / "farmer_scores.json", "w") as f:
        json.dump(all_scored, f, indent=2, default=str)

    csv_path = OUTPUT_DIR / "farmer_scores.csv"
    with open(csv_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["score", "username", "name", "bio", "followers", "following",
                     "tweets", "created", "profile_url", "reasons"])
        for s in all_scored:
            w.writerow([s["score"], s["username"], s["name"], s["bio"][:100],
                        s["followers"], s["following"], s["tweets"], s["created"],
                        s["profile_url"], "; ".join(s["reasons"])])

    # ── Summary ──
    print(f"\n{'─' * 60}", flush=True)
    print(f"RESULTS ({len(all_scored)} profiles scored)", flush=True)
    print(f"{'─' * 60}", flush=True)
    for t in [80, 60, 40, 20]:
        print(f"  Score >= {t:3d}: {sum(1 for s in all_scored if s['score'] >= t):>5d}", flush=True)
    print(f"  Score <  20: {sum(1 for s in all_scored if s['score'] < 20):>5d}", flush=True)
    print(f"  Suspended/deleted: {suspended}", flush=True)
    print(f"\n  CSV: {csv_path}", flush=True)

    # ── Preview ──
    print(f"\nTOP 20 HIGHEST-SCORING ACCOUNTS:", flush=True)
    print(f"{'─' * 60}", flush=True)
    for s in all_scored[:20]:
        print(f"  [{s['score']:3d}] @{s.get('username','???'):<20s} | "
              f"{s['followers']:>6d} flrs | {s['following']:>6d} flng | "
              f"{s['bio'][:50]}", flush=True)
    print(f"\nTo block: python3 py/block.py --auth-token ... --ct0 ... "
          f"--scores-file {OUTPUT_DIR / 'farmer_scores.json'} --threshold 50", flush=True)


if __name__ == "__main__":
    main()
