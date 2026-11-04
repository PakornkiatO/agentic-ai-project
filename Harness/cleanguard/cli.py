"""CLI (M18, D).

    python -m cleanguard scan ~/Downloads [--config C4] [--dry-run]
    python -m cleanguard approve <run_id>
    python -m cleanguard restore <batch_id>
    python -m cleanguard runs                 # list runs and quarantine batches
    python -m cleanguard resume <run_id>      # continue a FAILED run (e.g. Ollama was down)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from harness import Harness, execute

from . import quarantine
from .platform import load_profile
from .report import console_summary, groups, mb
from .safety import GATE
from .settings import DEFAULT_SETTINGS, load_settings
from .state_machine import S, CleanGuardRun, load_run, new_run, run_dir

__all__ = ["main", "approve", "parse_selection"]


class Cancelled(Exception):
    pass


def parse_selection(text: str, n: int) -> list[int]:
    """'all', 'none', '' or numbers like '1,3,7-12' -> sorted 1-based indexes within 1..n."""
    t = text.strip().lower()
    if t in ("", "none"):
        return []
    if t == "all":
        return list(range(1, n + 1))
    out = set()
    for part in t.replace(" ", "").split(","):
        if not part:
            continue
        a, sep, b = part.partition("-")
        lo, hi = (int(a), int(b)) if sep else (int(a), int(a))
        if lo > hi or lo < 1 or hi > n:
            raise ValueError(f"{part} is outside 1-{n}")
        out.update(range(lo, hi + 1))
    return sorted(out)


def _ask(ask, prompt: str) -> str:
    try:
        return ask(prompt)
    except (EOFError, KeyboardInterrupt):
        raise Cancelled from None


def _pick(ask, say, prompt: str, numbered: dict[int, object]) -> list:
    while True:
        text = _ask(ask, prompt)
        try:
            idx = parse_selection(text, max(numbered, default=0))
        except ValueError as e:
            say(f"  {e}. Try again, or press Ctrl+C to stop.")
            continue
        bad = [i for i in idx if i not in numbered]
        if bad and text.strip().lower() != "all":
            say(f"  numbers {bad} cannot be chosen here. Try again.")
            continue
        return [numbered[i] for i in idx if i in numbered]


def approve(h, run, cfg, ask=input, say=print) -> dict | None:
    """Section 17.3. Returns the quarantine result, or None when nothing moved."""
    runner = CleanGuardRun(h, run, cfg)
    if S(run.state) != S.APPROVE:
        raise RuntimeError(f"run {run.run_id} is in state {run.state}, not APPROVE")
    g = groups(run)
    ordered = g["deletable"] + g["review"] + g["sensitive"]       # report numbering
    num = {i: x for i, x in enumerate(ordered, 1)}
    dele = {i: x for i, x in num.items() if x[1].label == "deletable"}
    revw = {i: x for i, x in num.items() if x[1].label == "review"}
    try:
        if not dele and not revw:
            say("Nothing to approve.")
            raise Cancelled
        say(f"Deletable files ({len(dele)}):")
        for i, (r, d) in dele.items():
            say(f"  {i:>4}. {mb(r.size_bytes):>10}  {r.rel_path}  - {d.reason}")
        chosen = _pick(ask, say, "Move which files? (all / none / e.g. 1,3,7-12): ", dele) if dele else []

        if revw:
            say(f"Review files ({len(revw)}), not selected unless you add them:")
            for i, (r, d) in revw.items():
                tag = f" [SENSITIVE: {d.sensitive_type}]" if d.sensitive else ""
                say(f"  {i:>4}. {mb(r.size_bytes):>10}  {r.rel_path}{tag}  - {d.reason}")
            extra = _pick(ask, say, "Add review files by number (Enter for none): ", revw)
            sens = [x for x in extra if x[1].sensitive]
            if sens:
                say(f"{len(sens)} of these may hold personal data:")
                for r, d in sens:
                    say(f"    {r.rel_path} ({d.sensitive_type})")
                if _ask(ask, "Type SENSITIVE to include them, anything else to leave them: ") \
                        .strip() != "SENSITIVE":
                    extra = [x for x in extra if not x[1].sensitive]
            chosen += extra

        if not chosen:
            say("Nothing selected. Nothing was moved.")
            raise Cancelled
        total = sum(r.size_bytes for r, _ in chosen)
        say(f"{len(chosen)} files, {mb(total)}, will move to {run.profile.quarantine_dir}")
        say("They can be restored at any time. Nothing is deleted permanently.")
        if _ask(ask, "Type MOVE to continue: ").strip() != "MOVE":
            say("Cancelled. Nothing was moved.")
            raise Cancelled
    except Cancelled:
        runner.go(S.DONE)
        return None

    ids = [r.file_id for r, _ in chosen]
    token = GATE.issue(run.run_id, ids, cfg.approval.token_ttl_minutes)
    runner.go(S.QUARANTINE)
    res = execute({"function": {"name": "quarantine_files",
                                "arguments": {"file_ids": ids, "approval_token": token}}},
                  allowed={"read", "write"}, logger=h.log, run_id=run.run_id)
    if res["ok"]:
        q = res["result"]
        run.approved, run.batch_id = ids, q["batch_id"]
        run.quarantine_batches.append(q["batch_id"])
        h.log.write("quarantine_batch", run_id=run.run_id, batch_id=q["batch_id"],
                    files=len(q["moved"]), bytes=q["bytes"], skipped=len(q["skipped"]),
                    status=q["status"])
        if q["status"] == "rolled_back":
            say(f"An error occurred ({q.get('error')}); every file was put back. Nothing moved.")
        else:
            say(f"Moved {len(q['moved'])} files ({mb(q['bytes'])}) to batch {q['batch_id']}.")
            for s in q["skipped"]:
                r = run.records[s["file_id"]]
                why = "changed since scan" if s["status"] == "changed_since_scan" else "in use"
                say(f"  skipped ({why}): {r.rel_path}")
            say(f"Undo with: python -m cleanguard restore {q['batch_id']}")
    else:
        say(f"Quarantine refused: {res['error']}")
    runner.go(S.DONE)
    return res.get("result")


# ------------------------------------------------------------------- commands
def cmd_scan(a, cfg) -> int:
    root = Path(a.folder).expanduser()
    if not root.is_dir():
        print(f"Not a folder: {root}")
        return 2
    if not cfg.domain_rules and not a.dry_run:
        print(f"Config {cfg.name} disables the domain rules; it runs only with --dry-run.")
        return 2
    h = Harness()
    run = new_run(root, cfg, dry_run=a.dry_run)
    runner = CleanGuardRun(h, run, cfg)
    print(f"Scanning {run.root} (run {run.run_id}, config {cfg.name}) ...")
    state = runner.until_pause()
    return _after(run, cfg, state)


def _after(run, cfg, state) -> int:
    if state == S.FAILED.value:
        print(f"Run {run.run_id} failed: {run.error}. Nothing was moved.")
        if run.failed_from != S.SCAN.value:
            fix = ("Pull the models (`ollama pull qwen3:4b-q4_K_M` and `ollama pull qwen3:8b-q4_K_M`)"
                   if run.error == "ModelNotFoundError" else "Start Ollama (`ollama serve`)")
            print(f"{fix}, then continue with: python -m cleanguard resume {run.run_id}")
        return 1
    print(console_summary(run, run_dir(run, cfg) / "report.html"))
    if state == S.APPROVE.value:
        print(f"Nothing has moved yet. Next: python -m cleanguard approve {run.run_id}")
    return 0


def cmd_resume(a, cfg) -> int:
    run = load_run(a.run_id, cfg)
    state = CleanGuardRun(Harness(), run, cfg).resume()
    return _after(run, cfg, state)


def cmd_approve(a, cfg) -> int:
    run = load_run(a.run_id, cfg)
    if run.state != S.APPROVE.value:
        print(f"Run {run.run_id} is in state {run.state}; only runs waiting in APPROVE can be approved.")
        return 2
    approve(Harness(), run, cfg)
    return 0


def _find_batch_run(batch_id: str, qdir: Path) -> str | None:
    m = Path(qdir) / batch_id / "manifest.json"
    return json.loads(m.read_text(encoding="utf-8")).get("run_id") if m.exists() else None


def cmd_restore(a, cfg) -> int:
    h = Harness()
    run_id = _find_batch_run(a.batch_id, load_profile().quarantine_dir)
    runner = None
    if run_id and (Path(cfg.paths.runs_dir) / run_id / "state.json").exists():
        run = load_run(run_id, cfg)
        runner = CleanGuardRun(h, run, cfg)
        if run.state == S.DONE.value:
            runner.go(S.RESTORE)
    res = execute({"function": {"name": "restore_batch", "arguments": {"batch_id": a.batch_id}}},
                  allowed={"read", "write"}, logger=h.log, run_id=run_id)
    if runner and runner.run.state == S.RESTORE.value:
        runner.go(S.DONE)
    if not res["ok"]:
        print(f"Restore failed: {res['error']}")
        return 1
    r = res["result"]
    print(f"Restored {r['restored']} files from {a.batch_id}"
          + (f" ({r['renamed']} renamed with '(restored)' because the name was taken)" if r["renamed"] else "")
          + (f"; {r['missing']} were missing from the quarantine" if r["missing"] else "") + ".")
    return 0


def cmd_runs(a, cfg) -> int:
    rdir = Path(cfg.paths.runs_dir)
    states = sorted(rdir.glob("*/state.json"), key=lambda p: p.stat().st_mtime) if rdir.exists() else []
    print(f"Runs ({rdir}):" if states else "No runs yet.")
    for p in states:
        s = json.loads(p.read_text(encoding="utf-8"))
        labels = [d["label"] for d in s["decisions"].values()]
        print(f"  {s['run_id']}  {s['state']:<10} {s['config']:<8} {s['created_at']}  "
              f"{len(labels)} decided, {labels.count('deletable')} deletable  {s['root']}")
    qdir = load_profile().quarantine_dir
    batches = quarantine.list_batches(qdir) if qdir.exists() else []
    print(f"Quarantine ({qdir}):" if batches else f"Quarantine is empty ({qdir}).")
    for b in batches:
        print(f"  {b['batch_id']}  {b['status']:<12} {b['files']} files, {mb(b['bytes'])}  "
              f"from run {b['run_id']}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="cleanguard", description="CleanGuard: mark files keep / deletable / review; move only what you approve into a restorable quarantine.")
    ap.add_argument("--settings", default=str(DEFAULT_SETTINGS), help="path to cleanguard.yaml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("scan", help="scan a folder and mark files")
    p.add_argument("folder")
    p.add_argument("--config", help="eval configuration from eval/configs.yaml, e.g. C4")
    p.add_argument("--dry-run", action="store_true", help="stop after the report")
    p = sub.add_parser("approve", help="choose files to move into quarantine")
    p.add_argument("run_id")
    p = sub.add_parser("restore", help="move a quarantine batch back")
    p.add_argument("batch_id")
    sub.add_parser("runs", help="list runs and quarantine batches")
    p = sub.add_parser("resume", help="continue a run that FAILED")
    p.add_argument("run_id")
    a = ap.parse_args(argv)
    cfg = load_settings(a.settings, getattr(a, "config", None))
    return {"scan": cmd_scan, "approve": cmd_approve, "restore": cmd_restore,
            "runs": cmd_runs, "resume": cmd_resume}[a.cmd](a, cfg)


if __name__ == "__main__":
    sys.exit(main())
