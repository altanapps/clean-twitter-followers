# clean-twitter-followers

Triage 1,000+ X (Twitter) follows in a keyboard-first local app, then batch-unfollow at a paced, human-like rate. No API keys, no SaaS, no data leaves your machine.

Built because X's native "Following" page is useless for bulk cleanup: no filters, no sort, no bulk actions, and you can't see who follows you back without clicking every profile.

```
┌───────────────────────────────────────────────────────────────────────────────────┐
│ Clean Twitter Follows                                       1855 of 1858 shown   │
│ Your goal:  [building a B2B product for indie devs______________]                │
│ ☐ Not mutual  ☐ Dead  ☐ Oldest 500  ☐ Low activity  ☐ No avatar  ☐ Empty bio      │
│ ☐ Recently followed  ☐ Paid blue, not mutual  ☐ Bot ratio  ☐ One-way celeb        │
│ [search…]  [exclude bio keywords…]    pace [200]/hr   ☐ dry run   [Unfollow 412] │
├───────────────────────────────────────────────────────────────────────────────────┤
│ ☑│🖼 @deadacct   │web3 community manager…  │ —    │  42 │  12 │ 1803             │
│ ☐│🖼 @friend     │building useful software │ mut  │4.2K │ 420 │   58             │
│ ☑│🖼 @randominf  │growth ⚡ threads every… │ —    │89.0K│18.0K│  412             │
└───────────────────────────────────────────────────────────────────────────────────┘
● Unfollowing… 127 / 412 processed · 3 failed                       [Stop]
```

> **ToS notice.** Automated unfollowing violates X's Developer Agreement. This tool runs locally against your own logged-in session, paces actions at human-like rates, and caps at 1,000/day by default — but the risk is yours. Aged accounts with clean history have meaningful headroom; fresh/suspicious accounts have much less. Don't use this on an account you can't afford to lose.

## Features

- **Scrape** your full following list via the same GraphQL endpoints X's own UI uses — no official API, no rate-limit accounting. Typically 3-5 minutes for 1,500-2,000 follows.
- **Triage** in a keyboard-first table: sort/filter/bulk-select across 1,800+ rows with no lag.
- **Paced unfollow** as a detached background job, with start/stop/resume from the UI. Progress persists across browser reloads and server restarts.
- **Filters** for dead accounts, ghost follows, celebrity one-ways, bot-ratio mass-followers, paid-blue spam, and more.
- **Custom bio keyword exclusion** — hide accounts whose bio contains terms you don't care about ("crypto, NFT, trader").
- **Daily cap** (1,000/day by default) auto-pauses the job when X's soft-limit territory is approaching.
- **Dry-run mode** to simulate a full run before going live.

## Prerequisites

- Node 20 or newer
- npm
- A modern Mac/Linux (Windows probably works but untested)

## Setup

```bash
git clone <this repo>
cd clean-twitter-followers
npm install
npx playwright install chromium
```

## 1. Scrape your follows

```bash
npm run sweep
```

A Chrome window opens on first run — log into X. The login is cached in `data/playwright-profile/` so future runs skip this step. The script then navigates to your Following page and scrolls until exhausted, intercepting the GraphQL responses. Output: `data/follows.json`.

If you have more than ~1,000 follows and the first run stops short, wait a few hours and re-run — X's pagination window sometimes caps mid-list. The sweep dedupes, so re-runs are additive.

## 2. Triage + unfollow

```bash
npm run dev
```

Opens `http://localhost:5173`. The page loads your follows and presents them as a sortable, filterable table.

### Filters

| Filter | What it does |
|---|---|
| Not mutual | Hide accounts that follow you back |
| Dead (<10 tweets) | Accounts that barely ever posted |
| Oldest 500 follows | Bottom of your following list — stale intentional follows |
| Low activity (<100 tweets) | Low-effort or abandoned accounts |
| No avatar | Default egg profile picture — near-guaranteed dead or spam |
| Empty bio | Low-effort accounts |
| Recently followed (last 50) | Re-evaluate your recent impulse follows |
| Paid blue, not mutual | Verified accounts that don't follow you back — usually algorithmic-boost chasers |
| Bot ratio | Accounts that follow >10× more people than follow them back |
| One-way celeb | >100K followers, not mutual — celebrities you followed once |
| Exclude bio keywords | Free-text input — hide any bio matching comma-separated terms |

### Keyboard shortcuts

| Key | Action |
|-----|--------|
| `j` / `k` | Move cursor up/down |
| `x` / `space` | Toggle selection on current row |
| `a` | Select every row currently passing filters |
| `A` | Deselect everything |
| `/` | Focus the search field |
| `e` | Download `cuts.json` (optional — app saves it on "Unfollow" automatically) |
| `Enter` | Open current profile in a new tab |

### Running the unfollow

1. Pick your filter(s), select rows (or press `a` to select-all-filtered)
2. Set the pace — default is **200/hr**, range 30-600
3. Optionally check **dry run** to simulate
4. Click **Unfollow N selected** and confirm

The app saves your cut list to `data/cuts.json` and spawns the unfollow script as a detached background process. The status bar at the bottom shows live progress: `Unfollowing… 127 / 412 processed · 3 failed`. Click **Stop** at any time — progress is preserved.

Accounts that have already been unfollowed disappear from the list automatically.

### Pacing defaults

| Setting | Default | Env var override |
|---|---|---|
| Rate | 200/hour | `UNFOLLOW_PER_HOUR` |
| Daily cap | 1,000/day | `UNFOLLOW_DAILY_MAX` |
| Session length | 90 min | `UNFOLLOW_SESSION_MIN` |
| Session break | 30-60 min random | hardcoded |
| Jitter between actions | 8-25 sec random | hardcoded |
| Allowed hours | 9:00-24:00 local | hardcoded |

For 800 cuts at defaults → ~1 day elapsed (hit daily cap once). For 2,000 cuts → ~2 days.

Override examples:

```bash
UNFOLLOW_PER_HOUR=120 npm run dev      # more cautious
UNFOLLOW_DAILY_MAX=500 npm run dev     # smaller daily bites
```

## Files

```
sweep.ts       # Playwright scraper — read-only, harvests your following list
server.ts      # Local HTTP server (port 5173) — serves UI + manages unfollow jobs
unfollow.ts    # Paced write job — normally spawned by server, can run standalone via `npm run unfollow`
index.html     # Single-file triage UI
data/          # follows.json, cuts.json, unfollow-progress.json, unfollow.pid, unfollow.log, playwright-profile/
               # entire folder is gitignored
```

## Troubleshooting

**"EPERM" or Chromium doesn't open** — run `npx playwright install chromium` to re-install the browser.

**Sweep stops at ~1,000 even though you have more** — X caps pagination in a single session. Wait a few hours, re-run. Sweeps are additive.

**DOM selectors stop working after X ships a redesign** — open an issue with the broken step; selectors in `sweep.ts` and `unfollow.ts` are the usual suspects.

**Stop button seems to hang** — Playwright's chromium takes a few seconds to exit cleanly. If it really resists, the server will SIGKILL after 10s.

**Account got write-restricted / soft-limited** — lower the pace with `UNFOLLOW_PER_HOUR=80`, or just stop and try again tomorrow. Soft-limits usually clear in 12-24 hours.

## Roadmap

Open an issue or PR if any of these would be useful:

- [ ] Enrich with last-tweet date (per-profile sweep, ~30 min extra scrape time)
- [ ] Parse your X archive for interaction history (replies, likes, DMs) for a real engagement score
- [ ] Optional LLM pass for bio-vs-goal relevance scoring
- [ ] Browser-extension build (scrape + triage in-page, no local server)
- [ ] Follower-side version — clean who follows you

## License

MIT. Use it, fork it, break it, fix it.

---

Built in a weekend. Not a business.
