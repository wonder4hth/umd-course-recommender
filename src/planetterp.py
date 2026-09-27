#!/usr/bin/env python3
"""PlanetTerp API client: grade distributions and professor ratings.

Docs: https://planetterp.com/api/  Base: https://planetterp.com/api/v1
No auth required; be a polite citizen (this is used at low volume by design).
"""
import argparse
import json

from lib import http_get_json

BASE = "https://planetterp.com/api/v1"

GRADE_POINTS = {
    "A+": 4.0, "A": 4.0, "A-": 3.7,
    "B+": 3.3, "B": 3.0, "B-": 2.7,
    "C+": 2.3, "C": 2.0, "C-": 1.7,
    "D+": 1.3, "D": 1.0, "D-": 0.7,
    "F": 0.0,
}


def aggregate_grades(rows):
    totals = {g: 0 for g in GRADE_POINTS}
    for row in rows:
        for g in totals:
            totals[g] += row.get(g, 0) or 0
    n = sum(totals.values())
    if not n:
        return None
    avg_gpa = sum(totals[g] * GRADE_POINTS[g] for g in GRADE_POINTS) / n
    pct_a = (totals["A+"] + totals["A"] + totals["A-"]) / n
    return {
        "graded_n": n,
        "avg_gpa": round(avg_gpa, 3),
        "pct_a_range": round(pct_a, 3),
        "grade_totals": totals,
    }


def cmd_course(args):
    data = http_get_json(f"{BASE}/course", {"name": args.code.upper()})
    print(json.dumps(data, indent=2))


def cmd_professor(args):
    data = http_get_json(f"{BASE}/professor", {"name": args.name, "reviews": "false"})
    print(json.dumps(data, indent=2))


def cmd_grades(args):
    params = {"course": args.code.upper()}
    if args.professor:
        params["professor"] = args.professor
    rows = http_get_json(f"{BASE}/grades", params)
    stats = aggregate_grades(rows)
    out = {"course": args.code.upper(), "professor": args.professor, "sections_counted": len(rows)}
    out.update(stats or {"graded_n": 0, "avg_gpa": None, "pct_a_range": None, "grade_totals": None})
    print(json.dumps(out, indent=2))


def main():
    p = argparse.ArgumentParser(description="PlanetTerp client")
    sub = p.add_subparsers(dest="cmd", required=True)

    c1 = sub.add_parser("course", help="course-level average GPA + professors who've taught it")
    c1.add_argument("code")
    c1.set_defaults(func=cmd_course)

    c2 = sub.add_parser("professor", help="professor average rating")
    c2.add_argument("name")
    c2.set_defaults(func=cmd_professor)

    c3 = sub.add_parser("grades", help="aggregated grade distribution for a course, optionally by professor")
    c3.add_argument("code")
    c3.add_argument("--professor")
    c3.set_defaults(func=cmd_grades)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
