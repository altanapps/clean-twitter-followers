# clean-twitter-followers

Toolkit for cleaning your X (Twitter) graph at scale. Local-only. No API keys, no SaaS.

Two layers, pick the one that fits:

- **Python CLI toolkit** (`py/`) — for full follow graphs (>1,000 follows). Imports your X archive, scores followers, paced unfollow + block, all resumable. Nine focused scripts, glued by CSV and JSON files in `output/`.
- **TypeScript triage UI** (`ts/`) — keyboard-first table for hand-review. Best when your follow graph fits in one screen of scrolling, or for the final approve-this-batch step before unfollowing.

> **ToS notice.** Automated unfollowing/blocking violates X's Developer Agreement. Everything here runs locally against your own logged-in session and paces actions at human-like rates, but the risk is on you. Don't use on an account you can't afford to lose.

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
