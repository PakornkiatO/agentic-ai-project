"""Scanner (B): walk + metadata + hashing. Never follows links, never uses access time."""
from __future__ import annotations

import hashlib
import os
import time
from collections import defaultdict
from pathlib import Path

from .platform import is_hidden, is_link_or_junction
from .records import FileRecord


def scan(root: Path, profile, include_hidden: bool, max_files: int) -> tuple[list[FileRecord], list[str]]:
    root = Path(root).resolve(strict=True)
    if profile.covers_folder(root):
        raise PermissionError("Refusing to scan a protected or cloud-sync folder")
    records, skipped, now = [], [], time.time()
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            entries = sorted(os.scandir(d), key=lambda e: e.name)   # stable file_ids
        except PermissionError:
            skipped.append(f"{d}: permission denied")
            continue
        for e in entries:
            try:
                if is_link_or_junction(e):
                    continue                          # never follow links out of root
                if not include_hidden and is_hidden(e):
                    continue
                p = Path(e.path)
                if profile.in_sync_root(p):
                    continue
                if e.is_dir(follow_symlinks=False):
                    if not profile.is_protected(p):
                        stack.append(p)
                    continue
                st = e.stat(follow_symlinks=False)
                records.append(FileRecord(
                    file_id=f"f{len(records) + 1:04d}", abs_path=p,
                    rel_path=p.relative_to(root).as_posix(), name=e.name,
                    ext=p.suffix.lower(), size_bytes=st.st_size, mtime=st.st_mtime,
                    modified_days=int((now - st.st_mtime) // 86400),
                    protected=profile.is_protected(p)))
                if len(records) >= max_files:
                    return records, skipped + ["max_files reached"]
            except OSError as ex:
                skipped.append(f"{e.path}: {type(ex).__name__}")
    return records, skipped


def sha256_of(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while b := f.read(chunk):
            h.update(b)
    return h.hexdigest()


def mark_duplicates(records: list[FileRecord]) -> int:
    """Hash only files that share a size with another file. Returns duplicates found."""
    by_size = defaultdict(list)
    for r in records:
        if r.size_bytes > 0 and not r.protected:
            by_size[r.size_bytes].append(r)
    dups = 0
    for group in (g for g in by_size.values() if len(g) > 1):
        by_hash = defaultdict(list)
        for r in group:
            try:
                r.sha256 = sha256_of(r.abs_path)
                by_hash[r.sha256].append(r)
            except OSError:
                pass
        for same in (g for g in by_hash.values() if len(g) > 1):
            canonical = min(same, key=lambda r: (r.mtime, len(r.rel_path)))
            for r in same:
                if r is not canonical:
                    r.dup_of = canonical.file_id
                    dups += 1
    return dups
