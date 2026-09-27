#!/usr/bin/env python3
"""End-to-end driver: degree-audit PDF -> parsed requirements -> ranked easy-A
course recommendations, all dumped as JSON to output/.

    .venv/bin/python3 driver.py "path/to/My Audit.pdf" --credits-min 1 --credits-max 3

Writes up to three files per run, named after the PDF (e.g. "my_audit"):
  output/<slug>_parsed.json          raw parser output (status, requirements, completed courses)
  output/<slug>_candidates.json      candidates.json fed to recommend.py
  output/<slug>_recommendations.json final ranked report

If the audit says every requirement is already met, this stops after the
parsed dump: there's nothing to target, and generating an untargeted "browse
everything" list only makes sense once there's a UI for picking subject
interests (see web/).

`run_pipeline()` is the reusable entry point — web/app.py imports it directly
rather than shelling out to this script.
"""
import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from parser.audit_parser import parse  # noqa: E402


def slugify(name):
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "audit"


def extract_text(pdf_path):
    from pypdf import PdfReader
    reader = PdfReader(pdf_path)
    return "\n".join(page.extract_text() for page in reader.pages)


def build_candidates(parsed):
    """requirements -> candidates.json entries, dropping non-actionable ones
    (generic UNIV credit/GPA/residency requirements: no SELECT FROM, no Gen-Ed
    code, nothing a course search could resolve)."""
    candidates = []
    for req in parsed["requirements"]:
        if not req["actionable"]:
            continue
        label = req["label"] or "(unlabeled requirement)"
        if req["candidates"]:
            for code in req["candidates"]:
                candidates.append({"tier": req["tier"], "requirement": label, "code": code})
        elif req["gen_ed"]:
            candidates.append({"tier": req["tier"], "requirement": label, "gen_ed": req["gen_ed"]})
    return candidates


def run_pipeline(pdf_path, credits_min=1, credits_max=3, top=5, open_only=False,
                  out_dir=None, progress=None):
    """Runs the full parse -> candidates -> rank pipeline for one PDF.

    `progress`, if given, is called with short status strings as each stage
    starts (useful for a web UI's "still working..." indicator).
    Returns {"status": ..., "student": ..., "recommendations": [...] | None}.
    Also writes the same JSON dumps driver.py's CLI writes, if out_dir is given.
    """
    def note(msg):
        if progress:
            progress(msg)

    pdf_path = Path(pdf_path)
    slug = slugify(pdf_path.stem)
    out_dir = Path(out_dir) if out_dir else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)

    note("extracting text")
    text = extract_text(pdf_path)

    note("parsing requirements")
    parsed = parse(text)
    if out_dir:
        (out_dir / f"{slug}_parsed.json").write_text(json.dumps(parsed, indent=2, ensure_ascii=False))

    result = {"status": parsed["status"], "student": parsed["student"], "recommendations": None}
    if parsed["status"] == "complete":
        return result

    candidates = build_candidates(parsed)
    if out_dir:
        (out_dir / f"{slug}_candidates.json").write_text(json.dumps(candidates, indent=2, ensure_ascii=False))
    if not candidates:
        result["status"] = "no_actionable_requirements"
        return result

    note("ranking via umd.io / PlanetTerp" + (" (open-sections check: slower)" if open_only else ""))
    cmd_extra = ["--open-only"] if open_only else []
    if out_dir:
        candidates_path = out_dir / f"{slug}_candidates.json"  # already written above
        proc = subprocess.run(
            [sys.executable, str(SRC / "recommend.py"), str(candidates_path),
             "--credits-min", str(credits_min), "--credits-max", str(credits_max),
             "--top", str(top), *cmd_extra],
            capture_output=True, text=True,
        )
    else:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(candidates, f)
            tmp_path = f.name
        try:
            proc = subprocess.run(
                [sys.executable, str(SRC / "recommend.py"), tmp_path,
                 "--credits-min", str(credits_min), "--credits-max", str(credits_max),
                 "--top", str(top), *cmd_extra],
                capture_output=True, text=True,
            )
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    if proc.returncode != 0:
        raise RuntimeError(f"recommend.py failed: {proc.stderr}")

    result["recommendations"] = json.loads(proc.stdout)
    if out_dir:
        (out_dir / f"{slug}_recommendations.json").write_text(proc.stdout)
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("audit_pdf", help="path to a UMD uAchieve degree-audit PDF export")
    ap.add_argument("--credits-min", type=float, default=1)
    ap.add_argument("--credits-max", type=float, default=3)
    ap.add_argument("--top", type=int, default=5, help="max courses shown per requirement")
    ap.add_argument("--open-only", action="store_true", help="only keep courses with an open current section")
    ap.add_argument("--out-dir", default=str(ROOT / "output"))
    args = ap.parse_args()

    result = run_pipeline(
        args.audit_pdf, args.credits_min, args.credits_max, args.top, args.open_only,
        out_dir=args.out_dir, progress=lambda m: print(f"      {m} ...", flush=True),
    )
    print(f"status={result['status']}  student={result['student']}")
    if result["recommendations"] is None:
        print("Nothing to rank — see status above and the parsed dump in --out-dir.")


if __name__ == "__main__":
    main()
