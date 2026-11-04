"""PRIVACY pass (C).

Candidates: every file whose decision is sensitive or whose record has a signal, including
rule-decided files. Batched by 8, sent to the reasoning profile with privacy_check@v1 and
the snippets already collected (no new tool calls).

Merge rule: privacy can only raise sensitivity, never lower it. If the call fails, the
decisions stay as they are, which is already safe.
"""
from __future__ import annotations

import json

from harness import LLMError, ask_structured, build_messages

from .agent import FATAL
from .records import Decision
from .router import plan
from .rules import make_privacy_check
from .schemas import PrivacyBatch, to_meta
from .signals import signal_type

BATCH = 8


def merge_privacy(d: Decision, pv, rec, force_signals: bool = True) -> Decision:
    sig = bool(rec.signals) if force_signals else False
    sens = d.sensitive or pv.sensitive or sig
    stype = next((t for t in (d.sensitive_type, pv.sensitive_type, signal_type(rec.signals))
                  if t != "none"), "none") if sens else "none"
    label = "review" if sens and d.label == "deletable" else d.label
    return Decision(d.file_id, label, sens, stype, d.reason, d.decided_by)


def candidates(run) -> list:
    return [run.records[f] for f, d in run.decisions.items()
            if d.sensitive or run.records[f].signals]


def privacy_pass(h, run, cfg) -> dict:
    cands = candidates(run)
    p = h.prompts.get("privacy_check")
    checked = failed = 0
    for i in range(0, len(cands), BATCH):
        batch = cands[i:i + BATCH]
        metas = [to_meta(r, run.profile.name).model_dump() for r in batch]
        snip = {r.file_id: run.snippets[r.file_id] for r in batch if r.file_id in run.snippets}
        user = ("Files:\n" + json.dumps(metas, ensure_ascii=False)
                + "\nSnippets:\n" + json.dumps(snip, ensure_ascii=False))
        msgs = build_messages(p.render(), user=user)
        check = make_privacy_check(batch) if cfg.domain_rules else None
        try:
            res = ask_structured(h, msgs, PrivacyBatch, profiles=(plan(cfg).privacy,),
                                 max_retry=1, repair=True, domain_check=check,
                                 prompt_id=p.id, run_id=run.run_id)
        except FATAL:
            raise
        except LLMError:
            res = None
        if res is None or not res.ok:
            failed += len(batch)
            continue
        ids = {r.file_id for r in batch}
        for pv in res.value.verdicts:
            if pv.file_id in ids:
                run.decisions[pv.file_id] = merge_privacy(
                    run.decisions[pv.file_id], pv, run.records[pv.file_id], cfg.domain_rules)
                ids.discard(pv.file_id)
        checked += len(batch) - len(ids)
    return {"candidates": len(cands), "checked": checked, "failed": failed}
