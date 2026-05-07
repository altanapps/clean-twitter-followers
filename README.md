# clean-twitter-followers

Toolkit for cleaning your X (Twitter) graph at scale. Local-only. No API keys, no SaaS.

Two layers, pick the one that fits:

- **Python CLI toolkit** (`py/`) — for full follow graphs (>1,000 follows). Imports your X archive, scores followers, paced unfollow + block, all resumable. Nine focused scripts, glued by CSV and JSON files in `output/`.
- **TypeScript triage UI** (`ts/`) — keyboard-first table for hand-review. Best when your follow graph fits in one screen of scrolling, or for the final approve-this-batch step before unfollowing.

> **ToS notice.** Automated unfollowing/blocking violates X's Developer Agreement. Everything here runs locally against your own logged-in session and paces actions at human-like rates, but the risk is on you. Don't use on an account you can't afford to lose.

---

## What you need before running anything

**1. Your X archive** (one-time, takes ~24h)

- x.com → Settings → Your account → **Download an archive of your data**
- Confirm with your password; wait for the email (24-48h is normal).
- The .zip unpacks to a folder named `twitter-YYYY-MM-DD-<hash>`. Either rename it to something stable or pass the full path.

**2. Chrome, logged into x.com** (for any script that talks to X)

- The toolkit reads your X session cookies via `browser_cookie3`. No login flow inside the scripts — you log in once, in Chrome, and stay logged in.
- On first run macOS will prompt you to allow keychain access (Chrome's cookie file is keychain-encrypted). Click Always Allow once or you'll be re-prompted on every script invocation.
- If you log out of x.com, your `ct0` cookie rotates → in-flight scripts will start 401-ing. Just log back in and re-run; everything is resumable.

**3. Two cookie values for `score_farmers.py` and `block.py`** (the requests-based path)

These two scripts use `requests` directly instead of Playwright, so they need the cookies passed explicitly:

- Open x.com in Chrome → DevTools → **Application** → **Cookies** → `https://x.com`
- Copy the values of `ct0` and `auth_token`
- Pass them: `--auth-token <value> --ct0 <value>`
- They rotate every few hours. If a long run starts 401-ing, re-grab.

(`score_followers.py`, `block_browser.py`, `unfollow.py`, `resolve_handles.py`, and `delete_tweets_by_pattern.py` use the Playwright path and pull cookies automatically.)

**4. Python 3.10+ and Node 20+**

```bash
cd py
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium     # ~150 MB, one-time

cd ../ts
npm install
npx playwright install chromium # only needed if you'll use the TS UI
```

**5. Don't run write-actions on an account you can't lose**

`unfollow.py`, `block.py`, `block_browser.py`, and `delete_tweets_by_pattern.py --apply` modify your account irreversibly. They also violate X's Developer Agreement (your risk, not ours). Aged accounts with clean history have meaningful headroom; new accounts get write-restricted fast.

## First-run smoke test (5 minutes, no irreversible actions)

```bash
cd py && source .venv/bin/activate

# 1. Pull your follow IDs from your archive
python3 import_archive.py --archive /path/to/your/twitter-archive
# Expected: "Wrote N following IDs" where N matches your real follow count

# 2. Resolve just the first 5 to confirm your X session works
head -5 output/ids.txt > output/test_ids.txt
python3 resolve_handles.py \
    --input output/test_ids.txt \
    --output output/test_resolved.json \
    --csv output/test_resolved.csv \
    --candidates-csv output/test_candidates.csv \
    --max-followers 9999999999
# Expected: 5 entries with handles + follower counts

# 3. Dry-run unfollow against those 5
python3 unfollow.py --csv output/test_candidates.csv --dry-run
# Expected: "would unfollow @handle (N followers)" lines, no actual unfollows
```

If steps 2 and 3 both succeed, your environment is correctly wired up. Now you can run the full pipeline with confidence.

---

## Why this exists

X's "Following" page is hostile to bulk cleanup:
- No filters, no sort, no bulk actions.
- Live pagination caps at ~1,000-1,500 even if you follow 5,000.
- You can't see who follows you back without clicking every profile.

The only way to get your *complete* follow graph is to download your X archive (Settings → Your account → Download an archive of your data; takes ~24h to be ready). This toolkit is built around that fact.

---

## Two paths

### Big graphs (>1,000 follows): start with the archive

```bash
cd py
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium

# Pull the full follow list (live scrape would miss most of it)
python3 import_archive.py --archive ~/Downloads/twitter-archive

# Resolve IDs -> handles + follower counts; writes a CSV of <50k-followers candidates
python3 resolve_handles.py --max-followers 50000

# Paced unfollow (always dry-run first)
python3 unfollow.py --csv output/unfollow_candidates.csv --dry-run
python3 unfollow.py --csv output/unfollow_candidates.csv --limit 50
```

Other Python pipelines (block farmers among followers, audit your own tweet history): see [py/README.md](py/README.md).

### Small graphs (<1,000 follows): use the TS UI

```bash
cd ts
npm install
npx playwright install chromium
npm run sweep        # opens browser, scrolls Following, captures GraphQL
npm run dev          # opens triage UI on http://localhost:5173
```

The UI gives you a keyboard-first table over your follow list with filters: not-mutual, dead, oldest 500, low activity, paid-blue-not-mutual, bot-ratio, one-way celebrity, custom bio-keyword excludes. Bulk-select, set a pace (default 200/hr, 1,000/day cap), confirm. Unfollow runs as a detached background job with start/stop/resume.

### Hybrid: Python for ingest, TS for triage

If you want the keyboard-first UI but with the *full* archive-derived list (not the truncated live scrape), let `resolve_handles.py` produce the data, then point the TS UI at it. Wiring this up is on the roadmap — open an issue if you want it sooner.

---

## What's where

```
clean-twitter-followers/
├── py/                              # Python CLI toolkit
│   ├── import_archive.py            # archive → ID list
│   ├── resolve_handles.py           # IDs → handles + follower counts
│   ├── score_farmers.py             # score followers for farmer/yapper signals
│   ├── score_followers.py           # score followers for generic spam/OF/signal-seller
│   ├── block.py                     # block above score threshold (requests)
│   ├── block_browser.py             # block above score threshold (Playwright)
│   ├── unfollow.py                  # paced unfollow from CSV
│   ├── analyze_tweets.py            # keyword frequency report on tweets.js
│   ├── delete_tweets_by_pattern.py  # find + delete tweets matching regex patterns
│   ├── requirements.txt
│   └── README.md                    # CLI reference
│
├── ts/                              # TypeScript triage UI
│   ├── server.ts                    # localhost:5173, jobs API
│   ├── sweep.ts                     # live-scrape Playwright harvester
│   ├── unfollow.ts                  # browser-driven paced unfollow
│   ├── index.html                   # single-file triage UI
│   └── package.json
│
└── examples/                        # editable defaults you can swap in
    ├── farmer_patterns.json         # bio keyword → score weight (for score_farmers.py)
    ├── tweet_categories.example.json # for analyze_tweets.py
    ├── crypto_patterns.json         # for delete_tweets_by_pattern.py
    └── following.js.sample          # what your archive's following.js looks like
```

---

## What this is *not*

- Not a hosted service. There is no signup, no server you log into.
- Not an API client. There are no API keys to manage. Everything goes through your existing Chrome session.
- Not safe for fresh / suspicious accounts. Aged accounts with clean history have meaningful headroom; new ones get write-restricted faster.

## License

MIT. Use it, fork it, break it, fix it.
