"""Safety gate (M19, B): path sandbox and one-time approval tokens.

Tokens live only in memory, are bound to the exact file set, and are issued in exactly one
place: cli.approve, after the user confirms. A test asserts no other module calls GATE.issue.
"""
from __future__ import annotations

import secrets
import time
from pathlib import Path


def ensure_inside(root: Path, p: Path) -> Path:
    rp = Path(p).resolve(strict=True)
    if not rp.is_relative_to(Path(root).resolve()):
        raise PermissionError("path escapes the scan root")
    return rp


class SafetyGate:
    def __init__(self):
        self._tokens: dict[str, tuple[str, frozenset, float]] = {}

    def issue(self, run_id: str, file_ids: list[str], ttl_min: int) -> str:
        tok = secrets.token_urlsafe(16)
        self._tokens[tok] = (run_id, frozenset(file_ids), time.time() + ttl_min * 60)
        return tok

    def consume(self, run_id: str, token: str, file_ids: list[str]) -> None:
        entry = self._tokens.pop(token, None)            # one-time: removed on first use
        if entry is None:
            raise PermissionError("approval token is invalid or already used")
        rid, ids, expires = entry
        if rid != run_id or ids != frozenset(file_ids) or time.time() > expires:
            raise PermissionError("approval token does not match this run and file set")


GATE = SafetyGate()
