"""Evaluation runner (M20, D). Section 19.3.

    python eval/make_testbed.py --seed 7 --out testbed/
    python eval/run_eval.py --config C4 --split test --runs 3

Every evaluation run is a dry run: it stops at REPORT, so QUARANTINE is unreachable.
Each run writes eval/results/<config>_<os>_<run>.csv
(rel_path, label, sensitive, sensitive_type, decided_by, batch_wall_s, run_id)
and the metrics of all runs go to eval/results/summary_<config>_<os>.csv.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))

from harness import Harness  # noqa: E402

from cleanguard.platform import load_profile  # noqa: E402
from cleanguard.settings import load_settings  # noqa: E402
from cleanguard.state_machine import CleanGuardRun, new_run  # noqa: E402
from make_testbed import EXTRA_PROTECTED  # noqa: E402
from metrics import log_metrics, metrics, passes  # noqa: E402

RESULTS = Path(__file__).parent / "results"
FIELDS = ["rel_path", "label", "sensitive", "sensitive_type", "decided_by", "batch_wall_s", "run_id"]


def eval_settings(config: str):
    cfg = load_settings(config=config)
    # the testbed holds .git/, .DS_Store and other hidden junk, so hidden files are scanned
    return replace(cfg, scan=replace(cfg.scan, include_hidden=True),
                   paths=replace(cfg.paths, runs_dir=RESULTS / "runs"))


def run_once(h, cfg, testbed: Path, out_csv: Path) -> str:
    profile = load_profile(extra_protected=EXTRA_PROTECTED)
    run = new_run(testbed / "files", cfg, profile=profile, dry_run=True,
                  extra_protected=EXTRA_PROTECTED)
    state = CleanGuardRun(h, run, cfg).until_pause()
    if state != "DONE":
        raise RuntimeError(f"run {run.run_id} ended in {state}: {run.error}")
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for fid, d in run.decisions.items():
            w.writerow({"rel_path": run.records[fid].rel_path, "label": d.label,
                        "sensitive": str(d.sensitive).lower(), "sensitive_type": d.sensitive_type,
                        "decided_by": d.decided_by, "batch_wall_s": run.batch_wall.get(fid, 0.0),
                        "run_id": run.run_id})
    return run.run_id


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Run CleanGuard on the testbed (dry run)")
    ap.add_argument("--config", required=True, help="C0..C5 from eval/configs.yaml")
    ap.add_argument("--split", default="test", choices=["dev", "test"])
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--testbed", default=str(ROOT / "testbed"))
    a = ap.parse_args(argv)

    testbed = Path(a.testbed).resolve()
    labels = testbed / "labels.csv"
    if not labels.exists():
        print(f"No testbed at {testbed}. Run: python eval/make_testbed.py --seed 7 --out testbed/")
        return 2
    cfg = eval_settings(a.config)
    h = Harness()
    os_name = load_profile().name
    RESULTS.mkdir(parents=True, exist_ok=True)
    rows = []
    for i in range(1, a.runs + 1):
        out = RESULTS / f"{a.config}_{os_name}_{i}.csv"
        run_id = run_once(h, cfg, testbed, out)
        m = {"config": a.config, "os": os_name, "run": i, "run_id": run_id, "split": a.split,
             **metrics(out, labels, a.split), **log_metrics(h.log.path, run_id)}
        m["passes"] = json.dumps(passes(m))
        rows.append(m)
        print(f"[{a.config} run {i}] " + ", ".join(
            f"{k}={v:.3f}" if isinstance(v, float) else f"{k}={v}"
            for k, v in m.items() if k not in ("config", "os", "run", "split", "passes")))

    summary = RESULTS / f"summary_{a.config}_{os_name}.csv"
    keys = sorted({k for r in rows for k in r}, key=lambda k: list(rows[0]).index(k) if k in rows[0] else 99)
    with open(summary, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)
    print(f"Summary: {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
