"""Native-speaker review of the West-/East-Flemish lexicon (spec FR-19).

  python -m babbel.evals.lexicon_review export --region wvl --out review-wvl.csv
      a sheet for one reviewer: every entry for that region (plus general Flemish), with empty
      columns for the verdict. Open it in Excel or LibreOffice.
  python -m babbel.evals.lexicon_review import review-wvl.csv
      adds the filled-in rows to src/babbel/locales/dialects/vlaams_reviews.csv (the record)
  python -m babbel.evals.lexicon_review status
      per region: how many entries are confirmed, disputed or not reviewed yet

Verdicts: ok (form, meaning and region are right), wrong (fill in what is right), unsure.
An entry counts as confirmed with at least one ok and no wrong. Corrections are never applied
automatically: edit vlaams.yaml by hand, then the entry's next review confirms it.
Reviewer names stay in the reviews file only: use initials if the reviewer prefers.
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

from babbel.core.dialect import entries
from babbel.core.locale import LOCALES_DIR
from babbel.evals.asr_tools import read_csv_rows, write_csv_rows

REVIEWS = LOCALES_DIR / "dialects" / "vlaams_reviews.csv"
EXPORT_FIELDS = ["key", "forms", "standard", "region", "area", "category", "safety", "example",
                 "sourced", "verdict", "correct_meaning", "correct_region", "comment"]
REVIEW_FIELDS = ["date", "reviewer", "reviewer_area", "key", "verdict", "correct_meaning",
                 "correct_region", "comment"]
VERDICTS = {"ok", "wrong", "unsure"}


def entry_key(e: dict) -> str:
    return e["forms"][0]


def export(region: str | None, out: Path) -> int:
    rows = entries(region)
    write_csv_rows(out, EXPORT_FIELDS, [
        {"key": entry_key(e), "forms": " / ".join(e["forms"]), "standard": e["standard"],
         "region": e["region"], "area": e.get("area", ""), "category": e["category"],
         "safety": e.get("safety", ""), "example": e["example"],
         "sourced": "yes" if e.get("src") else "no"} for e in rows])
    return len(rows)


def load_reviews(path: Path = REVIEWS) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def import_sheet(sheet: Path, reviewer: str, reviewer_area: str, path: Path = REVIEWS) -> tuple[int, list[str]]:
    known = {entry_key(e) for e in entries()}
    added, problems = [], []
    # Excel may save with semicolons and a BOM: read_csv_rows handles both
    for i, row in enumerate(read_csv_rows(sheet), start=2):
        verdict = (row.get("verdict") or "").strip().lower()
        if not verdict:
            continue
        if verdict not in VERDICTS:
            problems.append(f"line {i}: verdict {verdict!r} (use ok, wrong or unsure)")
            continue
        if row.get("key") not in known:
            problems.append(f"line {i}: unknown entry {row.get('key')!r}")
            continue
        if verdict == "wrong" and not (row.get("correct_meaning") or row.get("correct_region")
                                       or row.get("comment")):
            problems.append(f"line {i}: 'wrong' needs a correct_meaning, correct_region or comment")
            continue
        added.append({"date": date.today().isoformat(), "reviewer": reviewer,
                      "reviewer_area": reviewer_area, "key": row["key"], "verdict": verdict,
                      **{k: (row.get(k) or "").strip() for k in
                         ("correct_meaning", "correct_region", "comment")}})
    if added and not problems:
        new = not path.exists()
        with path.open("a", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=REVIEW_FIELDS)
            if new:
                w.writeheader()
            w.writerows(added)
    return (len(added) if not problems else 0), problems


def status(reviews: list[dict] | None = None) -> dict[str, Counter]:
    reviews = load_reviews() if reviews is None else reviews
    by_key: dict[str, list[str]] = defaultdict(list)
    for r in reviews:
        by_key[r["key"]].append(r["verdict"])
    out: dict[str, Counter] = defaultdict(Counter)
    for e in entries():
        verdicts = by_key.get(entry_key(e), [])
        state = ("disputed" if "wrong" in verdicts else "confirmed" if "ok" in verdicts
                 else "unsure" if verdicts else "not reviewed")
        out[e["region"]][state] += 1
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Native-speaker review of the Flemish lexicon")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ex = sub.add_parser("export")
    ex.add_argument("--region", choices=["wvl", "ovl"], default=None)
    ex.add_argument("--out", required=True)
    im = sub.add_parser("import")
    im.add_argument("sheet")
    im.add_argument("--reviewer", required=True, help="name or initials")
    im.add_argument("--reviewer-area", required=True, help="area id, e.g. wvl-westhoek")
    sub.add_parser("status")
    args = ap.parse_args()

    if args.cmd == "export":
        n = export(args.region, Path(args.out))
        print(f"{n} entries written to {args.out}. Fill in: verdict (ok/wrong/unsure), "
              "correct_meaning, correct_region, comment.")
    elif args.cmd == "import":
        n, problems = import_sheet(Path(args.sheet), args.reviewer, args.reviewer_area)
        for p in problems:
            print("  -", p)
        if problems:
            sys.exit("nothing imported: fix the lines above and run again")
        print(f"{n} reviews added to {REVIEWS.relative_to(LOCALES_DIR.parents[2])}")
    else:
        for region, counts in sorted(status().items()):
            total = sum(counts.values())
            print(f"{region}: {total} entries, " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
        disputed = [r for r in load_reviews() if r["verdict"] == "wrong"]
        for r in disputed:
            print(f"  disputed {r['key']!r} by {r['reviewer']} ({r['reviewer_area']}): "
                  f"{r['correct_meaning'] or r['correct_region'] or r['comment']}")


if __name__ == "__main__":
    main()
