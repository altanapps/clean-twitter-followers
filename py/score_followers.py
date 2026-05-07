#!/usr/bin/env python3
"""
Generic low-quality follower scorer.

Sister to py/score_farmers.py with a different threat model: spam, OF,
signal-sellers, get-rich-quick, follow-back-bots, link-aggregator funnels,
ratio bots — rather than airdrop-farmer / yapper accounts specifically.

Targets non-mutual, non-blocked followers (people who follow you but you do
not follow back, and you have not already blocked). If a previous farmer-
score JSON exists at output/farmer_scores.json, re-uses that profile data
so we only hit the API for followers we have never seen before.

Output is the same shape as farmer_scores.json so py/block.py and
py/block_browser.py can consume it directly via --scores-file.

Run:
  python3 py/score_followers.py --workers 5
  python3 py/block_browser.py \\
      --scores-file output/quality_scores.json \\
      --blocked-log output/quality_blocked_log.json \\
      --threshold 60
"""

import argparse
import csv
import json
import re
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import browser_cookie3
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

BASE = Path(__file__).parent
DATA_DIR = BASE / "data"
OUTPUT_DIR = BASE / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

FARMER_SCORES = OUTPUT_DIR / "farmer_scores.json"
QUALITY_SCORES = OUTPUT_DIR / "quality_scores.json"
QUALITY_CSV = OUTPUT_DIR / "quality_scores.csv"
QUALITY_PROGRESS = OUTPUT_DIR / "quality_progress.json"

BEARER = "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
_GQL_RE = re.compile(r"/graphql/([^/]+)/([^?]+)")


# ── Generic spam / low-quality scoring ─────────────────────────────────────

SPAM_KEYWORDS = {
    # OF / adult spam
    "onlyfans": 50, "only fans": 50, "of model": 45,
    "spicy content": 45, "exclusive content": 30,
    "nudes": 50, "naughty": 25, "kinky": 25,
    "dm for fun": 45, "dm me baby": 50, "dm to play": 45,
    "available now": 20, "hot content": 35,

    # signal-seller / pump-and-dump
    "crypto signals": 50, "trading signals": 50, "free signals": 50,
    "pump signals": 55, "pump alerts": 55, "pump group": 45,
    "100x gem": 35, "1000x gem": 40, "next gem": 30,
    "next 100x": 35, "moonshot": 25,
    "alpha calls": 30, "alpha caller": 25, "gem caller": 30,
    "binary options": 40, "forex signals": 45,
    "forex trader": 25, "fx trader": 20,
    "vip signals": 50, "premium signals": 45,

    # get-rich-quick / hustle bro
    "make money online": 35, "financial freedom": 25,
    "passive income": 25, "earn from home": 35,
    "$10k/month": 30, "$10k a month": 30,
    "side hustle": 12, "money mindset": 15,
    "millionaire mindset": 25, "high ticket": 20,
    "scale your": 12, "6 figures": 15, "7 figures": 20,

    # follow-bait / mass-following
    "follow back": 35, "followback": 35,
    "follow4follow": 40, "f4f": 35,
    "follow for follow": 40, "team follow back": 40, "tfb": 25,
    "ifollowback": 35,

    # link aggregators (when stacked w/ other signals)
    "linktr.ee/": 12, "linkin.bio": 12, "beacons.ai": 12,
    "link in bio": 12, "links in bio": 12,

    # off-platform funnel
    "telegram": 22, "t.me/": 28, "join my telegram": 40,
    "join my discord": 18, "discord.gg/": 12,
    "whatsapp": 25, "wa.me/": 30,

    # collab / DM bait
    "dm for collab": 22, "dm for promo": 30,
    "open for collabs": 18, "promo dm": 30,
    "available for promo": 25, "promo enquiries": 20,
}

SPAM_EMOJIS = ["💋", "💦", "🍑", "🔞", "💸", "💰", "💵", "🤑", "📈", "🚀", "🔥"]


# ── Scoring ────────────────────────────────────────────────────────────────

def parse_created(created_at):
    if not created_at:
        return None
    try:
        return datetime.strptime(created_at, "%a %b %d %H:%M:%S %z %Y")
    except (ValueError, TypeError):
        return None


def score_quality(profile):
    """
    Generic spam / low-quality scorer. Operates on the dict shape produced by
    score_farmers.score_profile() *or* a freshly-built profile dict.

    Required: bio, name, username, followers, following, tweets, created.
    Optional: default_profile_image, verified.
    """
    score = 0
    reasons = []

    bio = (profile.get("bio") or "").lower()
    name = (profile.get("name") or "").lower()
    username = (profile.get("username") or "").lower()
    followers = profile.get("followers") or 0
    following = profile.get("following") or 0
    tweet_count = profile.get("tweets") or 0
    created_at = profile.get("created") or ""
    default_img = profile.get("default_profile_image", False)
    verified = profile.get("verified", False)

    # ── bio / name keyword scan ──
    for kw, weight in SPAM_KEYWORDS.items():
        if kw in bio:
            score += weight
            reasons.append(f"bio:'{kw}' (+{weight})")
        if len(kw) > 4 and kw in name:
            half = weight // 2
            score += half
            reasons.append(f"name:'{kw}' (+{half})")

    # ── spammy emoji clusters ──
    raw_bio = profile.get("bio") or ""
    emoji_count = sum(1 for e in SPAM_EMOJIS if e in raw_bio)
    if emoji_count >= 4:
        score += 20; reasons.append(f"spam_emojis:{emoji_count} (+20)")
    elif emoji_count >= 3:
        score += 12; reasons.append(f"spam_emojis:{emoji_count} (+12)")

    # ── follow ratio ──
    if followers > 0:
        ratio = following / followers
        if ratio > 10:
            score += 30; reasons.append(f"ratio:{ratio:.1f} (+30)")
        elif ratio > 5:
            score += 20; reasons.append(f"ratio:{ratio:.1f} (+20)")
        elif ratio > 3:
            score += 10; reasons.append(f"ratio:{ratio:.1f} (+10)")
    elif following > 100:
        score += 25; reasons.append(f"zero_followers ({following} flng) (+25)")

    # ── mass following ──
    if following > 5000:
        score += 15; reasons.append(f"mass_following:{following} (+15)")
    elif following > 2000:
        score += 8; reasons.append(f"high_following:{following} (+8)")

    # ── tweet/follow imbalance (lurker bot) ──
    if tweet_count < 20 and following > 500:
        score += 25; reasons.append(f"silent_bot:{tweet_count}t/{following}f (+25)")
    elif tweet_count < 100 and following > 1000:
        score += 15; reasons.append(f"low_tweets:{tweet_count} (+15)")

    # ── account age ──
    created = parse_created(created_at)
    if created:
        age_days = (datetime.now(timezone.utc) - created).days
        if age_days < 90:
            score += 25; reasons.append(f"very_new:{age_days}d (+25)")
        elif age_days < 365:
            score += 12; reasons.append(f"new:{age_days}d (+12)")
        elif age_days > 5 * 365:
            score -= 10; reasons.append(f"old_account:{age_days // 365}y (-10)")

    # ── default avatar (only known on fresh fetches) ──
    if default_img:
        score += 20; reasons.append("default_avatar (+20)")

    # ── handle pattern ──
    if username:
        digit_ratio = sum(c.isdigit() for c in username) / len(username)
        if digit_ratio > 0.5:
            score += 15; reasons.append("numeric_handle (+15)")
        if username.count("_") >= 3:
            score += 10; reasons.append("multi_underscore (+10)")

    # ── empty bio + many follows ──
    if not bio.strip() and following > 500:
        score += 10; reasons.append("empty_bio + mass_follow (+10)")

    # ── negatives ──
    if verified:
        score -= 40; reasons.append("verified (-40)")
    if followers > 10000:
        score -= 30; reasons.append(f"big_account:{followers} (-30)")
    elif followers > 5000:
        score -= 15; reasons.append(f"decent_account:{followers} (-15)")

    return max(score, 0), reasons


# ── Twitter session ────────────────────────────────────────────────────────

def build_profile(user_result, rest_id):
    """Build profile dict from GraphQL UserByRestId result.
    X moved screen_name/name/created_at from legacy to core."""
    legacy = user_result.get("legacy", {}) or {}
    core = user_result.get("core", {}) or {}
    screen_name = core.get("screen_name") or legacy.get("screen_name")
    return {
        "id": rest_id,
        "username": screen_name,
        "name": core.get("name") or legacy.get("name"),
        "bio": (legacy.get("description") or "")[:500],
        "followers": legacy.get("followers_count", 0),
        "following": legacy.get("friends_count", 0),
        "tweets": legacy.get("statuses_count", 0),
        "created": core.get("created_at") or legacy.get("created_at", ""),
        "default_profile_image": legacy.get("default_profile_image", False),
        "verified": user_result.get("is_blue_verified", False) or legacy.get("verified", False),
        "profile_url": f"https://x.com/{screen_name}",
    }


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


# ── Persistence ────────────────────────────────────────────────────────────

def load_archive_ids(data_dir, filename, key):
    with open(data_dir / filename) as f:
        raw = f.read()
    return set(item[key]["accountId"] for item in json.loads(raw[raw.index("["):]))


def load_progress():
    if QUALITY_PROGRESS.exists():
        d = json.loads(QUALITY_PROGRESS.read_text())
        return d.get("scored", []), set(d.get("checked_ids", []))
    return [], set()


def save_progress(scored, checked):
    QUALITY_PROGRESS.write_text(json.dumps(
        {"scored": scored, "checked_ids": list(checked),
         "ts": datetime.now(timezone.utc).isoformat()},
        default=str))


def load_farmer_cache():
    if not FARMER_SCORES.exists():
        return {}
    return {e["id"]: e for e in json.loads(FARMER_SCORES.read_text())}


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Generic low-quality follower scorer")
    parser.add_argument("--limit", type=int, default=None,
                        help="Only fetch the first N un-cached candidates")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--skip-cached", action="store_true",
                        help="Do not re-score profiles already in farmer_scores.json")
    parser.add_argument("--visible", action="store_true",
                        help="Run browser head-ful")
    parser.add_argument("--archive-data", type=Path, default=DATA_DIR,
                        help="Path to your X archive's data/ folder")
    args = parser.parse_args()

    # ── Load archive ──
    if not args.archive_data.exists():
        print(f"ERROR: archive data dir not found: {args.archive_data}")
        print("Pass --archive-data /path/to/your/twitter-archive/data")
        sys.exit(1)
    print(f"Loading archive from {args.archive_data}...", flush=True)
    followers = load_archive_ids(args.archive_data, "follower.js", "follower")
    following = load_archive_ids(args.archive_data, "following.js", "following")
    blocked = (load_archive_ids(args.archive_data, "block.js", "blocking")
               if (args.archive_data / "block.js").exists() else set())
    candidates = followers - following - blocked
    print(f"  followers={len(followers)} following={len(following)} "
          f"blocked={len(blocked)} candidates={len(candidates)}", flush=True)

    cache = load_farmer_cache()
    print(f"  farmer cache: {len(cache)} profiles available", flush=True)

    scored = []
    checked = set()
    if args.resume:
        scored, checked = load_progress()
        print(f"  Resuming: {len(checked)} done, {len(scored)} kept", flush=True)

    # ── Phase 1: re-score everything we already have cached (no API) ──
    cached_to_score = [c for c in candidates if c in cache and c not in checked]
    if not args.skip_cached and cached_to_score:
        print(f"\nPhase 1: scoring {len(cached_to_score)} cached profiles offline...",
              flush=True)
        for cid in cached_to_score:
            entry = cache[cid]
            s, reasons = score_quality(entry)
            scored.append({
                "id": cid,
                "username": entry.get("username"),
                "name": entry.get("name"),
                "bio": (entry.get("bio") or "")[:500],
                "followers": entry.get("followers", 0),
                "following": entry.get("following", 0),
                "tweets": entry.get("tweets", 0),
                "created": entry.get("created", ""),
                "score": s,
                "reasons": reasons,
                "profile_url": entry.get("profile_url",
                                         f"https://x.com/{entry.get('username')}"),
                "source": "cache",
            })
            checked.add(cid)
        save_progress(scored, checked)
        flagged = sum(1 for x in scored if x["score"] >= 50)
        print(f"  Phase 1 done. flagged>=50: {flagged}", flush=True)

    # ── Phase 2: fetch + score uncached via Playwright ──
    uncached = sorted(candidates - checked, key=lambda x: int(x), reverse=True)
    if args.limit:
        uncached = uncached[:args.limit]

    if not uncached:
        print("\nNothing left to fetch.", flush=True)
    else:
        print(f"\nPhase 2: fetching {len(uncached)} uncached profiles...", flush=True)

        print("Loading cookies from Chrome...", flush=True)
        cookies, ct0 = get_chrome_cookies()
        if not ct0:
            print("ERROR: ct0 cookie not found. Log into x.com in Chrome first.")
            sys.exit(1)
        print(f"  Loaded {len(cookies)} cookies", flush=True)

        with Stealth().use_sync(sync_playwright()) as p:
            browser = p.chromium.launch(
                headless=not args.visible,
                channel="chrome",
                args=["--disable-blink-features=AutomationControlled"],
            )
            context = browser.new_context(
                viewport={"width": 1280, "height": 900}, locale="en-US",
            )
            context.add_cookies(cookies)
            page = context.new_page()

            # Capture GraphQL query IDs from natural navigation
            captured_queries = {}
            captured_features = {}

            def on_request(req):
                m = _GQL_RE.search(req.url)
                if not m:
                    return
                qid, qname = m.group(1), m.group(2)
                captured_queries[qname] = qid
                if "features=" in req.url:
                    try:
                        qs = urllib.parse.urlparse(req.url).query
                        params = urllib.parse.parse_qs(qs)
                        if "features" in params:
                            captured_features[qname] = params["features"][0]
                    except Exception:
                        pass

            page.on("request", on_request)

            print("Verifying session...", flush=True)
            page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=30000)
            time.sleep(3)
            if "login" in page.url.lower():
                print("ERROR: session invalid")
                browser.close()
                sys.exit(1)
            print(f"  Session OK ({page.url})", flush=True)

            # Harvest UserByRestId query ID
            print("Harvesting GraphQL query IDs...", flush=True)
            seed_id = uncached[0]
            try:
                page.goto(f"https://x.com/i/user/{seed_id}",
                          wait_until="domcontentloaded", timeout=30000)
                time.sleep(5)
            except Exception as e:
                print(f"  seed nav warn: {e}")
            try:
                page.goto("https://x.com/elonmusk",
                          wait_until="domcontentloaded", timeout=30000)
                time.sleep(4)
            except Exception:
                pass

            ubri = captured_queries.get("UserByRestId")
            if not ubri:
                print("  UserByRestId not captured. Got:")
                for k, v in sorted(captured_queries.items()):
                    print(f"    {k} -> {v}")
                browser.close()
                sys.exit(2)
            print(f"  UserByRestId query id = {ubri}", flush=True)

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

            done = 0
            suspended = 0
            errors = 0
            t0 = time.time()

            for i, uid in enumerate(uncached, 1):
                variables = json.dumps({"userId": uid, "withSafetyModeUserFields": True})
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
                    print(f"  [{i}] {uid}: EXC {type(e).__name__}", flush=True)
                    time.sleep(2)
                    checked.add(uid)
                    done += 1
                    continue

                status = resp.get("status", 0)
                body = resp.get("body", "")

                if status == 200:
                    try:
                        data = json.loads(body)
                        user = data.get("data", {}).get("user", {}).get("result", {})
                        if not user or user.get("__typename") == "UserUnavailable":
                            suspended += 1
                        else:
                            prof = build_profile(user, uid)
                            s, reasons = score_quality(prof)
                            prof["score"] = s
                            prof["reasons"] = reasons
                            prof["source"] = "fresh"
                            scored.append(prof)
                    except Exception as e:
                        errors += 1
                elif status == 429:
                    print("  Rate limited — sleeping 60s", flush=True)
                    save_progress(scored, checked)
                    time.sleep(60)
                    checked.add(uid)
                    done += 1
                    continue
                elif status in (401, 403):
                    print(f"  AUTH FAILED (status {status}). Saving and exiting.", flush=True)
                    save_progress(scored, checked)
                    browser.close()
                    sys.exit(2)
                else:
                    errors += 1

                checked.add(uid)
                done += 1

                if done % 25 == 0 or done == len(uncached):
                    elapsed = time.time() - t0
                    rate = done / elapsed if elapsed else 0
                    eta = (len(uncached) - done) / rate if rate else 0
                    flagged = sum(1 for x in scored if x["score"] >= 50)
                    print(f"  [{done}/{len(uncached)}] flagged>=50: {flagged} | "
                          f"suspended: {suspended} | err: {errors} | "
                          f"{rate:.1f}/s eta {eta/60:.0f}m", flush=True)
                    save_progress(scored, checked)

                # Re-warm session periodically
                if done % 200 == 0:
                    try:
                        page.goto("https://x.com/home",
                                  wait_until="domcontentloaded", timeout=20000)
                        time.sleep(2)
                    except Exception:
                        pass

                time.sleep(0.3)

            browser.close()

    # ── Save ──
    scored.sort(key=lambda x: x["score"], reverse=True)
    save_progress(scored, checked)
    QUALITY_SCORES.write_text(json.dumps(scored, indent=2, default=str))

    with open(QUALITY_CSV, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["score", "username", "name", "bio", "followers", "following",
                    "tweets", "created", "source", "profile_url", "reasons"])
        for s in scored:
            w.writerow([s["score"], s["username"], s["name"],
                        (s.get("bio") or "")[:120],
                        s["followers"], s["following"], s["tweets"], s["created"],
                        s.get("source", "?"), s["profile_url"],
                        "; ".join(s["reasons"])])

    print(f"\n{'─' * 60}")
    print(f"RESULTS ({len(scored)} profiles)")
    print(f"{'─' * 60}")
    for t in [80, 60, 50, 40, 20]:
        print(f"  >= {t:3d}: {sum(1 for x in scored if x['score'] >= t):>5d}")
    print(f"  CSV:  {QUALITY_CSV}")
    print(f"  JSON: {QUALITY_SCORES}")

    print("\nTOP 20:")
    for s in scored[:20]:
        print(f"  [{s['score']:3d}] @{(s.get('username') or '?'):<22s} | "
              f"{s['followers']:>6d}f | {s['following']:>6d}fl | "
              f"{(s.get('bio') or '')[:55]}")

    print("\nNext: review the CSV, then block with:")
    print("  python3 block_farmers_browser.py \\")
    print(f"      --scores-file {QUALITY_SCORES.relative_to(BASE)} \\")
    print("      --blocked-log output/quality_blocked_log.json \\")
    print("      --threshold 60")


if __name__ == "__main__":
    main()
