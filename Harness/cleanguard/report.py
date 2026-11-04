"""Report (M18, D): static report.html (no scripts, no external assets), report.json and
a console summary. File names are untrusted input: every one is escaped."""
from __future__ import annotations

import json
from html import escape
from pathlib import Path

from .quarantine import quarantine_size
from .schemas import human_size


def mb(n: int) -> str:
    return human_size(n)        # "0.0 MB" for a 2 KB document reads as empty, to people too


def groups(run) -> dict[str, list]:
    """Deletable (largest first), Review, Sensitive, Keep. Sensitive files appear only in
    Sensitive; they are never deletable."""
    rows = [(run.records[f], d) for f, d in run.decisions.items()]
    g = {
        "deletable": [x for x in rows if x[1].label == "deletable" and not x[1].sensitive],
        "review": [x for x in rows if x[1].label == "review" and not x[1].sensitive],
        "sensitive": [x for x in rows if x[1].sensitive],
        "keep": [x for x in rows if x[1].label == "keep" and not x[1].sensitive],
    }
    g["deletable"].sort(key=lambda x: -x[0].size_bytes)
    g["review"].sort(key=lambda x: -x[0].size_bytes)
    g["sensitive"].sort(key=lambda x: x[0].rel_path)
    return g


def _folder(rel: str) -> str:
    return rel.rsplit("/", 1)[0] + "/" if "/" in rel else "./"


CSS = """
:root{--bg:#fbfbfa;--fg:#1d1d1b;--muted:#6b6b66;--line:#e4e3df;--card:#fff;
--del:#b4441b;--rev:#8a6d00;--sen:#6a3fb5;--keep:#2f7a4a}
@media (prefers-color-scheme: dark){:root{--bg:#171716;--fg:#ecebe7;--muted:#9a9a94;
--line:#2e2e2b;--card:#1f1f1d;--del:#f08a5d;--rev:#e3c25a;--sen:#b79cf0;--keep:#7cc79a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1100px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:32px 0 8px}
.muted{color:var(--muted)}.tiles{display:flex;flex-wrap:wrap;gap:12px;margin:20px 0}
.tile{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 14px;min-width:140px}
.tile b{display:block;font-size:20px}.wrap{overflow-x:auto}
table{border-collapse:collapse;width:100%;background:var(--card);font-size:13.5px}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-weight:600;color:var(--muted)}td.num{text-align:right;white-space:nowrap}
td.name{word-break:break-all}.badge{display:inline-block;padding:0 6px;border-radius:4px;
font-size:12px;border:1px solid currentColor}
.del{color:var(--del)}.rev{color:var(--rev)}.sen{color:var(--sen)}.keep{color:var(--keep)}
"""


def _table(rows, start: int, badge: bool = False) -> str:
    out = ["<div class='wrap'><table><tr><th>#</th><th>Name</th><th>Folder</th><th>Size</th>"
           "<th>Age</th>" + ("<th>Type</th><th>Label</th>" if badge else "") +
           "<th>Reason</th><th>Decided by</th></tr>"]
    for i, (r, d) in enumerate(rows, start):
        extra = (f"<td><span class='badge sen'>{escape(d.sensitive_type)}</span></td>"
                 f"<td>{escape(d.label)}</td>") if badge else ""
        out.append(f"<tr><td class='num'>{i}</td><td class='name'>{escape(r.name)}</td>"
                   f"<td class='name'>{escape(_folder(r.rel_path))}</td>"
                   f"<td class='num'>{mb(r.size_bytes)}</td><td class='num'>{r.modified_days} d</td>"
                   f"{extra}<td>{escape(d.reason)}</td><td>{escape(d.decided_by)}</td></tr>")
    out.append("</table></div>")
    return "\n".join(out)


def render_html(run) -> str:
    g = groups(run)
    rec = sum(r.size_bytes for r, _ in g["deletable"])
    qdir = run.profile.quarantine_dir
    qsize = quarantine_size(qdir) if Path(qdir).exists() else 0
    tiles = [("Files scanned", len(run.records)), ("Deletable", len(g["deletable"])),
             ("Reclaimable", mb(rec)), ("Review", len(g["review"])),
             ("Sensitive", len(g["sensitive"])), ("Keep", len(g["keep"]))]
    parts = [
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        f"<title>CleanGuard report</title><style>{CSS}</style></head><body><main>",
        "<h1>CleanGuard report</h1>",
        f"<div class='muted'>Run {escape(run.run_id)} · config {escape(run.config_name)} · "
        f"{escape(run.profile.name)} · {escape(run.created_at)} · folder "
        f"{escape(run.root.as_posix())}</div>",
        "<div class='tiles'>" + "".join(f"<div class='tile'><span class='muted'>{k}</span>"
                                        f"<b>{v}</b></div>" for k, v in tiles) + "</div>",
        "<p>Nothing has been moved. Approve files with <code>python -m cleanguard approve "
        f"{escape(run.run_id)}</code>. Approved files go to a quarantine you can restore from; "
        "CleanGuard never deletes anything permanently.</p>",
        f"<h2 class='del'>Deletable ({len(g['deletable'])}, {mb(rec)})</h2>",
        _table(g["deletable"], 1),
        f"<h2 class='rev'>Review ({len(g['review'])})</h2>",
        "<p class='muted'>CleanGuard was not sure. You can add these by number when approving.</p>",
        _table(g["review"], len(g["deletable"]) + 1),
        f"<h2 class='sen'>Sensitive ({len(g['sensitive'])})</h2>",
        "<p class='muted'>These probably hold personal data. They are never marked deletable.</p>",
        _table(g["sensitive"], len(g["deletable"]) + len(g["review"]) + 1, badge=True),
        f"<h2 class='keep'>Keep ({len(g['keep'])})</h2>",
        f"<p class='muted'>{len(g['keep'])} files are kept (protected, recent or still useful).</p>",
        "<h2>Quarantine</h2>",
        f"<p>Location: <code>{escape(Path(qdir).as_posix())}</code> · currently holds "
        f"{mb(qsize)}. CleanGuard never empties it; delete it yourself when you are sure.</p>",
    ]
    if run.skipped:
        parts.append(f"<p class='muted'>{len(run.skipped)} paths could not be read during the scan.</p>")
    parts.append("</main></body></html>")
    return "\n".join(parts)


def write_report(run, out_dir: Path) -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.html").write_text(render_html(run), encoding="utf-8")
    rows = [{"file_id": f, "rel_path": run.records[f].rel_path, "size_bytes": run.records[f].size_bytes,
             "modified_days": run.records[f].modified_days, "label": d.label,
             "sensitive": d.sensitive, "sensitive_type": d.sensitive_type, "reason": d.reason,
             "decided_by": d.decided_by} for f, d in run.decisions.items()]
    (out_dir / "report.json").write_text(json.dumps(
        {"run_id": run.run_id, "config": run.config_name, "os": run.profile.name, "files": rows},
        indent=1, ensure_ascii=False), encoding="utf-8")
    return out_dir / "report.html"


def console_summary(run, report_path: Path | None = None) -> str:
    g = groups(run)
    rec = sum(r.size_bytes for r, _ in g["deletable"])
    lines = [f"Run {run.run_id} ({run.config_name}, {run.profile.name}): "
             f"{len(run.records)} files scanned",
             f"  deletable  {len(g['deletable']):>5}   {mb(rec)} reclaimable",
             f"  review     {len(g['review']):>5}",
             f"  sensitive  {len(g['sensitive']):>5}   (never deletable)",
             f"  keep       {len(g['keep']):>5}"]
    for r, d in g["deletable"][:10]:
        lines.append(f"    {mb(r.size_bytes):>10}  {r.rel_path}  - {d.reason}")
    if len(g["deletable"]) > 10:
        lines.append(f"    ... and {len(g['deletable']) - 10} more in the report")
    if run.skipped:
        lines.append(f"  {len(run.skipped)} paths could not be read "
                     "(on macOS, grant the terminal Files and Folders access)")
    if report_path:
        lines.append(f"Report: {report_path}")
    return "\n".join(lines)
