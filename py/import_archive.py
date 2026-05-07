#!/usr/bin/env python3
"""
Parse account IDs out of an X (Twitter) data archive.

Why: X caps live-Following pagination at ~1,000-1,500 entries, so live scrapes
miss most of a real follow graph. The archive download has the full list.

Inputs the archive's data/ folder. Writes a newline-separated ID list ready
for resolve_handles.py to enrich with handles + follower counts.

Run:
  python3 py/import_archive.py --archive ~/Downloads/twitter-archive
  # writes py/output/ids.txt

  # Source other than 'following':
  python3 py/import_archive.py --archive ~/Downloads/twitter-archive --source follower
  # writes IDs from follower.js
"""

import argparse
import json
import sys
from pathlib import Path

OUTPUT_DIR = Path(__file__).parent / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

# (archive filename, top-level key in each entry, accountId field)
SOURCES = {
    "following": ("following.js", "following"),
    "follower":  ("follower.js",  "follower"),
    "block":     ("block.js",     "blocking"),
    "mute":      ("mute.js",      "muting"),
}


def parse_archive_js(path, key):
    """Twitter archive .js files start with `window.YTD.<name>.partN = [...]`.
    Strip the assignment, parse the JSON array, extract `entry[key].accountId`.
    """
    raw = path.read_text()
    arr = json.loads(raw[raw.index("["):])
    return [item[key]["accountId"] for item in arr if item.get(key, {}).get("accountId")]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", type=Path, required=True,
                        help="Path to your unzipped X archive (the folder containing data/)")
    parser.add_argument("--source", choices=list(SOURCES), default="following",
                        help="Which list to import (default: following)")
    parser.add_argument("--output", type=Path, default=OUTPUT_DIR / "ids.txt")
    args = parser.parse_args()

    data_dir = args.archive / "data"
    if not data_dir.exists():
        print(f"ERROR: {data_dir} not found. Pass the archive root (folder containing data/).")
        sys.exit(1)

    filename, key = SOURCES[args.source]
    src_path = data_dir / filename
    if not src_path.exists():
        print(f"ERROR: {src_path} not found in your archive.")
        sys.exit(1)

    ids = parse_archive_js(src_path, key)
    args.output.write_text("\n".join(ids) + "\n")
    print(f"Wrote {len(ids):,} {args.source} IDs → {args.output}")
    print(f"Next: python3 py/resolve_handles.py --input {args.output}")


if __name__ == "__main__":
    main()
