#!/usr/bin/env python3
"""
Surface keyword/pattern frequencies in your X archive's tweets.js.
Read-only. Useful before running delete_tweets_by_pattern.py — see what's in
your post history before you decide what to remove.

Categories of keywords to scan for are loaded from a JSON file (see
examples/tweet_categories.example.json for format). The same JSON can be
fed to delete_tweets_by_pattern.py.

Run:
  python3 py/analyze_tweets.py \\
      --tweets /path/to/twitter-archive/data/tweets.js \\
      --categories examples/tweet_categories.example.json
"""

import argparse
import csv
import json
import re
import sys
from collections import Counter
from pathlib import Path

OUTPUT_DIR = Path(__file__).parent / "output"
OUTPUT_DIR.mkdir(exist_ok=True)


def load_tweets(path):
    raw = path.read_text()
    return json.loads(raw[raw.find("["):])


def contains_any(text_low, words):
    hits = []
    for w in words:
        if " " in w or not w.isalnum():
            if w in text_low:
                hits.append(w)
        else:
            if re.search(r"\b" + re.escape(w) + r"\b", text_low):
                hits.append(w)
    return hits


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tweets", type=Path, required=True,
                        help="Path to your archive's data/tweets.js")
    parser.add_argument("--categories", type=Path, required=True,
                        help="JSON file mapping category name -> list of keywords")
    parser.add_argument("--report-csv", type=Path,
                        default=OUTPUT_DIR / "keyword_frequency.csv")
    args = parser.parse_args()

    if not args.tweets.exists():
        print(f"Missing {args.tweets}")
        sys.exit(1)
    if not args.categories.exists():
        print(f"Missing {args.categories}")
        sys.exit(1)

    tweets = load_tweets(args.tweets)
    categories = json.loads(args.categories.read_text())
    print(f"Loaded {len(tweets):,} tweets and {len(categories)} categories")

    cat_counts = Counter()
    keyword_counts = Counter()
    cat_examples = {k: [] for k in categories}
    is_rt = 0
    is_reply = 0

    for t in tweets:
        tw = t.get("tweet", t)
        text = tw.get("full_text", "")
        low = text.lower()
        if text.startswith("RT @"):
            is_rt += 1
        if tw.get("in_reply_to_status_id_str"):
            is_reply += 1

        for cat, kws in categories.items():
            hits = contains_any(low, kws)
            if hits:
                cat_counts[cat] += 1
                for h in hits:
                    keyword_counts[(cat, h)] += 1
                if len(cat_examples[cat]) < 5:
                    cat_examples[cat].append(text[:180])

    print(f"\n=== OVERVIEW ===")
    print(f"Total tweets:  {len(tweets)}")
    print(f"Retweets:      {is_rt}")
    print(f"Replies:       {is_reply}")

    print(f"\n=== MATCHES BY CATEGORY ===")
    for cat, n in cat_counts.most_common():
        print(f"  {cat:20s} {n:5d}")

    print(f"\n=== TOP KEYWORDS PER CATEGORY ===")
    per_cat = {}
    for (cat, kw), n in keyword_counts.items():
        per_cat.setdefault(cat, []).append((n, kw))
    for cat, items in per_cat.items():
        items.sort(reverse=True)
        print(f"\n  [{cat}]")
        for n, kw in items[:12]:
            print(f"    {n:5d}  {kw}")

    with open(args.report_csv, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["category", "keyword", "tweet_count"])
        for (cat, kw), n in sorted(keyword_counts.items(), key=lambda x: -x[1]):
            w.writerow([cat, kw, n])
    print(f"\nWrote {args.report_csv}")


if __name__ == "__main__":
    main()
