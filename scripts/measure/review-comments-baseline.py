#!/usr/bin/env python3
"""Count how often a reviewer's comments on pull requests repeat an earlier one.

    python3 review-comments-baseline.py

The comments are in review-comments-baseline.json: each is a GitHub comment id with the pull
request and time, and the groups that a person gave it by hand (the group names are in the
same file). A comment repeats when an earlier one has a group in common. The script reads
that file only and makes no request. It is the other half of the baseline of DRU-1247, next
to corrections-baseline.py.
"""

import collections
import json
import os


def main():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "review-comments-baseline.json")
    with open(path) as handle:
        recorded = json.load(handle)
    comments = sorted(recorded["comments"], key=lambda c: c["at"])
    seen = set()
    for comment in comments:
        comment["repeat"] = bool(seen & set(comment["groups"]))
        seen.update(comment["groups"])

    pulls = {c["pr"] for c in comments}
    print(
        f"{len(comments)} comments on {len(pulls)} pull requests, "
        f"{sum(c['repeat'] for c in comments)} repeat an earlier group"
    )
    pulls_of = collections.defaultdict(set)
    for comment in comments:
        for group in comment["groups"]:
            pulls_of[group].add(comment["pr"])
    counts = collections.Counter(g for c in comments for g in c["groups"])
    for group, count in counts.most_common():
        print(f"{count:3} on {len(pulls_of[group]):2} pull requests  {recorded['groups'][group]}")

    half = comments[len(comments) // 2]["at"]
    first = sum(c["at"] < half for c in comments)
    print(f"first half {first}, second half {len(comments) - first}, split at {half[:10]}")


if __name__ == "__main__":
    main()
