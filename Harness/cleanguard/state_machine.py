"""State machine (M9, A).

SCAN -> TRIAGE -> CLASSIFY -> PRIVACY -> VALIDATE -> REPORT -> APPROVE -> QUARANTINE -> DONE
TRIAGE -> VALIDATE when nothing is pending; REPORT -> DONE on a dry run.

FAILED covers fatal errors such as Ollama being unreachable. Nothing has moved at that
point, and the run can be resumed from its saved state (cli resume).
APPROVE and QUARANTINE are driven by the CLI approve command, not by until_pause.
"""
from __future__ import annotations

import json
import os
import time
from collections import Counter
from dataclasses import asdict
from datetime import datetime
from enum import Enum
from pathlib import Path

from harness import LLMConnectionError, ModelNotFoundError, execute, new_run_id

from . import run_context
from .agent import classify_batch
from .platform import load_profile
from .privacy import privacy_pass
from .records import Decision, FileRecord
from .report import write_report
from .rules import enforce_invariants
from .signals import signal_type
from .triage import triage
from .run_context import RunContext


class S(str, Enum):
    SCAN = "SCAN"; TRIAGE = "TRIAGE"; CLASSIFY = "CLASSIFY"; PRIVACY = "PRIVACY"  # noqa: E702
    VALIDATE = "VALIDATE"; REPORT = "REPORT"; APPROVE = "APPROVE"  # noqa: E702
    QUARANTINE = "QUARANTINE"; DONE = "DONE"; RESTORE = "RESTORE"; FAILED = "FAILED"  # noqa: E702


ALLOWED = {
    S.SCAN: {S.TRIAGE, S.FAILED},
    S.TRIAGE: {S.CLASSIFY, S.VALIDATE},
    S.CLASSIFY: {S.PRIVACY, S.VALIDATE, S.FAILED},
    S.PRIVACY: {S.VALIDATE, S.FAILED},
    S.VALIDATE: {S.REPORT},
    S.REPORT: {S.APPROVE, S.DONE},          # DONE = dry run (evaluation)
    S.APPROVE: {S.QUARANTINE, S.DONE},      # DONE = user approved nothing
    S.QUARANTINE: {S.DONE},
    S.DONE: {S.RESTORE},
    S.RESTORE: {S.DONE},
    S.FAILED: set(),
}

FATAL = (LLMConnectionError, ModelNotFoundError)


# ------------------------------------------------------------------ persistence
def run_dir(run: RunContext, cfg) -> Path:
    return Path(cfg.paths.runs_dir) / run.run_id


def _atomic_write(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def save_state(run: RunContext, cfg, files: bool = True) -> None:
    """runs/<run_id>/state.json (+ files.json). Holds names: local only, never logged."""
    d = run_dir(run, cfg)
    d.mkdir(parents=True, exist_ok=True)
    _atomic_write(d / "state.json", {
        "run_id": run.run_id, "state": run.state, "config": run.config_name,
        "os": run.profile.name, "root": run.root.as_posix(), "dry_run": run.dry_run,
        "extra_protected": list(run.extra_protected),
        "quarantine_dir": run.profile.quarantine_dir.as_posix(),
        "created_at": run.created_at, "updated_at": datetime.now().isoformat(timespec="seconds"),
        "pending_llm": run.pending_llm, "failed_from": run.failed_from, "error": run.error,
        "approved": run.approved, "batch_id": run.batch_id,
        "quarantine_batches": run.quarantine_batches, "skipped": run.skipped,
        "batch_wall": run.batch_wall,
        "decisions": {f: asdict(x) for f, x in run.decisions.items()},
    })
    if files:
        _atomic_write(d / "files.json", {f: r.to_json() for f, r in run.records.items()})


def load_run(run_id: str, cfg) -> RunContext:
    d = Path(cfg.paths.runs_dir) / run_id
    st = json.loads((d / "state.json").read_text(encoding="utf-8"))
    root = Path(st["root"])
    profile = load_profile(st["os"], extra_protected=st["extra_protected"],
                           quarantine_dir=st["quarantine_dir"])
    files_path = d / "files.json"
    files = json.loads(files_path.read_text(encoding="utf-8")) if files_path.exists() else {}
    run = RunContext(run_id=st["run_id"], root=root, profile=profile, config_name=st["config"],
                     state=st["state"], dry_run=st["dry_run"],
                     extra_protected=tuple(st["extra_protected"]),
                     records={f: FileRecord.from_json(f, root, x) for f, x in files.items()},
                     decisions={f: Decision(**x) for f, x in st["decisions"].items()},
                     pending_llm=st["pending_llm"], approved=st["approved"],
                     batch_id=st["batch_id"], failed_from=st["failed_from"], error=st["error"],
                     created_at=st["created_at"], skipped=st["skipped"],
                     batch_wall=st.get("batch_wall", {}),
                     quarantine_batches=st["quarantine_batches"])
    return run


def new_run(root: Path, cfg, profile=None, dry_run=False, extra_protected=()) -> RunContext:
    profile = profile or load_profile(extra_protected=extra_protected)
    return RunContext(run_id=new_run_id(), root=Path(root).resolve(), profile=profile,
                      config_name=cfg.name, dry_run=dry_run,
                      extra_protected=tuple(extra_protected),
                      created_at=datetime.now().isoformat(timespec="seconds"))


# ----------------------------------------------------------------------- runner
class CleanGuardRun:
    def __init__(self, h, run: RunContext, cfg, dry_run: bool | None = None):
        self.h, self.run, self.cfg = h, run, cfg
        if dry_run is not None:
            run.dry_run = dry_run
        self.t0 = time.perf_counter()
        self.handlers = {S.SCAN: self.do_scan, S.TRIAGE: self.do_triage,
                         S.CLASSIFY: self.do_classify, S.PRIVACY: self.do_privacy,
                         S.VALIDATE: self.do_validate, S.REPORT: self.do_report}
        run_context.set_active(run)

    def go(self, nxt: S):
        cur = S(self.run.state)
        if nxt not in ALLOWED[cur]:
            raise RuntimeError(f"illegal transition {cur.value} -> {nxt.value}")
        self.h.log.write("state_transition", run_id=self.run.run_id, src=cur.value, dst=nxt.value)
        self.run.state = nxt.value
        save_state(self.run, self.cfg)          # runs/<run_id>/state.json, for resume

    def fail(self, error: str):
        self.run.failed_from, self.run.error = self.run.state, error
        self.go(S.FAILED)

    def until_pause(self) -> str:
        """Run automatically until APPROVE (needs the user), DONE or FAILED."""
        while S(self.run.state) in self.handlers:
            try:
                self.go(self.handlers[S(self.run.state)]())
            except FATAL as e:
                self.fail(type(e).__name__)
            except _ScanFailed as e:
                self.fail(str(e))
        return self.run.state

    def resume(self) -> str:
        """Continue a FAILED run from the state it failed in. Nothing had moved."""
        if S(self.run.state) != S.FAILED or not self.run.failed_from:
            raise RuntimeError("only a FAILED run can be resumed")
        self.h.log.write("run_resumed", run_id=self.run.run_id, dst=self.run.failed_from)
        self.run.state, self.run.failed_from, self.run.error = self.run.failed_from, None, None
        save_state(self.run, self.cfg)
        return self.until_pause()

    # ------------------------------------------------------------------ handlers
    def _tool(self, name: str, args: dict) -> dict:
        res = execute({"function": {"name": name, "arguments": args}}, allowed={"read"},
                      logger=self.h.log, run_id=self.run.run_id)
        if not res["ok"]:
            raise _ScanFailed(res["error"])
        return res["result"]

    def do_scan(self) -> S:
        self.scan_info = self._tool("scan_folder", {"include_hidden": self.cfg.scan.include_hidden,
                                                    "max_files": self.cfg.scan.max_files})
        self._tool("find_duplicates", {})
        return S.TRIAGE

    def do_triage(self) -> S:
        counts = Counter()
        self.run.pending_llm = triage(self.run, self.cfg, counts)
        self.h.log.write("triage_summary", run_id=self.run.run_id, **counts,
                         pending=len(self.run.pending_llm))
        return S.CLASSIFY if self.run.pending_llm else S.VALIDATE

    def do_classify(self) -> S:
        todo = [f for f in self.run.pending_llm if f not in self.run.decisions]  # resume
        pending = [self.run.records[f] for f in todo]
        if not self.cfg.use_llm:                                 # C0: rules only
            for r in pending:
                self.run.decisions[r.file_id] = Decision(
                    r.file_id, "review", bool(r.signals), _stype(r), "not checked (LLM disabled)",
                    "fallback")
            return S.VALIDATE
        size = self.cfg.classify.batch_size
        for i in range(0, len(pending), size):
            for d in classify_batch(self.h, pending[i:i + size], self.cfg, self.run.run_id):
                self.run.decisions[d.file_id] = d
            save_state(self.run, self.cfg, files=False)          # resume mid-way after a crash
        return S.PRIVACY if self.cfg.privacy.enabled else S.VALIDATE

    def do_privacy(self) -> S:
        info = privacy_pass(self.h, self.run, self.cfg)
        self.h.log.write("privacy_summary", run_id=self.run.run_id, **info)
        return S.VALIDATE

    def do_validate(self) -> S:
        for f, r in self.run.records.items():                   # every file gets a decision
            if f not in self.run.decisions:
                self.run.decisions[f] = Decision(f, "review", bool(r.signals), _stype(r),
                                                 "not classified", "fallback")
        if self.cfg.domain_rules:                               # C5 measures what this prevents
            enforce_invariants(self.run, self.cfg, self.h.log)
        for d in self.run.decisions.values():
            self.h.log.write("decision", run_id=self.run.run_id, file_id=d.file_id, label=d.label,
                             sensitive=d.sensitive, sensitive_type=d.sensitive_type,
                             decided_by=d.decided_by)
        return S.REPORT

    def do_report(self) -> S:
        write_report(self.run, run_dir(self.run, self.cfg))
        labels = Counter(d.label for d in self.run.decisions.values())
        self.h.log.write("run_summary", run_id=self.run.run_id, config=self.run.config_name,
                         os=self.run.profile.name, files=len(self.run.records),
                         keep=labels["keep"], deletable=labels["deletable"],
                         review=labels["review"],
                         sensitive=sum(d.sensitive for d in self.run.decisions.values()),
                         reclaimable_bytes=reclaimable(self.run),
                         wall_s=round(time.perf_counter() - self.t0, 2))
        return S.DONE if self.run.dry_run else S.APPROVE


class _ScanFailed(RuntimeError):
    pass


def _stype(r) -> str:
    return signal_type(r.signals)


def reclaimable(run) -> int:
    return sum(run.records[f].size_bytes for f, d in run.decisions.items() if d.label == "deletable")
