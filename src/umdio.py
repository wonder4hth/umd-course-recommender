#!/usr/bin/env python3
"""umd.io API client: course info, current sections/instructors, gen-ed lookups.

Docs: https://docs.umd.io/  Base: https://api.umd.io/v1
"""
import argparse
import json

from lib import http_get_json

BASE = "https://api.umd.io/v1"


def cmd_course(args):
    data = http_get_json(f"{BASE}/courses/{args.code.upper()}")
    print(json.dumps(data, indent=2))


def cmd_sections(args):
    data = http_get_json(f"{BASE}/courses/{args.code.upper()}/sections")
    print(json.dumps(data, indent=2))


def cmd_gened(args):
    """List courses tagged with a given Gen-Ed code (e.g. DSHU, DVUP, FSPW, SCIS)."""
    results = []
    page = 1
    while True:
        params = {"gen_ed": args.tag.upper(), "per_page": 100, "page": page}
        if args.dept:
            params["dept_id"] = args.dept.upper()
        batch = http_get_json(f"{BASE}/courses", params)
        if not batch:
            break
        results.extend(batch)
        if len(batch) < 100 or page >= args.max_pages:
            break
        page += 1
    print(json.dumps(results, indent=2))


def main():
    p = argparse.ArgumentParser(description="umd.io client")
    sub = p.add_subparsers(dest="cmd", required=True)

    c1 = sub.add_parser("course", help="course info incl. credits, gen_ed tags")
    c1.add_argument("code")
    c1.set_defaults(func=cmd_course)

    c2 = sub.add_parser("sections", help="currently scheduled sections + instructors")
    c2.add_argument("code")
    c2.set_defaults(func=cmd_sections)

    c3 = sub.add_parser("gened", help="courses satisfying a Gen-Ed attribute code")
    c3.add_argument("tag")
    c3.add_argument("--dept", help="restrict to a department id, e.g. CMSC")
    c3.add_argument("--max-pages", type=int, default=5)
    c3.set_defaults(func=cmd_gened)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
