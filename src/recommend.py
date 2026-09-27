#!/usr/bin/env python3
"""Rank candidate courses by 'easy A' signal (PlanetTerp grade history + current
instructor rating), filtered by a credit range, grouped by requirement.

Input: a JSON file of candidate entries, each either
  {"tier": "major"|"gened"|"other", "requirement": "<label>", "code": "CMSC330"}
or a Gen-Ed wildcard that gets expanded via umd.io:
  {"tier": "gened", "requirement": "Humanities (DSHU)", "gen_ed": "DSHU", "dept": null}

Output: JSON grouped by (tier, requirement), courses sorted by avg_gpa desc
(courses with no PlanetTerp grade data sort last, not dropped).

Courses discovered only via a Gen-Ed wildcard search (not explicitly named by the
audit's own SELECT FROM list) that carry an enrollment restriction or permission
requirement (e.g. "Must be an entering freshman in the Honors Humanities Program",
"Permission of department required") are split into a separate `restricted_or_permission`
list per requirement, since the audit never vetted them and the student may not
actually be eligible to register. Courses the audit itself names are trusted and
stay in `recommendations` even if they happen to carry a restriction note.

Every returned course also carries `sections_total` / `open_seats_total` /
`has_open_section` (from umd.io's live sections, `has_open_section` is None if
the course has no scheduled sections yet). Pass --open-only to additionally
*filter out* anything with no open section before ranking — slower, since it
means checking sections for every candidate instead of only the final picks.
"""
import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from lib import http_get_json
from planetterp import aggregate_grades

UMDIO = "https://api.umd.io/v1"
PT = "https://planetterp.com/api/v1"
TIER_ORDER = {"major": 0, "gened": 1, "other": 2}
PAUSE = 0.05  # be polite to PlanetTerp between calls
WORKERS = 16  # network-bound calls, threading is safe (each hits a distinct cache key)

# Free-text signals in umd.io's `relationships.additional_info` that mean "you may
# not just be able to register for this" even though `relationships.restrictions`
# (which is always trusted when present) came back empty.
RESTRICTION_KEYWORDS = (
    "permission", "restrict", "priority", "must be", "only open",
    "eligib", "consent", "approval", "admitted to", "majors only",
)

_course_cache = {}
_sections_cache = {}
_grades_cache = {}
_rating_cache = {}


def resolve_candidates(entries):
    """Expand gen_ed wildcards via umd.io. Tags every entry with `from_audit`:
    True for a course code the audit's own SELECT FROM list named directly,
    False for one only discovered by broadening the search ourselves."""
    resolved = []
    for e in entries:
        if e.get("gen_ed"):
            params = {"gen_ed": e["gen_ed"].upper(), "per_page": 100}
            if e.get("dept"):
                params["dept_id"] = e["dept"].upper()
            try:
                courses = http_get_json(f"{UMDIO}/courses", params)
            except Exception as err:
                print(f"warning: gen_ed lookup failed for {e['gen_ed']!r} "
                      f"({e['requirement']!r}), skipping: {err}", file=sys.stderr)
                continue
            for c in courses:
                resolved.append({
                    "code": c["course_id"], "tier": e["tier"], "requirement": e["requirement"],
                    "from_audit": False,
                })
        else:
            ee = dict(e)
            ee.setdefault("from_audit", True)
            resolved.append(ee)
    seen, out = set(), []
    for e in resolved:
        key = (e["code"], e["requirement"])
        if key not in seen:
            seen.add(key)
            out.append(e)
    return out


def course_info(code):
    if code not in _course_cache:
        try:
            data = http_get_json(f"{UMDIO}/courses/{code}")
            _course_cache[code] = data[0] if data else None
        except Exception:
            _course_cache[code] = None
    return _course_cache[code]


def get_sections(code):
    """Current Testudo-listed sections for a course. Shared cache so checking
    instructors and checking open seats never double-fetch the same endpoint."""
    if code not in _sections_cache:
        try:
            _sections_cache[code] = http_get_json(f"{UMDIO}/courses/{code}/sections")
        except Exception:
            _sections_cache[code] = []
    return _sections_cache[code]


def current_instructors(code):
    return sorted({n for s in get_sections(code) for n in s.get("instructors", [])})


def open_seat_info(code):
    sections = get_sections(code)
    total_open = 0
    for s in sections:
        try:
            total_open += int(s.get("open_seats") or 0)
        except (TypeError, ValueError):
            pass
    return {
        "sections_total": len(sections),
        "open_seats_total": total_open,
        "has_open_section": total_open > 0 if sections else None,  # None = unknown (not yet scheduled)
    }


def grade_stats(code):
    if code not in _grades_cache:
        try:
            rows = http_get_json(f"{PT}/grades", {"course": code})
            _grades_cache[code] = aggregate_grades(rows)
        except Exception:
            _grades_cache[code] = None
        time.sleep(PAUSE)
    return _grades_cache[code]


def professor_rating(name):
    if name not in _rating_cache:
        try:
            data = http_get_json(f"{PT}/professor", {"name": name})
            _rating_cache[name] = data.get("average_rating")
        except Exception:
            _rating_cache[name] = None
        time.sleep(PAUSE)
    return _rating_cache[name]


def restriction_note(info):
    rel = (info or {}).get("relationships") or {}
    restrictions = (rel.get("restrictions") or "").strip()
    extra = (rel.get("additional_info") or "").strip()
    parts = []
    if restrictions:
        parts.append(restrictions)
    if extra and any(k in extra.lower() for k in RESTRICTION_KEYWORDS):
        parts.append(extra)
    return " | ".join(parts) if parts else None


def cheap_row(code):
    """Course info + grade history only (2 calls) — cheap enough to run on every candidate."""
    info = course_info(code)
    if not info:
        return None
    try:
        credits = float(info.get("credits", 0) or 0)
    except ValueError:
        credits = 0.0
    gs = grade_stats(code)
    return {
        "code": code,
        "title": info.get("name"),
        "credits": credits,
        "avg_gpa": gs["avg_gpa"] if gs else None,
        "pct_a_range": gs["pct_a_range"] if gs else None,
        "n_grades": gs["graded_n"] if gs else 0,
        "restriction": restriction_note(info),
    }


def enrich_instructor(row):
    """Current sections + per-instructor rating (several calls) — only run on courses
    that actually made it into the final top-N, not every resolved candidate.
    Also attaches open-seat info at no extra cost, since it's the same section
    fetch current_instructors() already needs (cached in get_sections)."""
    instructors = current_instructors(row["code"])
    best_prof, best_rating = None, None
    for prof in instructors:
        r = professor_rating(prof)
        if r and (best_rating is None or r > best_rating):
            best_prof, best_rating = prof, r
    row["current_instructors"] = instructors
    row["top_rated_current_instructor"] = best_prof
    row["instructor_rating"] = best_rating
    row.update(open_seat_info(row["code"]))
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("candidates_file")
    ap.add_argument("--credits-min", type=float, default=1)
    ap.add_argument("--credits-max", type=float, default=3)
    ap.add_argument("--top", type=int, default=5, help="max courses shown per requirement")
    ap.add_argument("--open-only", action="store_true",
                     help="only keep courses with at least one current section that "
                          "isn't full. Requires checking sections for every candidate "
                          "up front (not just the eventual top picks), so this is "
                          "noticeably slower than the default.")
    args = ap.parse_args()

    with open(args.candidates_file) as f:
        entries = json.load(f)
    entries = resolve_candidates(entries)

    by_req = {}
    for e in entries:
        by_req.setdefault((e["tier"], e["requirement"]), []).append(e)

    all_codes = sorted({e["code"] for items in by_req.values() for e in items})
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        cheap_rows = dict(zip(all_codes, pool.map(cheap_row, all_codes)))

    if args.open_only:
        # Normally section data is only fetched for the final top-N (in enrich_instructor).
        # Filtering on open seats has to happen *before* ranking/truncation, so every
        # candidate needs it up front here instead — this is the slow part.
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            open_infos = dict(zip(all_codes, pool.map(open_seat_info, all_codes)))
        for code, info in open_infos.items():
            if cheap_rows.get(code):
                cheap_rows[code].update(info)

    def rank(rows):
        rows.sort(key=lambda r: (r["avg_gpa"] is None, -(r["avg_gpa"] or 0)))
        return rows[: args.top]

    buckets = []
    for (tier, requirement), items in sorted(by_req.items(), key=lambda kv: TIER_ORDER.get(kv[0][0], 9)):
        clean, flagged = [], []
        for item in items:
            base = cheap_rows.get(item["code"])
            if base is None:
                continue
            if not (args.credits_min <= base["credits"] <= args.credits_max):
                continue
            if args.open_only and not base.get("has_open_section"):
                continue
            row = dict(base)
            row["from_audit"] = item["from_audit"]
            if row["restriction"] and not item["from_audit"]:
                flagged.append(row)
            else:
                clean.append(row)
        buckets.append((tier, requirement, rank(clean), rank(flagged)))

    # Only fetch current-instructor + rating (the expensive part) for courses that
    # actually made the cut, and only once per unique course across all buckets.
    to_enrich = {row["code"]: row for _, _, clean, flagged in buckets for row in clean + flagged}
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        list(pool.map(enrich_instructor, to_enrich.values()))

    report = [
        {"tier": t, "requirement": r, "recommendations": clean, "restricted_or_permission": flagged}
        for t, r, clean, flagged in buckets
    ]
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
