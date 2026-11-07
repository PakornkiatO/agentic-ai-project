"""Build the UAT task folders from data/test_set.jsonl.

    python make_uat_folders.py --out uat_folders [--seed 4] [--os auto]

Each folder gets <folder>.manifest.json beside it: rel_path -> sha256 at build time.
All personal data is synthetic. Needs: python-docx, fpdf2.
"""
import argparse, hashlib, json, os, platform, random, shutil, time
from pathlib import Path

INSTALLER = {"windows": ".exe", "macos": ".dmg"}


def current_os():
    name = {"Windows": "windows", "Darwin": "macos"}.get(platform.system())
    if name is None:
        raise SystemExit("UAT supports Windows and macOS only; pass --os windows or --os macos")
    return name


def thai_id(rng):
    d = [rng.randint(1, 8)] + [rng.randint(0, 9) for _ in range(11)]
    check = (11 - sum(d[i] * (13 - i) for i in range(12)) % 11) % 10
    s = "".join(map(str, d + [check]))
    return f"{s[0]}-{s[1:5]}-{s[5:10]}-{s[10:12]}-{s[12]}"


def card(rng):
    d = [4] + [rng.randint(0, 9) for _ in range(14)]
    total = 0
    for i, x in enumerate(reversed(d)):          # check digit position counts as index 0
        y = x * 2 if i % 2 == 0 else x
        total += y - 9 if y > 9 else y
    d.append((10 - total % 10) % 10)
    s = "".join(map(str, d))
    return " ".join(s[i:i + 4] for i in range(0, 16, 4))


def write_text_file(path: Path, text: str):
    ext = path.suffix.lower()
    if ext == ".docx":
        import docx
        doc = docx.Document()
        doc.add_paragraph(text)
        doc.save(str(path))
    elif ext == ".pdf":
        from fpdf import FPDF
        pdf = FPDF()
        pdf.add_page()
        pdf.set_font("Helvetica", size=12)
        pdf.multi_cell(0, 8, text)               # ASCII only: core fonts are Latin-1
        pdf.output(str(path))
    else:
        path.write_text(text, encoding="utf-8")


def image_only_pdf(path: Path):
    from fpdf import FPDF
    pdf = FPDF()
    pdf.add_page()
    for i in range(12):                          # grey bars: looks scanned, no text layer
        pdf.set_fill_color(120 + i * 8)
        pdf.rect(20, 20 + i * 18, 170, 10, style="F")
    pdf.output(str(path))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(task, out: Path, os_name: str, rng):
    folder = out / task["folder"]
    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir(parents=True)
    now = time.time()
    manifest = {}
    for spec in task["files"]:
        rel = spec["path"].replace("{inst}", INSTALLER[os_name])
        path = folder / rel
        content = spec["content"].replace("{thai_id}", thai_id(rng)).replace("{card}", card(rng))
        if content == "random":
            path.write_bytes(rng.randbytes(spec.get("size_kb", 100) * 1024))
        elif content == "pdf_image_only":
            image_only_pdf(path)
        elif content.startswith("dup:"):
            src = folder / content[4:].replace("{inst}", INSTALLER[os_name])
            shutil.copyfile(src, path)
        elif content.startswith("text:"):
            write_text_file(path, content[5:])
        else:
            raise ValueError(f"unknown content kind in {rel}")
        mtime = now - spec["age_days"] * 86400
        os.utime(path, (mtime, mtime))
        manifest[rel] = sha256(path)
    (out / f"{task['folder']}.manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    return len(manifest)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tests", default="data/test_set.jsonl")
    ap.add_argument("--out", default="uat_folders")
    ap.add_argument("--seed", type=int, default=4)
    ap.add_argument("--os", default="auto", choices=["auto", "windows", "macos"])
    a = ap.parse_args()
    os_name = current_os() if a.os == "auto" else a.os
    rng = random.Random(a.seed)
    out = Path(a.out)
    out.mkdir(exist_ok=True)
    for line in Path(a.tests).read_text(encoding="utf-8").splitlines():
        task = json.loads(line)
        if task.get("files"):
            n = build(task, out, os_name, rng)
            print(f"{task['id']}: {task['folder']} ({n} files)")


if __name__ == "__main__":
    main()
