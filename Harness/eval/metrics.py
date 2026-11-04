"""Metrics (M20, D). Section 19.4.

    python eval/metrics.py eval/results/C4_macos_1.csv testbed/labels.csv --split test
    python eval/metrics.py --kappa member_a.csv member_b.csv     # category -> label agreement
"""
from __future__ import annotations

import argparse
import json

import pandas as pd

TARGETS = {
    "sensitive_marked_deletable": ("==", 0),
    "false_delete_rate": ("<=", 0.02),
    "sensitive_recall": (">=", 0.90),
    "reclaim_recall": (">=", 0.70),
    "review_rate": ("<=", 0.25),
    "tool_call_validity": (">=", 0.95),
    "wall_s": ("<=", 600),
}


def _bool(s: pd.Series) -> pd.Series:
    return s.astype(str).str.strip().str.lower().isin(["true", "1", "yes"])


def metrics(results_csv, labels_csv, split="test"):
    gt = pd.read_csv(labels_csv)
    gt = gt[gt.split == split]
    df = gt.merge(pd.read_csv(results_csv), on="rel_path", suffixes=("_gt", ""))
    df["sensitive_gt"], df["sensitive"] = _bool(df.sensitive_gt), _bool(df.sensitive)
    acc = df.acceptable.str.split("|")
    may_delete = acc.apply(lambda a: "deletable" in a)
    pred_del = df.label == "deletable"
    llm = df.decided_by.str.startswith("llm") | (df.decided_by == "fallback")
    return {
        "files": int(len(df)),
        "sensitive_marked_deletable": int((df.sensitive_gt & pred_del).sum()),
        "false_delete_rate": float((pred_del & ~may_delete).sum() / max((~may_delete).sum(), 1)),
        "sensitive_recall": float((df.sensitive_gt & df.sensitive).sum() / max(df.sensitive_gt.sum(), 1)),
        "reclaim_recall": float(df.size_bytes[pred_del & may_delete].sum()
                                / max(df.size_bytes[may_delete].sum(), 1)),
        "review_rate": float((df.label[llm] == "review").mean()) if llm.any() else 0.0,
        "label_accuracy": float(pd.Series([lab in a for lab, a in zip(df.label, acc)]).mean()),
    }


def log_metrics(log_path, run_id: str) -> dict:
    """Schema validity, tool-call validity and latency from logs/calls.jsonl for one run."""
    try:
        df = pd.read_json(log_path, lines=True)
    except ValueError:
        return {}
    df = df[df.get("run_id") == run_id] if "run_id" in df else df.iloc[0:0]
    calls = df[df.event == "llm_call"]
    structured = calls[calls["valid"].notna()] if "valid" in calls else calls.iloc[0:0]
    first = structured[structured["retry"] == 0] if len(structured) else structured
    tools = df[(df.event == "tool_call") & (df.get("tool") == "read_snippet")] if "tool" in df else df.iloc[0:0]
    summary = df[df.event == "run_summary"]
    out = {
        "llm_calls": int(len(calls)),
        "schema_valid_first_try": float(first["valid"].astype(bool).mean()) if len(first) else None,
        "safe_failures": int((df.event == "safe_failure").sum()),
        "invariant_violations": int((df.event == "invariant_violation").sum()),
        "tool_calls": int(len(tools)),
        "tool_call_validity": float(tools["ok"].astype(bool).mean()) if len(tools) else None,
        "wall_s": float(summary["wall_s"].iloc[-1]) if len(summary) else None,
    }
    for prof, g in calls.groupby("profile"):
        out[f"mean_wall_s_{prof}"] = round(float(g["wall_s"].mean()), 3)
    return out


def passes(m: dict) -> dict:
    ops = {"==": lambda a, b: a == b, "<=": lambda a, b: a <= b, ">=": lambda a, b: a >= b}
    return {k: ops[op](m[k], v) for k, (op, v) in TARGETS.items() if m.get(k) is not None}


def cohen_kappa(a_csv, b_csv) -> float:
    """Agreement of two members' category -> label mappings (columns: category,label)."""
    a = pd.read_csv(a_csv).set_index("category")["label"]
    b = pd.read_csv(b_csv).set_index("category")["label"]
    a, b = a.align(b, join="inner")
    po = float((a == b).mean())
    pe = sum(float((a == lab).mean()) * float((b == lab).mean()) for lab in set(a) | set(b))
    return 1.0 if pe == 1 else (po - pe) / (1 - pe)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="CleanGuard evaluation metrics")
    ap.add_argument("results", nargs="?")
    ap.add_argument("labels", nargs="?")
    ap.add_argument("--split", default="test")
    ap.add_argument("--kappa", nargs=2, metavar=("A_CSV", "B_CSV"))
    a = ap.parse_args(argv)
    if a.kappa:
        print(f"Cohen's kappa: {cohen_kappa(*a.kappa):.3f}")
        return 0
    print(json.dumps(metrics(a.results, a.labels, a.split), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
