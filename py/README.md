# Python toolkit

CLI tools that operate on your X archive + your live X session.

## Setup

```bash
cd py
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

You also need to be logged into x.com in **Chrome** (the toolkit reads
your session cookies via `browser_cookie3`). Two scripts also accept
`--auth-token` and `--ct0` directly — extract those from Chrome devtools
> Application > Cookies > x.com.

## Pipelines

### A) Trim your follow list (paced unfollow)

```bash
# 1. Pull your follow IDs from the archive (full list, not capped like live UI)
python3 import_archive.py --archive ~/Downloads/twitter-archive

# 2. Resolve IDs → handles + follower counts; also writes a "small accounts" CSV
python3 resolve_handles.py --max-followers 50000

# 3. (Optional) review output/unfollow_candidates.csv in your editor

# 4. Paced unfollow — run with --dry-run first
python3 unfollow.py --csv output/unfollow_candidates.csv --dry-run
python3 unfollow.py --csv output/unfollow_candidates.csv --limit 50
```

### B) Block low-quality / farmer accounts among your *followers*

```bash
# 1. Score follower bios for farmer signals
python3 score_farmers.py \
    --auth-token <YOUR_AUTH_TOKEN> --ct0 <YOUR_CT0> \
    --archive-data ~/Downloads/twitter-archive/data \
    --patterns ../examples/farmer_patterns.json

# 2. Block above threshold
python3 block.py --auth-token <...> --ct0 <...> --threshold 50

#    OR via Playwright (more robust if the API path 401s):
python3 block_browser.py --threshold 50
```

### C) Score followers for generic spam (OF, signal-sellers, follow-bait)

```bash
python3 score_followers.py --archive-data ~/Downloads/twitter-archive/data
python3 block_browser.py --scores-file output/quality_scores.json --threshold 60
```

### D) Audit + delete crypto-coded tweets in your post history

```bash
# 1. See what categories of language you've used
python3 analyze_tweets.py \
    --tweets ~/Downloads/twitter-archive/data/tweets.js \
    --categories ../examples/tweet_categories.example.json

# 2. Find tweets matching a pattern set; writes a review CSV
python3 delete_tweets_by_pattern.py \
    --tweets ~/Downloads/twitter-archive/data/tweets.js \
    --patterns ../examples/crypto_patterns.json

# 3. Edit output/matched.csv: set approve_delete=1 on rows you want gone
# 4. Apply (always run with --dry-run first)
python3 delete_tweets_by_pattern.py --apply --csv output/matched.csv --dry-run
python3 delete_tweets_by_pattern.py --apply --csv output/matched.csv --limit 50
```

## Scripts

| Script | Purpose |
|---|---|
| `import_archive.py` | Parse `following.js` / `follower.js` from a Twitter archive → ID list |
| `resolve_handles.py` | Resolve IDs → handles + follower counts via X's GraphQL UserByRestId |
| `score_farmers.py` | Score followers for airdrop/farmer signals (bio keyword weights) |
| `score_followers.py` | Score followers for generic spam/OF/signal-seller patterns |
| `block.py` | Block accounts above score threshold (requests-based) |
| `block_browser.py` | Block accounts above score threshold (Playwright fallback) |
| `unfollow.py` | Paced unfollow from a CSV of targets |
| `analyze_tweets.py` | Read-only keyword frequency report on your tweet history |
| `delete_tweets_by_pattern.py` | Find + delete tweets matching regex patterns |

## Conventions

- All output goes to `py/output/` (gitignored).
- Scripts that touch X are resumable: kill, fix, re-run.
- Default rate-limit handling: catch 429, sleep, continue.
- ToS reminder: automating X actions violates the Developer Agreement.
  These scripts pace conservatively but the risk is on you. Don't run on
  an account you can't lose.
