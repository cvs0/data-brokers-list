#!/usr/bin/env python3
"""Score brokers for automation work.

score = min(100, impact + readiness)
  impact (0-50): reviewed judgement in data/v2/priority-impact.csv (reach of free public
                 profiles, data sensitivity, brands covered by one request, Canadian relevance)
  readiness (0-50): v1 research status (verified 20 / partial 10 / blocked 0)
                  + automation class (assisted 15 / manual 8)
                  + no bot wall or inaccessible page (+10)
                  + published privacy email or email channel (+5)
tier: launch = score >= 75 and a usable channel (no wall, or email exists), plus california-drop
      research = v1 blocked;  next = everything else
Writes data/v2/priority.csv sorted by impact order of the input file.
"""

from __future__ import annotations

import sys
from pathlib import Path

_here = Path(__file__).resolve().parent
_scripts = _here if (_here / "bootstrap_path.py").exists() else _here.parent
if str(_scripts) not in sys.path:
    sys.path.insert(0, str(_scripts))
import bootstrap_path

bootstrap_path.install()

import csv

from v2_common import V2_DIR, v1_records

IMPACT = V2_DIR / "priority-impact.csv"
OUT = V2_DIR / "priority.csv"


def readiness(record: dict) -> int:
    v, a, rw = record["verification"], record["automation"], record["request_workflow"]
    blockers = a["observed_blockers"]
    score = {"verified": 20, "partial": 10}.get(v["research_status"], 0)
    score += {"assisted_workflow": 15, "manual_workflow": 8}.get(a["classification"], 0)
    score += 0 if ("bot_wall" in blockers or "inaccessible_page" in blockers) else 10
    score += 5 if ("email" in rw["submission_methods"] or record["discovery"]["privacy_contact_email"]) else 0
    return score


def tier(record: dict, score: int) -> str:
    if record["id"] == "california-drop":
        return "launch"
    blockers = record["automation"]["observed_blockers"]
    walled = "bot_wall" in blockers or "inaccessible_page" in blockers
    usable = (not walled) or "email" in record["request_workflow"]["submission_methods"]
    if score >= 75 and usable:
        return "launch"
    if record["verification"]["research_status"] == "blocked":
        return "research"
    return "next"


def main() -> None:
    records = v1_records()
    with IMPACT.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    out = []
    for rank, row in enumerate(rows, 1):
        record = records.get(row["id"])
        if record is None:
            raise SystemExit(f"{row['id']} is not in the v1 dataset")
        ready = readiness(record)
        score = min(100, int(row["impact"]) + ready)
        out.append({"rank": rank, "id": row["id"], "impact": row["impact"], "readiness": ready,
                    "score": score, "tier": tier(record, score), "rationale": row["rationale"]})
    with OUT.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(out[0]))
        writer.writeheader()
        writer.writerows(out)
    counts: dict[str, int] = {}
    for row in out:
        counts[row["tier"]] = counts.get(row["tier"], 0) + 1
    print({"brokers": len(out), "tiers": counts})


if __name__ == "__main__":
    main()
