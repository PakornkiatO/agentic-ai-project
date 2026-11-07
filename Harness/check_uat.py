"""Check one UAT task against data/test_set.jsonl.

    python check_uat.py --task T04 --run runs/<run_id>
    python check_uat.py --task T02                      # folder-only checks

Exit code 0 = every automatic check passed. Manual checks are printed for the observer.
uat_session.py imports evaluate() from here.
"""
import argparse, hashlib, json, sys
from pathlib import Path

INSTALLER = (".exe", ".dmg")
LABELS = ("keep", "deletable", "review")


def load_tasks(tests: Path) -> list:
    return [json.loads(l) for l in Path(tests).read_text(encoding="utf-8").splitlines() if l.strip()]


def find_task(tasks: list, task_id: str) -> dict:
    for t in tasks:
        if t["id"] == task_id:
            return t
    raise SystemExit(f"unknown task {task_id}")


def folder_specs(tasks: list, folder: str) -> list:
    """Files of a folder; tasks that reuse a folder (T02, T05) have no 'files' of their own."""
    for t in tasks:
        if t.get("folder") == folder and t.get("files"):
            return t["files"]
    return []


def _find(d: dict, *keys):
    for k in keys:
        if k in d:
            return d[k]
    return None


def load_run(run_dir: Path) -> dict:
    """rel_path -> {label, sensitive, reason}.

    Expects runs/<run_id>/state.json with "decisions" keyed by file_id, and files.json mapping
    file_id -> {"rel_path": ...}. If your run files differ, change only this function.
    """
    run_dir = Path(run_dir)
    state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    files = json.loads((run_dir / "files.json").read_text(encoding="utf-8"))
    out = {}
    for fid, d in state["decisions"].items():
        f = files.get(fid, {})
        rel = _find(f, "rel_path", "path", "name") if isinstance(f, dict) else f
        out[str(rel).replace("\\", "/")] = {
            "label": d.get("label"),
            "sensitive": bool(d.get("sensitive")),
            "reason": d.get("reason", ""),
        }
    return out


def resolve(spec_path: str, manifest: dict) -> str:
    if "{inst}" not in spec_path:
        return spec_path
    for ext in INSTALLER:
        name = spec_path.replace("{inst}", ext)
        if name in manifest:
            return name
    return spec_path


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def evaluate(task: dict, tasks: list, folders: Path, run_dir=None) -> dict:
    """Run every automatic check for one task. Returns a dict with checks and counts."""
    exp = task["expected"]
    res = {"task": task["id"], "uat": task["uat"], "type": task["type"], "checks": [],
           "labeled": 0, "acceptable": 0, "sensitive_total": 0, "sensitive_flagged": 0,
           "sensitive_deletable": 0, "manual": task.get("manual", [])}

    def check(name, ok, detail=""):
        res["checks"].append({"name": name, "ok": bool(ok), "detail": detail})

    if task.get("folder") is None:
        res["note"] = "no folder: result comes from the observer sheet"
        return res

    folders = Path(folders)
    manifest = json.loads((folders / f"{task['folder']}.manifest.json").read_text(encoding="utf-8"))
    specs = {resolve(s["path"], manifest): s for s in folder_specs(tasks, task["folder"])}
    root = folders / task["folder"]
    sens = [p for p, s in specs.items() if s["sensitive"]]
    res["sensitive_total"] = len(sens)

    if exp.get("labels"):
        if run_dir is None:
            check("run found for this task", False, "pass --run runs/<run_id>")
        else:
            labels = load_run(Path(run_dir))
            missing = [p for p in specs if p not in labels]
            check("every file labeled", not missing, ", ".join(missing))
            wrong = [f"{p}: {labels[p]['label']}" for p, s in specs.items()
                     if p in labels and labels[p]["label"] not in s["acceptable"]]
            res["labeled"] = sum(p in labels for p in specs)
            res["acceptable"] = res["labeled"] - len(wrong)
            check("labels in acceptable set", not wrong, "; ".join(wrong))
            sens_del = [p for p in sens if labels.get(p, {}).get("label") == "deletable"]
            res["sensitive_deletable"] = len(sens_del)
            res["sensitive_flagged"] = sum(labels.get(p, {}).get("sensitive", False) for p in sens)
            check("HARD GATE: 0 sensitive files deletable", not sens_del, ", ".join(sens_del))
            if "min_sensitive_flagged" in exp:
                check(f"at least {exp['min_sensitive_flagged']} sensitive files flagged",
                      res["sensitive_flagged"] >= exp["min_sensitive_flagged"],
                      f"{res['sensitive_flagged']} of {len(sens)}")

    may_change = {resolve(p, manifest) for p in exp.get("present", [])}
    # a later task on the same folder may legitimately move a file (T05 after T04), so allow it for all of them
    may_move = {resolve(p, manifest) for t in tasks if t.get("folder") == task["folder"]
                for p in t["expected"].get("may_move", [])}
    must_stay = {p for p, s in specs.items() if "deletable" not in s["acceptable"] or s["sensitive"]}
    if exp.get("all_present_unchanged"):
        must_stay = set(specs)
    must_stay = (must_stay - may_move) | may_change
    if exp.get("never_moved") or exp.get("all_present_unchanged") or may_change:
        bad = []
        for p in sorted(must_stay):
            f = root / p
            if not f.exists():
                bad.append(f"{p} missing")
            elif p not in may_change and sha256(f) != manifest[p]:
                bad.append(f"{p} changed")
        check("no needed file lost or changed", not bad, "; ".join(bad))
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True)
    ap.add_argument("--tests", default="data/test_set.jsonl")
    ap.add_argument("--folders", default="uat_folders")
    ap.add_argument("--run")
    a = ap.parse_args()
    tasks = load_tasks(Path(a.tests))
    r = evaluate(find_task(tasks, a.task), tasks, Path(a.folders), a.run)
    print(f"{r['task']} · {r['uat']} · {r['type']}")
    for c in r["checks"]:
        print(f"{'PASS' if c['ok'] else 'FAIL'}  {c['name']}" + (f"  ({c['detail']})" if c["detail"] else ""))
    if r.get("note"):
        print(r["note"])
    for m in r["manual"]:
        print(f"MANUAL  {m}")
    sys.exit(0 if all(c["ok"] for c in r["checks"]) else 1)


if __name__ == "__main__":
    main()
