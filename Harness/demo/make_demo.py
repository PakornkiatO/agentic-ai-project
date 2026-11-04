"""Build a small demo folder (14 files) from the eval testbed, for a live demo.

    python eval/make_testbed.py --seed 7 --out testbed/     # once, if testbed/ is missing
    python demo/make_demo.py                                # -> demo/files/
    python -m cleanguard scan demo/files

Files are picked by category from testbed/labels.csv, so this works for any seed and OS.
Copies keep their modification time (copy2), which the age rules depend on. All data is
synthetic; nothing personal is shown on screen.
"""
from __future__ import annotations

import argparse
import csv
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = Path(__file__).resolve().parent / "files"

# category -> how many, and an optional preference on the file name (first match wins)
PLAN = [
    ("old_installers", 2, None),
    ("partial_downloads", 1, None),
    ("sensitive_fake", 1, "bank_statement"),
    ("sensitive_fake", 1, "id card"),
    ("ambiguous_sensitive", 1, ".pdf"),        # only the content gives it away
    ("ambiguous_benign", 1, ".pdf"),
    ("course_materials", 1, None),
    ("personal_docs", 1, "CV_"),
    ("recent_files", 1, None),
]


def pick(rows, category, n, prefer, taken):
    cands = [r for r in rows if r["category"] == category and r["rel_path"] not in taken]
    if prefer:
        cands = [r for r in cands if prefer in r["rel_path"]] or cands
    return cands[:n]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--testbed", default=str(ROOT / "testbed"))
    ap.add_argument("--force", action="store_true", help="replace an existing demo/files/")
    a = ap.parse_args(argv)
    src_root = Path(a.testbed) / "files"
    labels = Path(a.testbed) / "labels.csv"
    if not labels.exists():
        print("No testbed. Run: python eval/make_testbed.py --seed 7 --out testbed/")
        return 2
    if OUT.exists() and any(OUT.iterdir()):
        if not a.force:
            print(f"{OUT} is not empty. Use --force to rebuild it.")
            return 2
        shutil.rmtree(OUT)
    rows = list(csv.DictReader(open(labels, newline="", encoding="utf-8")))
    taken: list[str] = []

    # an exact duplicate together with its original (same size = same bytes in the testbed)
    for dup in (r for r in rows if r["category"] == "exact_duplicates" and r["sensitive"] == "false"):
        orig = next((r for r in rows if r["category"] == "old_installers"
                     and r["size_bytes"] == dup["size_bytes"]), None)
        if orig:
            taken += [orig["rel_path"], dup["rel_path"]]
            break
    # a draft and the final version in the same folder
    for draft in (r for r in rows if r["category"] == "superseded_drafts"):
        folder = draft["rel_path"].rsplit("/", 1)[0]
        final = next((r for r in rows if r["category"] == "final_versions"
                      and r["rel_path"].startswith(folder + "/")), None)
        if final:
            taken += [draft["rel_path"], final["rel_path"]]
            break
    for category, n, prefer in PLAN:
        taken += [r["rel_path"] for r in pick(rows, category, n, prefer, taken)]

    by_path = {r["rel_path"]: r for r in rows}
    for rel in taken:
        dst = OUT / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_root / rel, dst)
    print(f"{len(taken)} files in {OUT}:")
    for rel in sorted(taken):
        r = by_path[rel]
        print(f"  {rel:<48} {r['category']:<20} expect {r['acceptable']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
