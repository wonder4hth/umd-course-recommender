"""Deterministic (regex-based, no LLM) parser for UMD uAchieve/CollegeSource
degree-audit PDF text.

This engine (uAchieve) is used university-wide, so the low-level markers below
(NEEDS:, SELECT FROM:, [TAG] section headers, the two top banners, course rows)
are consistent across majors. What varies by major is section wording, which we
don't try to fully model — instead we grab the nearest description line above
each unmet requirement as a best-effort label and preserve enough raw context
for a human (or a follow-up LLM pass) to sanity-check it.

Only tested against two real audits (a CS-ML student mid-program, a graduated
CompE student) — treat parsing of an unseen major's audit as a first draft that
may need the `label`/`tier` fields corrected by hand, not gospel.
"""
import re

TERM = r"(?:Fa|Sp|Wi|Su|S1|S2)\d{2}"
COURSE_ROW_RE = re.compile(rf"^{TERM}\s+([A-Z]{{2,6}}\d{{3}}[A-Z0-9]{{0,2}})\s+([\d.]+)\s+(\S+)\s*(.*)$")
NEEDS_RE = re.compile(r"^NEEDS:\s*(.+)$", re.IGNORECASE)
SELECT_FROM_RE = re.compile(r"^SELECT FROM:\s*(.*)$", re.IGNORECASE)
BRACKET_RE = re.compile(r"^\[([\w/]+)\]\s*(.*)$")
GENED_CODE_RE = re.compile(r"\(([A-Z]{3,5})\)")
MARKER_RE = re.compile(r"^\d+\)$")
NUM_CREDITS_RE = re.compile(r"([\d.]+)\s*CREDITS?", re.IGNORECASE)
NUM_COURSES_RE = re.compile(r"(\d+)\s*(?:COURSES?|SUB-GROUPS?)", re.IGNORECASE)
TOKEN_RE = re.compile(r"\b([A-Z]{2,4})\b|\b(\d{3}[A-Z]{0,2})\b")
NOISE_PREFIXES = ("XCMSC", "XMATH", "XPHYS", "XCHEM", "XECON", "XHIS", "X")

STOP_WORDS = {"OR", "AND"}

STATUS_COMPLETE = "ALL REQUIREMENTS IDENTIFIED BELOW HAVE BEEN MET"
STATUS_INCOMPLETE = "AT LEAST ONE REQUIREMENT HAS NOT BEEN SATISFIED"


def _is_noise_line(line):
    if not line.strip():
        return True
    if MARKER_RE.match(line.strip()):
        return True
    if COURSE_ROW_RE.match(line.strip()):
        return True
    if line.strip().startswith(("IN-P", "IN-", "PROGRESS", "EARNED:", "Advanced Placement Exam")):
        return True
    if any(line.strip().startswith(p) for p in NOISE_PREFIXES):
        return True
    if re.match(r"^\(\s*[\d.]+\s+CREDITS", line.strip()):
        return True
    if re.match(r"^\d{4}/\d{1,2}/\d{1,2}\s", line.strip()):  # page footer timestamp
        return True
    if line.strip().startswith("https://uachieve"):
        return True
    if re.match(r"^\S.*\s\d+/\d+$", line.strip()) and "http" not in line:  # "... 1/5"
        return True
    return False


def parse_course_codes(blob):
    """'CMSC 426,460 OR AMSC 460 ... CMSC 474,498F' -> ['CMSC426','CMSC460',...]."""
    codes = []
    current_dept = None
    for m in TOKEN_RE.finditer(blob):
        dept, num = m.group(1), m.group(2)
        if dept:
            if dept in STOP_WORDS:
                continue
            current_dept = dept
        elif num and current_dept:
            codes.append(f"{current_dept}{num}")
    return codes


def classify_tier(tag, seen_general_elective):
    """uAchieve only brackets [UNIV] and [GenEd]/[CORE/GenEd] sections — major
    department requirements (Lower Level Requirements, ML Specialization, ...)
    sit untagged under whatever bracket came before them (usually still [UNIV],
    since majors don't get their own tag). So `tag` alone can't separate
    "generic university requirement" from "major requirement"; it doesn't need
    to, since the generic ones (total credits, residency, GPA) never have a
    SELECT FROM/Gen-Ed code and get dropped by the `actionable` filter anyway.
    Only a real [GenEd]/[CORE/GenEd] tag, or the General Elective Courses tail,
    should route away from "major"."""
    if seen_general_elective:
        return "other"
    if tag and "GenEd" in tag:
        return "gened"
    return "major"


def find_label(lines, needs_idx, lookback=15):
    """Nearest non-noise line above a NEEDS: line is usually the requirement's
    own header — but wrapped boilerplate ("Courses fulfilling ... may also
    double count with...") sometimes sits between the real header and NEEDS:,
    and boilerplate never carries a Gen-Ed (XXXX) code. So: prefer the closest
    candidate line that itself carries a Gen-Ed code, and only fall back to
    the plain nearest line when none in the window has one."""
    candidates = []
    for k in range(needs_idx - 1, max(-1, needs_idx - 1 - lookback), -1):
        line = lines[k].strip()
        if not line or _is_noise_line(line) or BRACKET_RE.match(line):
            continue
        if NEEDS_RE.match(line) or SELECT_FROM_RE.match(line):
            continue
        candidates.append(line)
    for line in candidates:
        if GENED_CODE_RE.search(line):
            return line
    return candidates[0] if candidates else None


def parse_completed_courses(lines):
    out = []
    for line in lines:
        m = COURSE_ROW_RE.match(line.strip())
        if not m:
            continue
        code, credits, grade, title = m.groups()
        out.append({
            "code": code, "credits": float(credits), "grade": grade,
            "title": title.strip(), "in_progress": grade.upper() == "IP",
            "transfer_or_ap": grade.upper() in ("TP", "TA"),
        })
    return out


def parse_student_header(lines):
    name, major = None, None
    for i, line in enumerate(lines[:80]):
        line = line.strip()
        if re.match(r"^[A-Za-z.\-']+,\s*[A-Za-z.\-' ]+$", line) and "," in line and len(line) < 40:
            name = line
            for j in range(i + 1, min(i + 3, len(lines))):
                cand = lines[j].strip()
                if cand and not cand.startswith("Prepared"):
                    major = cand
                    break
            break
    return name, major


def parse(text):
    lines = text.split("\n")

    if STATUS_COMPLETE in text:
        status = "complete"
    elif STATUS_INCOMPLETE in text:
        status = "in_progress"
    else:
        status = "unknown"

    name, major = parse_student_header(lines)

    requirements = []
    current_tag = None
    seen_general_elective = False

    for i, raw in enumerate(lines):
        line = raw.strip()

        m = BRACKET_RE.match(line)
        if m:
            current_tag = m.group(1)
            continue

        if line.startswith("General Elective Courses") or line.startswith("Unused Courses"):
            seen_general_elective = True
            continue

        m = NEEDS_RE.match(line)
        if not m:
            continue

        needs_text = m.group(1)
        cred_m = NUM_CREDITS_RE.search(needs_text)
        course_m = NUM_COURSES_RE.search(needs_text)
        needs_credits = float(cred_m.group(1)) if cred_m else None
        needs_courses = int(course_m.group(1)) if course_m else None

        # SELECT FROM can be on the next line, and can spill onto further
        # lines that keep listing "DEPT num,num,..." until the list ends.
        select_from_blob = ""
        j = i + 1
        sf = SELECT_FROM_RE.match(lines[j].strip()) if j < len(lines) else None
        if sf:
            select_from_blob = sf.group(1)
            j += 1
            while j < len(lines) and re.match(r"^[A-Z]{2,4}\s", lines[j].strip()):
                select_from_blob += " " + lines[j].strip()
                j += 1

        candidates = parse_course_codes(select_from_blob) if select_from_blob else None

        label = find_label(lines, i) or ""
        gened_code = None
        gm = GENED_CODE_RE.search(label)
        if gm:
            gened_code = gm.group(1)

        # Hardcoded: the audit's second diversity requirement reads "Cultural
        # Competence (DVCC) or 2nd / Understanding Plural Society (DVUP) course",
        # wrapped over two lines — find_label only catches the DVUP half, so it
        # shows up as a duplicate DVUP. Any DVUP requirement after the first is
        # that one; recommend DVCC courses for it instead.
        if gened_code == "DVUP" and any(r["gen_ed"] == "DVUP" for r in requirements):
            gened_code = "DVCC"
            label = "Cultural Competence (DVCC) course"

        tier = classify_tier(current_tag, seen_general_elective)

        requirements.append({
            "tier": tier,
            "section_tag": current_tag,
            "label": label,
            "needs_credits": needs_credits,
            "needs_courses": needs_courses,
            "candidates": candidates,
            "gen_ed": gened_code,
            "actionable": bool(candidates) or bool(gened_code),
        })

    return {
        "status": status,
        "student": {"name": name, "major": major},
        "requirements": requirements,
        "completed_courses": parse_completed_courses(lines),
    }
