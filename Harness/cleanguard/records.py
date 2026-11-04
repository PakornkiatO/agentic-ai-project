"""Internal records (M4, C). Never sent to the LLM: they hold absolute paths."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal, Optional

DecidedBy = Literal["rule", "llm_fast", "llm_reasoning", "fallback", "invariant"]


@dataclass
class FileRecord:
    file_id: str                 # "f0001" ... stable within a run
    abs_path: Path
    rel_path: str                # POSIX, relative to the scan root
    name: str
    ext: str                     # lower case, with dot
    size_bytes: int
    mtime: float
    modified_days: int
    protected: bool = False
    sha256: Optional[str] = None
    dup_of: Optional[str] = None # file_id of the canonical copy
    signals: list[str] = field(default_factory=list)   # e.g. ["thai_id", "kw:financial"]

    def to_json(self) -> dict:
        d = asdict(self)
        for k in ("file_id", "abs_path", "name", "ext"):   # derivable from root + rel_path
            d.pop(k)
        return d

    @classmethod
    def from_json(cls, file_id: str, root: Path, d: dict) -> "FileRecord":
        p = root / d["rel_path"]
        return cls(file_id=file_id, abs_path=p, name=p.name, ext=p.suffix.lower(), **d)


@dataclass
class Decision:
    file_id: str
    label: Literal["keep", "deletable", "review"]
    sensitive: bool
    sensitive_type: str
    reason: str
    decided_by: DecidedBy
