"""Platform adapter (B). OS differences live in profiles/*.yaml, not in code branches."""
from __future__ import annotations

import fnmatch
import os
import platform
import stat
from dataclasses import dataclass, replace
from pathlib import Path

import yaml

PROFILE_DIR = Path(__file__).parent / "profiles"


def current_os() -> str:
    return {"Windows": "windows", "Darwin": "macos"}.get(platform.system(), "linux")


def _expand(p: str) -> str:
    return Path(os.path.expandvars(os.path.expanduser(p))).as_posix()


@dataclass(frozen=True)
class OSProfile:
    name: str
    case_sensitive: bool
    protected: tuple[str, ...]
    sync_roots: tuple[str, ...]
    junk_names: tuple[str, ...]
    partial_download_ext: tuple[str, ...]
    quarantine_dir: Path

    def _match(self, path: Path, patterns) -> bool:
        s = path.as_posix()
        if not self.case_sensitive:
            s = s.lower()
        for pat in patterns:
            p = pat.replace("**", "*")          # fnmatch's * already crosses "/"
            if not self.case_sensitive:
                p = p.lower()
            if fnmatch.fnmatchcase(s, p):
                return True
        return False

    def is_protected(self, path: Path) -> bool:
        return self._match(path, self.protected)

    def in_sync_root(self, path: Path) -> bool:
        return self._match(path, self.sync_roots)

    def covers_folder(self, folder: Path) -> bool:
        """True if the folder itself is protected or a sync root.

        "/usr/**" does not match "/usr", so the folder is also tested with a trailing "/".
        """
        f = Path(folder)
        slash = Path(f.as_posix().rstrip("/") + "/x")   # any child of the folder
        return any(m(p) for m in (self.is_protected, self.in_sync_root) for p in (f, slash))

    def is_junk_name(self, name: str) -> bool:
        n = name if self.case_sensitive else name.lower()
        return any(fnmatch.fnmatchcase(n, p if self.case_sensitive else p.lower())
                   for p in self.junk_names)

    def with_extra_protected(self, patterns) -> "OSProfile":
        """Used by the eval testbed to mark a folder such as app_config/ as protected."""
        return replace(self, protected=self.protected + tuple(_expand(p) for p in patterns))


def load_profile(name: str | None = None, extra_protected=(),
                 quarantine_dir: str | Path | None = None) -> OSProfile:
    name = name or current_os()
    d = yaml.safe_load((PROFILE_DIR / f"{name}.yaml").read_text(encoding="utf-8"))
    prof = OSProfile(
        name=d["name"], case_sensitive=d["case_sensitive"],
        protected=tuple(_expand(p) for p in d["protected"]),
        sync_roots=tuple(_expand(p) for p in d["sync_roots"]),
        junk_names=tuple(d["junk_names"]),
        partial_download_ext=tuple(e.lower() for e in d["partial_download_ext"]),
        quarantine_dir=Path(_expand(str(quarantine_dir or d["quarantine_dir"]))),
    )
    return prof.with_extra_protected(extra_protected) if extra_protected else prof


def is_hidden(entry: os.DirEntry) -> bool:
    if entry.name.startswith("."):
        return True
    st = entry.stat(follow_symlinks=False)
    if hasattr(st, "st_file_attributes"):                       # Windows
        return bool(st.st_file_attributes & stat.FILE_ATTRIBUTE_HIDDEN)
    if hasattr(st, "st_flags") and hasattr(stat, "UF_HIDDEN"):  # macOS
        return bool(st.st_flags & stat.UF_HIDDEN)
    return False


def is_link_or_junction(entry: os.DirEntry) -> bool:
    if entry.is_symlink():
        return True
    st = entry.stat(follow_symlinks=False)
    attrs = getattr(st, "st_file_attributes", 0)                 # Windows reparse points
    return bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
