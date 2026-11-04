"""Domain rules (M5, C): per-batch checks for ask_structured, and the final invariant pass.

Error messages go into the repair prompt, so they say what to change. They hold file_ids
only, because the harness also logs them.
"""
from __future__ import annotations

from harness.validation import DomainError


def _same_ids(b, recs) -> None:
    got = [v.file_id for v in b.verdicts]
    if sorted(got) != sorted(recs):
        raise DomainError(f"answer each file_id exactly once; expected {sorted(recs)}, got {got}")


def _type_matches(v) -> None:
    if (v.sensitive_type != "none") != v.sensitive:
        raise DomainError(f"{v.file_id}: sensitive_type must be none exactly when sensitive is false")


def make_domain_check(batch, cfg):
    recs = {r.file_id: r for r in batch}
    min_age = cfg.scan.min_age_days_for_delete

    def check(b) -> None:
        _same_ids(b, recs)
        for v in b.verdicts:
            r = recs[v.file_id]
            if v.sensitive and v.label == "deletable":
                raise DomainError(f"{v.file_id} is sensitive, so its label must be keep or review")
            _type_matches(v)
            if v.label == "deletable" and r.modified_days < min_age:
                raise DomainError(f"{v.file_id} was modified {r.modified_days} days ago; use keep or review")
            if r.protected and v.label != "keep":
                raise DomainError(f"{v.file_id} is in a protected folder; label must be keep")
    return check


def make_privacy_check(batch):
    recs = {r.file_id: r for r in batch}

    def check(b) -> None:
        _same_ids(b, recs)
        for v in b.verdicts:
            _type_matches(v)
    return check


def enforce_invariants(run, cfg, log) -> int:
    """The VALIDATE state: a whole-run pass over all decisions. It should never fire;
    each time it does, it logs invariant_violation, which means there is a bug to fix."""
    fixed = 0
    for fid, d in run.decisions.items():
        r = run.records[fid]
        bad = None
        if d.label == "deletable" and (d.sensitive or r.signals):
            bad = "sensitive_deletable"
        elif d.label == "deletable" and r.modified_days < cfg.scan.min_age_days_for_delete:
            bad = "recent_deletable"
        elif r.protected and d.label != "keep":
            bad = "protected_not_keep"
        if bad:
            d.label, d.decided_by = ("keep" if r.protected else "review"), "invariant"
            log.write("invariant_violation", run_id=run.run_id, file_id=fid, rule=bad)
            fixed += 1
    return fixed
