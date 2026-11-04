"""Triage rules (M10, A). Decide as many files as possible without the LLM.

| # | Rule                                         | Result                    |
| 1 | path matches a protected pattern             | keep                      |
| 2 | modified_days < min_age_days_for_delete      | keep (recent)             |
| 3 | name has a privacy signal                    | -> LLM, then PRIVACY      |
| 4 | name matches junk_names                      | deletable                 |
| 5 | partial download older than 7 days           | deletable                 |
| 6 | exact duplicate (dup_of set)                 | deletable                 |
| 7 | everything else                              | -> LLM                    |

Rule 3 sits before junk and duplicate rules on purpose: a duplicate of a passport scan is
judged by the model and the privacy pass, not silently marked deletable.
"""
from __future__ import annotations

from collections import Counter

from .records import Decision
from .signals import find_signals, signal_type


def triage(run, cfg, counts: Counter | None = None) -> list[str]:
    """Fill run.decisions for rule-decided files; return file_ids that need the LLM."""
    counts = counts if counts is not None else Counter()
    pending = []
    for r in run.records.values():
        r.signals = find_signals(r.name)
        sens, stype = bool(r.signals), signal_type(r.signals)

        def decide(rule, label, reason):
            counts[rule] += 1
            run.decisions[r.file_id] = Decision(r.file_id, label, sens, stype, reason, "rule")

        if r.protected:
            decide("protected", "keep", "inside a protected folder")
        elif r.modified_days < cfg.scan.min_age_days_for_delete:
            decide("recent", "keep", f"modified {r.modified_days} days ago")
        elif r.signals:
            counts["privacy_signal"] += 1
            pending.append(r.file_id)
        elif run.profile.is_junk_name(r.name):
            decide("junk", "deletable", "system junk file")
        elif r.ext in run.profile.partial_download_ext and \
                r.modified_days >= cfg.scan.partial_download_min_age_days:
            decide("partial_download", "deletable", "unfinished download")
        elif r.dup_of:
            decide("duplicate", "deletable", f"exact copy of {r.dup_of}")
        else:
            counts["to_llm"] += 1
            pending.append(r.file_id)
    return pending
