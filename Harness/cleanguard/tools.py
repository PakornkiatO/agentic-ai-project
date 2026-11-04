"""CleanGuard tools (M6 / M7, B).

| Caller                 | allowed set       | tools                         |
| LLM in CLASSIFY        | {"read"}          | tool_defs(["read_snippet"])   |
| Pipeline (SCAN)        | {"read"}          | scan_folder, find_duplicates  |
| Pipeline (QUARANTINE)  | {"read", "write"} | quarantine_files (with token) |
| User via CLI restore   | {"read", "write"} | restore_batch                 |
| Anyone                 | never "dangerous" | -                             |

Errors raised here end up in the tool_call log line, so they carry no names or paths.
Never import harness.sample_tools: the registry is global.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from harness.tools import tool

from . import quarantine, scanner
from . import run_context
from .run_context import active_run
from .safety import GATE, ensure_inside
from .snippets import SUPPORTED, extract_text


class ScanArgs(BaseModel):
    include_hidden: bool = Field(default=False, description="Also list hidden files")
    max_files: int = Field(default=5000, ge=1, le=100000, description="Stop after this many files")


@tool(permission="read", timeout=600)
def scan_folder(args: ScanArgs) -> dict:
    """List the files under the active run's root folder and store their metadata."""
    run = active_run()
    try:
        records, skipped = scanner.scan(run.root, run.profile, args.include_hidden, args.max_files)
    except PermissionError as e:
        raise PermissionError(str(e)) from None          # message has no path
    except OSError as e:
        raise OSError(f"cannot scan the root folder ({type(e).__name__})") from None
    run.records = {r.file_id: r for r in records}
    run.skipped = skipped
    return {"files": len(records), "bytes": sum(r.size_bytes for r in records),
            "skipped": len(skipped), "truncated": "max_files reached" in skipped}


class DupArgs(BaseModel):
    pass


@tool(permission="read", timeout=600)
def find_duplicates(args: DupArgs) -> dict:
    """Mark exact duplicate files (same size and SHA-256) in the active run."""
    run = active_run()
    dups = scanner.mark_duplicates(list(run.records.values()))
    return {"duplicates": dups}


class SnippetArgs(BaseModel):
    file_id: str = Field(pattern=r"^f\d{4}$", description="ID of a file in the current list, e.g. f0017")
    max_chars: int = Field(default=1000, ge=100, le=1500, description="How many characters to read")


@tool(permission="read", timeout=15)
def read_snippet(args: SnippetArgs) -> dict:
    """Read the first characters of text from a .txt, .md, .csv, .pdf or .docx file in the
    current list. Use only when the file name and metadata are not enough to decide."""
    run = active_run()
    if args.file_id not in run.current_batch:
        raise LookupError(f"{args.file_id} is not in the current list")
    rec = run.records[args.file_id]
    if rec.ext not in SUPPORTED:
        raise ValueError(f"{args.file_id} is not a text, PDF or Word file")
    path = ensure_inside(run.root, rec.abs_path)
    try:
        text = extract_text(path, args.max_chars)
    except Exception as e:  # noqa: BLE001 - library errors may quote the path
        raise RuntimeError(f"could not read text from {args.file_id} ({type(e).__name__})") from None
    return {"file_id": args.file_id, "text": text, "chars": len(text)}


class QuarantineArgs(BaseModel):
    file_ids: list[str] = Field(min_length=1)
    approval_token: str


@tool(permission="write", timeout=600)
def quarantine_files(args: QuarantineArgs) -> dict:
    """Move user-approved files into the quarantine folder. Requires an approval token."""
    run = active_run()
    GATE.consume(run.run_id, args.approval_token, args.file_ids)   # raises if invalid
    return quarantine.move_batch(run, args.file_ids)


class RestoreArgs(BaseModel):
    batch_id: str = Field(pattern=r"^q-\d{8}-\d{6}-[0-9a-f]{4}$")


@tool(permission="write", timeout=600)
def restore_batch(args: RestoreArgs) -> dict:
    """Move every file of a quarantine batch back to its original location."""
    run = run_context.ACTIVE_RUN
    return quarantine.restore(args.batch_id, run.profile.quarantine_dir if run else None)


class DeleteArgs(BaseModel):
    file_ids: list[str]


@tool(permission="dangerous")
def delete_permanent(args: DeleteArgs) -> dict:
    """Permanently delete files. Disabled in CleanGuard; never in any allowed set."""
    raise NotImplementedError("permanent deletion is out of scope")
