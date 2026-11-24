#!/usr/bin/env python3
"""Append like/skip feedback for a freelancer.in gig.

Usage:
  python tune.py like <url> "reason"
  python tune.py skip <url> "reason"

Appends one JSON line to feedback.jsonl in this directory. Weights live in
scoring.json and are edited by hand from this feedback (this script does not
change weights automatically).
"""
import json, os, sys, datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
BASE = os.path.dirname(os.path.abspath(__file__))
FEEDBACK = os.path.join(BASE, "feedback.jsonl")
SHORTLIST = os.path.join(BASE, "shortlist.jsonl")


def find_gig(url):
    if not url or not os.path.exists(SHORTLIST):
        return None
    url = url.strip()
    with open(SHORTLIST) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            g = json.loads(line)
            if (g.get("url") or "").rstrip("/") == url.rstrip("/"):
                return g
    return None


def main(argv):
    if len(argv) < 3 or argv[0].lower() not in ("like", "skip"):
        print("Usage: python tune.py like|skip <url> \"reason\"", file=sys.stderr)
        return 2
    verdict = argv[0].lower()
    url = argv[1].strip()
    reason = " ".join(argv[2:]).strip()
    if not url.startswith("http"):
        print("url must be an http(s) link", file=sys.stderr)
        return 2
    gig = find_gig(url)
    rec = {
        "ts": datetime.datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S %Z"),
        "verdict": verdict,
        "url": url,
        "reason": reason,
    }
    if gig:
        rec["title"] = gig.get("title")
        rec["fit_score"] = gig.get("fit_score")
        rec["matched"] = gig.get("matched")
    with open(FEEDBACK, "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"appended {verdict} feedback to {os.path.basename(FEEDBACK)}")
    if gig:
        print(f"  title: {gig.get('title')}")
        print(f"  recorded fit_score: {gig.get('fit_score')}  matched: {gig.get('matched')}")
    else:
        print("  (url not found in shortlist.jsonl; stored url + reason only)")
    print("Edit scoring.json weights by hand when you want the scorer to learn from this.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
