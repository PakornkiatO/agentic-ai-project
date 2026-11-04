"""Quarantine (M13, B): move approved files, keep a manifest, restore.

CleanGuard never empties the quarantine: that would be permanent deletion.

<quarantine_dir>/<batch_id>/
    manifest.json
    files/f0017__setup_zoom_5.17.exe
"""
from __future__ import annotations

import errno
import json
import os
import secrets
import shutil
from datetime import datetime
from pathlib import Path

from .safety import ensure_inside
from .scanner import sha256_of


def new_batch_id() -> str:
    return f"q-{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _write_manifest(bdir: Path, manifest: dict) -> None:
    tmp = bdir / "manifest.json.tmp"
    tmp.write_text(json.dumps(manifest, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, bdir / "manifest.json")             # never a half-written manifest


def _move(src: Path, dst: Path) -> None:
    """Atomic rename on the same volume; verified copy + unlink across volumes."""
    try:
        os.replace(src, dst)
    except PermissionError:
        raise
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise
        shutil.copy2(src, dst)
        if sha256_of(src) != sha256_of(dst):
            dst.unlink()
            raise OSError("copy verification failed; source kept") from None
        os.unlink(src)


def _unchanged(run, rec) -> Path | None:
    """Time-of-check vs time-of-use: the file must still match its scan record."""
    try:
        if rec.abs_path.is_symlink():
            return None
        p = ensure_inside(run.root, rec.abs_path)
        st = p.stat()
    except (OSError, PermissionError):
        return None
    if st.st_size != rec.size_bytes or st.st_mtime != rec.mtime:
        return None
    return p


def move_batch(run, file_ids: list[str], qdir: Path | None = None) -> dict:
    qdir = Path(qdir or run.profile.quarantine_dir)
    batch_id = new_batch_id()
    bdir = qdir / batch_id
    (bdir / "files").mkdir(parents=True, exist_ok=False)
    manifest = {"batch_id": batch_id, "run_id": run.run_id, "created_at": _now(),
                "status": "in_progress", "entries": []}
    _write_manifest(bdir, manifest)
    moved, skipped, total = [], [], 0

    for fid in file_ids:
        rec = run.records[fid]
        src = _unchanged(run, rec)
        if src is None:
            skipped.append({"file_id": fid, "status": "changed_since_scan"})
            continue
        dst = bdir / "files" / f"{fid}__{rec.name}"
        entry = {"file_id": fid, "original_path": src.as_posix(), "quarantine_path": dst.as_posix(),
                 "size_bytes": rec.size_bytes, "sha256": None, "mtime": rec.mtime,
                 "moved_at": None, "status": "moved"}
        try:
            entry["sha256"] = rec.sha256 or sha256_of(src)
            _move(src, dst)
        except PermissionError:                 # open in another app (common on Windows)
            skipped.append({"file_id": fid, "status": "skipped_in_use"})
            continue
        except Exception as e:  # noqa: BLE001 - anything else: undo this batch
            _rollback(manifest, bdir)
            return {"batch_id": batch_id, "status": "rolled_back", "moved": [],
                    "skipped": skipped, "bytes": 0, "error": type(e).__name__}
        entry["moved_at"] = _now()
        manifest["entries"].append(entry)
        _write_manifest(bdir, manifest)          # after each file: a crash leaves it accurate
        moved.append(fid)
        total += rec.size_bytes

    manifest["status"] = "quarantined"
    _write_manifest(bdir, manifest)
    return {"batch_id": batch_id, "status": "quarantined", "moved": moved,
            "skipped": skipped, "bytes": total, "quarantine_dir": bdir.as_posix()}


def _free_name(path: Path) -> Path:
    if not path.exists():
        return path
    for i in range(1, 1000):
        tag = " (restored)" if i == 1 else f" (restored {i})"
        cand = path.with_name(f"{path.stem}{tag}{path.suffix}")
        if not cand.exists():
            return cand
    raise FileExistsError("no free name to restore to")


def _restore_entries(manifest: dict) -> list[dict]:
    out = []
    for e in manifest["entries"]:
        if e["status"] != "moved":
            continue
        src, orig = Path(e["quarantine_path"]), Path(e["original_path"])
        if not src.exists():
            e["status"] = "missing"
            out.append({"file_id": e["file_id"], "status": "missing"})
            continue
        orig.parent.mkdir(parents=True, exist_ok=True)
        dst = _free_name(orig)
        _move(src, dst)
        e["status"], e["restored_to"] = "restored", dst.as_posix()
        out.append({"file_id": e["file_id"], "status": "restored",
                    "renamed": dst != orig})
    return out


def _rollback(manifest: dict, bdir: Path) -> None:
    _restore_entries(manifest)
    manifest["status"] = "rolled_back"
    _write_manifest(bdir, manifest)


def restore(batch_id: str, qdir: Path | None = None) -> dict:
    if qdir is None:
        from .platform import load_profile
        qdir = load_profile().quarantine_dir
    bdir = Path(qdir) / batch_id
    mpath = bdir / "manifest.json"
    if not mpath.exists():
        raise LookupError(f"no quarantine batch {batch_id}")
    manifest = json.loads(mpath.read_text(encoding="utf-8"))
    results = _restore_entries(manifest)
    manifest["status"] = "restored"
    manifest["restored_at"] = _now()
    _write_manifest(bdir, manifest)
    return {"batch_id": batch_id, "run_id": manifest.get("run_id"),
            "restored": sum(r["status"] == "restored" for r in results),
            "renamed": sum(r.get("renamed", False) for r in results),
            "missing": sum(r["status"] == "missing" for r in results)}


def list_batches(qdir: Path) -> list[dict]:
    out = []
    for m in sorted(Path(qdir).glob("q-*/manifest.json")):
        try:
            d = json.loads(m.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        moved = [e for e in d["entries"] if e["status"] == "moved"]
        out.append({"batch_id": d["batch_id"], "run_id": d.get("run_id"),
                    "status": d["status"], "created_at": d.get("created_at"),
                    "files": len(moved), "bytes": sum(e["size_bytes"] for e in moved)})
    return out


def quarantine_size(qdir: Path) -> int:
    return sum(b["bytes"] for b in list_batches(qdir))
