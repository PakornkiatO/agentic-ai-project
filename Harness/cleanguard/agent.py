"""CLASSIFY agent loop (M8, A).

Each batch of up to 8 pending files runs in two phases:

A  read step (agentic, optional): the model sees the batch metadata and may call
   read_snippet, a read-only tool restricted to the current batch. This is the only place
   the model chooses an action.
B  verdicts (structured): a fresh request with metadata plus snippets goes through
   ask_structured with schema VerdictBatch and the batch's domain check.

The phases are separate because tools + a format schema in one request is unreliable on
small models, and keeping them apart lets each be measured.
"""
from __future__ import annotations

import json
import time

from harness import (LLMConnectionError, LLMError, ModelNotFoundError, ask_structured,
                     build_messages, err, execute, tool_defs, tool_message)

from . import tools  # noqa: F401  (registers the CleanGuard tools)
from .records import Decision
from .router import decided_by, plan
from .rules import make_domain_check
from .run_context import active_run
from .schemas import VerdictBatch, to_meta
from .signals import find_signals, signal_type

FATAL = (LLMConnectionError, ModelNotFoundError)


def classify_messages(h, metas, snippets):
    p = h.prompts.get("classify_files")
    user = ("Files:\n" + json.dumps([m.model_dump() for m in metas], ensure_ascii=False)
            + "\nSnippets:\n" + json.dumps(snippets, ensure_ascii=False))
    return build_messages(p.render(), user=user), p.id


def gather_snippets(h, batch, cfg, run_id) -> dict[str, str]:
    p = h.prompts.get("snippet_select")
    metas = [to_meta(r, active_run().profile.name).model_dump() for r in batch]
    msgs = build_messages(p.render(max_reads=cfg.classify.max_snippets_per_batch),
                          user=json.dumps(metas, ensure_ascii=False))
    snippets = {}
    for _ in range(cfg.classify.max_tool_rounds):
        r = h.llm(plan(cfg).first, msgs, tools=tool_defs(["read_snippet"]), run_id=run_id,
                  prompt_id=p.id)
        if not r.tool_calls:
            break
        msgs.append(r.message)
        for call in r.tool_calls:
            if len(snippets) >= cfg.classify.max_snippets_per_batch:
                res = err("read budget for this batch is used up")
            else:
                res = execute(call, allowed={"read"}, logger=h.log, run_id=run_id)
                if res["ok"]:
                    snippets[res["result"]["file_id"]] = \
                        res["result"]["text"][:cfg.classify.snippet_chars]
            msgs.append(tool_message(call, res))
    return snippets


def _fallback(batch, reason="could not be classified reliably") -> list[Decision]:
    return [Decision(r.file_id, "review", bool(r.signals), signal_type(r.signals),
                     reason, "fallback") for r in batch]


def _ask(h, msgs, batch, cfg, profile, max_retry, pid, run_id):
    check = make_domain_check(batch, cfg) if cfg.domain_rules else None
    return ask_structured(h, msgs, VerdictBatch, profiles=(profile,), max_retry=max_retry,
                          repair=True, domain_check=check, prompt_id=pid, run_id=run_id)


def _to_decisions(run, batch, verdicts, used, force_signals) -> dict[str, Decision]:
    ids = {r.file_id for r in batch}
    out = {}
    for v in verdicts:
        if v.file_id not in ids or v.file_id in out:   # only possible without domain rules
            continue
        rec = run.records[v.file_id]
        sig = bool(rec.signals) if force_signals else False
        sens = v.sensitive or sig
        stype = v.sensitive_type if v.sensitive_type != "none" else signal_type(rec.signals)
        out[v.file_id] = Decision(v.file_id, v.label, sens, stype if sens else "none",
                                  v.reason, used)
    return out


def classify_batch(h, batch, cfg, run_id) -> list[Decision]:
    run = active_run()
    route = plan(cfg)
    t0 = time.perf_counter()
    run.current_batch = {r.file_id for r in batch}
    try:
        snippets = {}
        if cfg.classify.use_snippets:
            try:
                snippets = gather_snippets(h, batch, cfg, run_id)
            except FATAL:
                raise
            except LLMError:
                snippets = {}                               # the read step is optional
        for fid, text in snippets.items():                 # snippets can add privacy signals
            rec = run.records[fid]
            rec.signals = sorted(set(rec.signals) | set(find_signals(rec.name, text)))
        run.snippets.update(snippets)

        metas = [to_meta(r, run.profile.name) for r in batch]
        msgs, pid = classify_messages(h, metas, snippets)

        # Route (M11): first profile once, then escalation with the error, then safe failure
        res = _ask(h, msgs, batch, cfg, route.first, 0, pid, run_id)
        used = decided_by(route.first)
        if not res.ok:
            note = {"role": "user", "content": f"A previous answer was invalid: {res.reason}. "
                                               "Follow every rule and answer each file_id once."}
            res = _ask(h, msgs + [note], batch, cfg, route.escalation,
                       route.escalation_retries, pid, run_id)
            used = decided_by(route.escalation)
        if not res.ok:                                      # safe failure -> review
            return _fallback(batch)

        out = _to_decisions(run, batch, res.value.verdicts, used, cfg.domain_rules)

        # Escalate review labels from the first profile to the escalation profile
        unsure = [r for r in batch if r.file_id in out and out[r.file_id].label == "review"]
        if route.escalate_on_review and used == decided_by(route.first) and unsure:
            sub_snip = {f: t for f, t in snippets.items() if f in {r.file_id for r in unsure}}
            sub_msgs, _ = classify_messages(h, [to_meta(r, run.profile.name) for r in unsure],
                                            sub_snip)
            res2 = _ask(h, sub_msgs, unsure, cfg, route.escalation, 0, pid, run_id)
            if res2.ok:                                     # replace only if it validated
                out.update(_to_decisions(run, unsure, res2.value.verdicts,
                                         decided_by(route.escalation), cfg.domain_rules))

        missing = [r for r in batch if r.file_id not in out]   # only without domain rules
        return list(out.values()) + _fallback(missing, "the model gave no answer for this file")
    except FATAL:
        raise
    except LLMError:                                        # timeout, HTTP error -> review
        return _fallback(batch)
    finally:
        run.current_batch = set()
        wall = round(time.perf_counter() - t0, 3)
        for r in batch:
            run.batch_wall[r.file_id] = wall
