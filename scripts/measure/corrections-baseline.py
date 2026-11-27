#!/usr/bin/env python3
"""Count gate corrections and house-rules nudges for each build, through the dashboard API.

    python3 corrections-baseline.py --since 2026-11-04 --until 2026-11-26 --base https://druks.example

The numbers are the baseline that the lessons are judged against (DRU-1247). Builds and
nudges come from the dashboard: a build is a run of a Software Factory work item, a nudge is
a rule.hit event. Human corrections (a plan gate note, or a pull request feedback item that
triage turned into a change) come from corrections-baseline.json, where a person grouped each
one by hand. A correction repeats when an earlier one has a group in common, and a nudge
repeats when an earlier nudge came from the same rule.
Authentication is the same cookie file as fetch-event-transcripts.py.
"""

import argparse
import collections
import json
import os
import re
import sys
import urllib.parse
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
fetch = __import__("fetch-event-transcripts")
find = __import__("find-long-runs")

APP = "software_factory"


def rule_events(api, since):
    """The rule.hit events since ``since``, oldest first, with the subject each came from."""
    hits, before = [], None
    while True:
        query = {"app": APP, "from": since, "limit": 500}
        if before:
            query["before"] = before
        page = api.json("/api/events?" + urllib.parse.urlencode(query))
        hits += [item for item in page.get("items", []) if item.get("topic") == "rule.hit"]
        before = page.get("nextCursor") or page.get("next_cursor")
        if not before:
            return sorted(hits, key=lambda item: item["seq"])


def rule_of(hit):
    """The number of the rule that a nudge names, or None."""
    match = re.match(r"(\d+):", hit["payload"].get("summary", ""))
    return match[1] if match else None


def repeats(keys):
    """How many keys came up before."""
    seen, count = set(), 0
    for key in keys:
        count += key in seen
        seen.add(key)
    return count


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", required=True)
    parser.add_argument("--since", required=True, help="first day, YYYY-MM-DD")
    parser.add_argument("--until", required=True, help="last day, YYYY-MM-DD")
    parser.add_argument("--cookie", default="~/.druks-cloud-cookie", help="header file")
    args = parser.parse_args()
    end = (date.fromisoformat(args.until) + timedelta(days=1)).isoformat()

    api = fetch.Api(args.base, fetch.read_cookie(args.cookie))
    start = args.since + "T00:00:00Z"
    subjects = find.subjects_since(api, APP, start)
    repo_of, builds = {}, collections.Counter()
    for (app, subject_type, subject_id), _ in subjects.items():
        if subject_type != "work_item":
            continue
        subject = api.json(fetch.subject_path(app, subject_type, subject_id))
        repo = subject["summary"]["repo"]
        repo_of[str(subject_id)] = repo
        builds[repo] += sum(
            args.since <= run["createdAt"] < end for run in subject.get("timeline", [])
        )

    hits = [
        hit
        for hit in rule_events(api, start)
        if hit["at"] < end and str(hit.get("subjectId")) in repo_of
    ]

    with open(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "corrections-baseline.json")
    ) as handle:
        recorded = json.load(handle)
    corrections = sorted(
        (c for c in recorded["corrections"] if args.since <= c["at"] < end), key=lambda c: c["at"]
    )
    seen = set()
    for correction in corrections:
        correction["repeat"] = bool(seen & set(correction["groups"]))
        seen.update(correction["groups"])

    print(
        f"{'repo':24} {'builds':>6} {'nudges':>6} {'/build':>6} {'repeat':>6}"
        f" {'human':>5} {'/build':>6} {'repeat':>6}"
    )
    for repo in [None, *sorted(builds)]:
        runs = builds[repo] if repo else sum(builds.values())
        rows = [hit for hit in hits if not repo or repo_of[str(hit["subjectId"])] == repo]
        mine = [c for c in corrections if not repo or c["repo"] == repo]
        named = [rule_of(hit) for hit in rows if rule_of(hit)]
        print(
            f"{repo or 'all':24} {runs:6} {len(rows):6} {len(rows) / max(runs, 1):6.2f}"
            f" {repeats(named):6}"
            f" {len(mine):5} {len(mine) / max(runs, 1):6.2f} {sum(c['repeat'] for c in mine):6}"
        )
    print(
        f"\nnudge repeats are counted among the {sum(bool(rule_of(h)) for h in hits)}"
        " hits that name a rule"
    )
    print(
        "top rules:",
        collections.Counter(rule_of(hit) for hit in hits if rule_of(hit)).most_common(6),
    )

    print("\nhuman corrections by group (count, tickets):")
    tickets = collections.defaultdict(set)
    for correction in corrections:
        for group in correction["groups"]:
            tickets[group].add(correction["ticket"])
    counts = collections.Counter(g for c in corrections for g in c["groups"])
    for group, count in counts.most_common():
        print(f"{count:3} {len(tickets[group]):2} {recorded['groups'][group]}")


if __name__ == "__main__":
    main()
