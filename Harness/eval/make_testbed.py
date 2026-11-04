"""Testbed generator (M20, D). Section 19.1.

    python eval/make_testbed.py --seed 7 --out testbed/

Lays out the 200 files of eval/categories.yaml for the current OS under testbed/files/ (the
folder that gets scanned) and writes testbed/labels.csv beside it, so the labels file is
never part of the scan. The same seed gives the same files, ages and dev/test split on every OS
(only installer extensions and junk names follow the OS).
"""
from __future__ import annotations

import argparse
import csv
import itertools
import os
import random
import shutil
import sys
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cleanguard.platform import current_os, load_profile  # noqa: E402

CATEGORIES = Path(__file__).parent / "categories.yaml"
SYNTH = "SAMPLE - SYNTHETIC DATA"
DAY = 86400
N_DEV = 60
EXTRA_PROTECTED = ("**/app_config/**",)      # the eval profile marks this folder protected


# ------------------------------------------------------------------ fake data
def thai_id(rng: random.Random) -> str:
    d = [rng.randint(1, 8)] + [rng.randint(0, 9) for _ in range(11)]
    d.append((11 - sum(d[i] * (13 - i) for i in range(12)) % 11) % 10)
    s = "".join(map(str, d))
    return f"{s[0]}-{s[1:5]}-{s[5:10]}-{s[10:12]}-{s[12]}"


def card_number(rng: random.Random) -> str:
    d = [4] + [rng.randint(0, 9) for _ in range(14)]
    total = 0
    for i, x in enumerate(reversed(d)):              # check digit position is index 0
        y = x * 2 if i % 2 == 0 else x
        total += y - 9 if y > 9 else y
    d.append((10 - total % 10) % 10)
    s = "".join(map(str, d))
    return " ".join(s[i:i + 4] for i in range(0, 16, 4))


NAMES = ["Somchai Jaidee", "Ananya Srisuk", "Krit Wongsa", "Pimchanok Rattana", "Arisa Boonmee"]


def sensitive_text(kind: str, rng: random.Random) -> str:
    who = rng.choice(NAMES)
    if kind == "thai_id_card":
        return (f"{SYNTH}\nThai National ID Card\nName: {who}\nIdentification Number: "
                f"{thai_id(rng)}\nDate of Birth: {rng.randint(1, 28)} Mar {rng.randint(1980, 2004)}\n")
    if kind == "bank_statement":
        rows = "\n".join(f"{rng.randint(1, 28):02d}/05  Transfer  {rng.randint(100, 9000):>8}.00"
                         for _ in range(8))
        return (f"{SYNTH}\nMonthly account statement\nAccount holder: {who}\nCard: "
                f"{card_number(rng)}\n{rows}\nClosing balance: {rng.randint(1000, 90000)}.00 THB\n")
    if kind == "payslip":
        return (f"{SYNTH}\nPayslip\nEmployee: {who}\nID: {thai_id(rng)}\nGross salary: "
                f"{rng.randint(25, 90)},000 THB\nSocial security: 750 THB\n")
    if kind == "password_list":
        return (f"{SYNTH}\n" + "\n".join(f"{s}: {who.split()[0].lower()}{rng.randint(10, 99)}!"
                                         f"{rng.choice('abcxyz')}" for s in
                                         ["email", "bank app", "wifi", "laptop", "cloud"]) +
                "\nrecovery codes: " + " ".join(f"{rng.randint(1000, 9999)}" for _ in range(6)) + "\n")
    if kind == "medical_letter":
        return (f"{SYNTH}\nMedical certificate\nPatient: {who}\nID: {thai_id(rng)}\n"
                f"Diagnosis: acute bronchitis. Rest for {rng.randint(2, 7)} days.\n")
    if kind == "ambiguous":
        return rng.choice([sensitive_text("thai_id_card", rng), sensitive_text("bank_statement", rng),
                           sensitive_text("payslip", rng)])
    raise ValueError(kind)


BENIGN = [
    "Green curry recipe\n400 ml coconut milk, 2 tbsp curry paste, chicken, basil, eggplant.\n"
    "Fry the paste, add the milk, simmer 15 minutes.\n",
    "Club meeting minutes\nAgenda: room booking, poster for the open house, snacks budget.\n"
    "Next meeting on Friday at 5 pm.\n",
    "Packing list for the trip\nCharger, umbrella, sunscreen, two shirts, hiking shoes.\n",
    "Reading list\n1. The Pragmatic Programmer\n2. Designing Data-Intensive Applications\n",
    "Wi-Fi router manual (excerpt)\nPress and hold the reset button for 10 seconds.\n",
]

LECTURE = ("Week {n}: {course}\nLearning objectives\n- understand the main idea of this week\n"
           "- work through two examples in class\nReading: chapter {n} of the course book.\n"
           "Exercises are posted on the course page.\n")


_doc_no = itertools.count(1)


def unique_text(text: str) -> str:
    """Every generated document differs, so only the intended copies are exact duplicates."""
    return f"{text}\nDocument no. {next(_doc_no):04d}\n"


def write_pdf(path: Path, text: str) -> None:
    from fpdf import FPDF
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=11)
    pdf.multi_cell(0, 6, unique_text(text))
    pdf.output(str(path))


def write_docx(path: Path, text: str) -> None:
    import docx
    d = docx.Document()
    for line in unique_text(text).splitlines():
        d.add_paragraph(line)
    d.save(str(path))


def write_doc(path: Path, text: str, rng: random.Random) -> None:
    ext = path.suffix.lower()
    if ext == ".pdf":
        write_pdf(path, text)
    elif ext == ".docx":
        write_docx(path, text)
    elif ext in (".txt", ".md", ".csv", ".log", ".json", ".ini"):
        path.write_text(unique_text(text), encoding="utf-8")
    else:                                            # images, archives, binaries
        path.write_bytes(rng.randbytes(rng.randint(20_000, 400_000)))


# ------------------------------------------------------------------- builder
class Builder:
    def __init__(self, out: Path, rng: random.Random, os_name: str, vocab: dict):
        self.out, self.rng, self.os, self.vocab = out, rng, os_name, vocab
        self.rows: list[dict] = []
        self.used: set[str] = set()
        self.counter = 0

    def fill(self, template: str, **extra) -> str:
        self.counter += 1
        v = {k: self.rng.choice(vals) for k, vals in self.vocab.items()}
        v.update(n=self.rng.randint(1, 40), year=self.rng.randint(2019, 2025))
        v.update(extra)
        return template.format(**v)

    def unique(self, folder: str, make) -> str:
        for _ in range(200):
            rel = (folder + make()).replace("//", "/")
            if rel.lower() not in self.used:
                self.used.add(rel.lower())
                return rel
        raise RuntimeError(f"could not find a free name in {folder}")

    def add(self, rel: str, cat: dict, writer, age: int, acceptable=None, sensitive=None) -> dict:
        p = self.out / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        writer(p)
        mtime = time.time() - age * DAY - self.rng.randint(0, DAY // 2)
        os.utime(p, (mtime, mtime))
        row = {"rel_path": rel, "category": cat["category"],
               "acceptable": "|".join(acceptable or cat["acceptable"]),
               "sensitive": str(cat["sensitive"] if sensitive is None else sensitive).lower(),
               "split": "", "size_bytes": p.stat().st_size, "_age": age}
        self.rows.append(row)
        return row

    def age(self, cat) -> int:
        lo, hi = cat["age_days"]
        return self.rng.randint(lo, hi)

    def names(self, cat) -> list[str]:
        n = cat["names"]
        return n[self.os] if isinstance(n, dict) else n


def build(out: Path, seed: int, os_name: str) -> list[dict]:
    spec = yaml.safe_load(CATEGORIES.read_text(encoding="utf-8"))
    rng = random.Random(seed)
    b = Builder(out, rng, os_name, spec["vocab"])
    prof = load_profile(os_name)
    by_cat: dict[str, list[dict]] = {}

    for cat in spec["categories"]:
        rows = by_cat.setdefault(cat["category"], [])
        gen, count = cat["gen"], cat["count"]
        for i in range(count):
            names = b.names(cat) if "names" in cat else []
            if gen == "installer":
                rel = b.unique(cat["folder"], lambda: b.fill(rng.choice(names)))
                size = int(rng.uniform(*cat["size_mb"]) * 1e6)
                rows.append(b.add(rel, cat, lambda p, s=size: p.write_bytes(rng.randbytes(s)), b.age(cat)))
            elif gen == "sensitive":
                kind = cat["kinds"][i % len(cat["kinds"])].rsplit("_", 1)
                base, ext = kind[0], "." + kind[1]
                label = {"thai_id_card": "id card {first}", "bank_statement": "bank_statement_{month}",
                         "payslip": "payslip_{month}_{year}", "password_list": "passwords_{n}",
                         "medical_letter": "medical_letter_{n}"}[base]
                rel = b.unique(cat["folder"], lambda: b.fill(label) + ext)
                text = sensitive_text(base, rng)
                rows.append(b.add(rel, cat, lambda p, t=text: write_doc(p, t, rng), b.age(cat)))
            elif gen in ("ambiguous_sensitive", "ambiguous_benign"):
                rel = b.unique(cat["folder"], lambda: b.fill(rng.choice(names)))
                text = sensitive_text("ambiguous", rng) if gen == "ambiguous_sensitive" \
                    else rng.choice(BENIGN)
                rows.append(b.add(rel, cat, lambda p, t=text: write_doc(p, t, rng), b.age(cat)))
            elif gen == "lecture":
                course = rng.choice(spec["vocab"]["course"])
                rel = b.unique(cat["folder"], lambda: b.fill(rng.choice(names), course=course))
                text = LECTURE.format(n=i + 1, course=course.upper())
                rows.append(b.add(rel, cat, lambda p, t=text: write_pdf(p, t), b.age(cat)))
            elif gen == "duplicate":
                pass                                  # built after all other categories
            elif gen == "junk":
                folder = cat["folders"][i % len(cat["folders"])]
                tmpl = names[i % len(names)]
                rel = b.unique(folder, lambda: b.fill(tmpl))
                assert prof.is_junk_name(Path(rel).name), rel
                rows.append(b.add(rel, cat, lambda p: p.write_bytes(rng.randbytes(rng.randint(500, 40_000))),
                                  b.age(cat)))
            elif gen == "partial":
                ext = rng.choice(prof.partial_download_ext)
                rel = b.unique(cat["folder"], lambda: b.fill(rng.choice(names), partial=ext))
                rows.append(b.add(rel, cat, lambda p: p.write_bytes(rng.randbytes(rng.randint(50_000, 900_000))),
                                  b.age(cat)))
            elif gen == "recent":
                rel = b.unique(cat["folder"], lambda: b.fill(rng.choice(names)))
                rows.append(b.add(rel, cat, lambda p: write_doc(p, rng.choice(BENIGN), rng), b.age(cat)))
            elif gen == "personal":
                rel = b.unique(cat["folder"], lambda: b.fill(rng.choice(names)))
                text = "Curriculum vitae\nEducation: B.Eng. Computer Engineering\nSkills: Python, SQL\n" \
                    if "CV" in rel else "Certificate of completion\nThis certifies that the course was completed.\n"
                rows.append(b.add(rel, cat, lambda p, t=text: write_doc(p, t, rng), b.age(cat)))
            elif gen in ("draft", "final"):
                project = spec["vocab"]["project"][i % len(spec["vocab"]["project"])]
                folder = cat["folder"].format(project=project)
                rel = b.unique(folder, lambda: b.fill(names[0], project=project, n=i % 3 + 1))
                text = f"{project.replace('_', ' ').title()}\n" + \
                    ("Draft. Some sections are still missing.\n" if gen == "draft" else "Final version.\n")
                age = b.age(cat)
                rows.append(b.add(rel, cat, lambda p, t=text: write_docx(p, t), age))
            elif gen == "archive":
                rel = b.unique(cat["folder"], lambda: b.fill(rng.choice(names)))
                size = int(rng.uniform(*cat["size_mb"]) * 1e6)
                rows.append(b.add(rel, cat, lambda p, s=size: p.write_bytes(rng.randbytes(s)), b.age(cat)))
            elif gen == "protected":
                rel = names[i]
                b.used.add(rel.lower())
                rows.append(b.add(rel, cat, lambda p: p.write_text(f"generated file {i}\n", encoding="utf-8"),
                                  b.age(cat)))
            else:
                raise ValueError(f"unknown gen {gen!r}")

    # exact duplicates: copies of earlier files, always newer than the original
    dup = next(c for c in spec["categories"] if c["gen"] == "duplicate")
    n_sens = dup["sensitive_copies"]
    plain = by_cat["old_installers"] + by_cat["course_materials"]
    sources = rng.sample(plain, dup["count"] - n_sens) + rng.sample(by_cat["sensitive_fake"], n_sens)
    for src in sources:
        sp = Path(src["rel_path"])
        folder = sp.parent.as_posix() + "/"
        tmpl = rng.choice(dup["names"])
        rel = b.unique(folder, lambda: tmpl.format(stem=sp.stem, ext=sp.suffix))
        age = rng.randint(31, max(31, src["_age"] - 5))
        sens = src["sensitive"] == "true"
        b.add(rel, dup, lambda p, s=out / src["rel_path"]: shutil.copyfile(s, p), age,
              acceptable=dup["acceptable_sensitive"] if sens else dup["acceptable"], sensitive=sens)

    # fixed split by the seed: 60 dev, 140 test
    dev = set(rng.sample(range(len(b.rows)), N_DEV))
    for i, r in enumerate(b.rows):
        r["split"] = "dev" if i in dev else "test"
    return b.rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default=str(ROOT / "testbed"))
    ap.add_argument("--os", default=current_os(), choices=["windows", "macos", "linux"],
                    help="lay out names for this OS (default: the current one)")
    ap.add_argument("--force", action="store_true", help="replace an existing testbed")
    a = ap.parse_args(argv)
    out = Path(a.out).resolve()
    if out.exists() and any(out.iterdir()):
        if not (a.force and (out / "labels.csv").exists()):
            print(f"{out} is not empty. Use --force to replace an existing testbed "
                  "(only a folder holding labels.csv is replaced).")
            return 2
        shutil.rmtree(out)
    (out / "files").mkdir(parents=True)
    rows = build(out / "files", a.seed, a.os)
    with open(out / "labels.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["rel_path", "category", "acceptable", "sensitive",
                                          "split", "size_bytes"], extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(f"{len(rows)} files in {out / 'files'} ({a.os}), labels in labels.csv "
          f"({sum(r['split'] == 'dev' for r in rows)} dev / {sum(r['split'] == 'test' for r in rows)} test)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
