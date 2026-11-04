"""The active run (A).

Tools are global functions and the executor runs them in a thread pool, so contextvars do
not propagate. One module-level active run; a CleanGuard process handles one run at a time.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class RunContext:
    run_id: str
    root: Path
    profile: object
    config_name: str
    state: str = "SCAN"
    records: dict = field(default_factory=dict)        # file_id -> FileRecord
    decisions: dict = field(default_factory=dict)      # file_id -> Decision
    current_batch: set = field(default_factory=set)    # file_ids the LLM may read
    pending_llm: list = field(default_factory=list)
    approved: list = field(default_factory=list)
    batch_id: str | None = None
    # implementation additions
    dry_run: bool = False
    extra_protected: tuple = ()
    skipped: list = field(default_factory=list)        # paths the scan could not read (local only)
    snippets: dict = field(default_factory=dict)       # file_id -> text, in memory only
    batch_wall: dict = field(default_factory=dict)     # file_id -> wall seconds of its batch
    failed_from: str | None = None                     # state to resume from after FAILED
    error: str | None = None
    created_at: str = ""
    quarantine_batches: list = field(default_factory=list)


ACTIVE_RUN: RunContext | None = None


def active_run() -> RunContext:
    if ACTIVE_RUN is None:
        raise RuntimeError("no active run")
    return ACTIVE_RUN


def set_active(run: RunContext | None) -> None:
    global ACTIVE_RUN
    ACTIVE_RUN = run
