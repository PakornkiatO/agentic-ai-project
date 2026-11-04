# CleanGuard — Implementation Details

A local, cross-platform storage-cleaning agent built on the Session 2 harness (`Harness.zip`).
It **marks** files as `keep`, `deletable` or `review`, protects private documents, and only moves
files the user approves into a restorable quarantine. Nothing is ever deleted permanently, and
nothing leaves the machine.

This document is the build guide behind the design pitch. Each section names its harness module
(M1–M20) and its owner role from the pitch: **A** orchestration, **B** tools and safety,
**C** model I/O, **D** evaluation and UI.

---

## 1. Ground rules

These are the invariants the whole design protects. Every code review checks them.

1. **The LLM labels; it never acts.** No LLM output can cause a file to move. Only the pipeline
   calls write tools, and only with a one-time approval token created by the user.
2. **Every failure degrades to `review`.** Schema failure, rule violation, tool error, timeout:
   the affected files get `label="review"`, never `deletable`.
3. **Sensitive is never deletable.** Enforced in the prompt, in M5 domain rules, and again in a
   final invariant pass.
4. **No network.** Only `http://localhost:11434` (Ollama). No telemetry, no cloud APIs.
5. **Logs hold metadata, never content or names.** Logs refer to files by `file_id` only.
6. **Stay inside the chosen folder.** Never follow symlinks or junctions; resolve and check every
   path before any read or move.
7. **Same code on Windows, macOS and Linux.** Differences live in profile YAML files, not in
   `if os == ...` branches scattered through the code.

---

## 2. Repository layout

The Session 2 `harness/` package is reused **unchanged**. CleanGuard is a new package beside it,
so `Harness()` still finds `config.yaml` and `prompts/` at the repo root.

```
Harness/
├── harness/                    # Session 2 core, unchanged: M1–M7, M14
├── cleanguard/
│   ├── __main__.py             # python -m cleanguard ...           (M18, D)
│   ├── cli.py                  # scan / approve / restore / runs    (M18, D)
│   ├── settings.py             # loads cleanguard.yaml              (C)
│   ├── platform.py             # platform adapter + profile loader  (B)
│   ├── profiles/
│   │   ├── windows.yaml
│   │   ├── macos.yaml
│   │   └── linux.yaml
│   ├── records.py              # FileRecord (internal, never sent to the LLM)
│   ├── schemas.py              # FileMeta, FileVerdict, VerdictBatch, PrivacyBatch (M4, C)
│   ├── scanner.py              # walk + metadata + hashing          (B)
│   ├── signals.py              # deterministic privacy signals      (C)
│   ├── snippets.py             # text extraction txt/pdf/docx       (B)
│   ├── tools.py                # @tool registrations                (M6, B)
│   ├── triage.py               # rule engine                        (M10, A)
│   ├── rules.py                # domain checks + invariants         (M5, C)
│   ├── agent.py                # CLASSIFY loop                      (M8, A)
│   ├── router.py               # profile selection / escalation     (M11, A)
│   ├── privacy.py              # PRIVACY pass                       (C)
│   ├── run_context.py          # RunContext + ACTIVE_RUN            (A)
│   ├── state_machine.py        # M9                                 (A)
│   ├── safety.py               # path sandbox + approval tokens     (M19, B)
│   ├── quarantine.py           # move, manifest, restore            (M13, B)
│   └── report.py               # console + HTML report              (M18, D)
├── prompts/
│   ├── classify_files@v1.txt
│   ├── snippet_select@v1.txt
│   └── privacy_check@v1.txt
├── cleanguard.yaml
├── eval/                                                             (M20, D)
│   ├── categories.yaml
│   ├── make_testbed.py
│   ├── configs.yaml
│   ├── run_eval.py
│   └── metrics.py
├── tests/
│   ├── test_offline.py         # Session 2 tests, unchanged
│   └── test_cleanguard_*.py
└── runs/                       # per-run state, reports, manifests (git-ignored)
```

Do **not** import `harness.sample_tools` anywhere in CleanGuard. The tool registry (`TOOLS`) is
global, and importing the sample module would register `calculator`, `lookup_customer`, etc.
Always pass explicit names to `tool_defs([...])`.

---

## 3. Setup on each OS

Requires Python 3.10+ and Ollama. The `.venv` inside `Harness.zip` contains Windows binaries;
macOS and Linux users must delete it and create their own.

```bash
# macOS / Linux
rm -rf .venv
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

```powershell
# Windows
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Then, on every OS:

```bash
ollama pull qwen3:4b-q4_K_M
ollama pull qwen3:8b-q4_K_M
python scripts/check_setup.py
python -m unittest discover -s tests
```

**Add to `requirements.txt`:**

```
pypdf>=4.0          # PDF text for read_snippet
python-docx>=1.1    # DOCX text for read_snippet
fpdf2>=2.7          # eval only: generates synthetic PDFs for the testbed
```

**macOS note:** the first scan of `~/Downloads` or `~/Documents` triggers a macOS privacy prompt
(Files and Folders) for the terminal app. Grant it once, or scanning returns permission errors.

---

## 4. Configuration

### 4.1 `config.yaml` (harness, M2)

Two changes only. Batches of 8 verdicts need more output tokens than Session 2 tickets
(about 8 × 45 = 360 tokens, plus headroom). `ask_structured` does not pass per-call overrides,
so the change goes in the profile.

```yaml
profiles:
  fast:
    model: qwen3:4b-q4_K_M
    temperature: 0
    num_ctx: 8192
    num_predict: 1024      # was 512
    keep_alive: 10m
    think: false
  reasoning:
    model: qwen3:8b-q4_K_M
    temperature: 0
    num_ctx: 16384
    num_predict: 1024
    keep_alive: 10m
    think: false
  # fallback (gemma3:12b) is kept in the file but never used by CleanGuard
```

### 4.2 `cleanguard.yaml` (CleanGuard)

```yaml
scan:
  max_files: 5000
  include_hidden: false
  min_age_days_for_delete: 30      # files newer than this are never deletable
  partial_download_min_age_days: 7

classify:
  batch_size: 8
  use_snippets: true
  max_snippets_per_batch: 3
  snippet_chars: 1000
  max_tool_rounds: 1

privacy:
  enabled: true

routing:
  first_profile: fast
  escalation_profile: reasoning
  escalate_on_review: true

approval:
  token_ttl_minutes: 30

domain_rules: true                # eval config C5 sets this to false

paths:
  runs_dir: runs
```

`settings.py` loads it into a frozen dataclass. The eval runner overrides fields per
configuration (section 19.2).

---

## 5. Platform adapter (B)

### 5.1 Profile files

One YAML per OS. Patterns use `*` and `**` against **POSIX-style absolute paths** after
`expanduser` + `expandvars`. Matching is case-insensitive when `case_sensitive: false`.

`cleanguard/profiles/windows.yaml`

```yaml
name: windows
case_sensitive: false
protected:
  - "C:/Windows/**"
  - "C:/Program Files/**"
  - "C:/Program Files (x86)/**"
  - "C:/ProgramData/**"
  - "%APPDATA%/**"
  - "%LOCALAPPDATA%/**"
  - "**/.git/**"
sync_roots:
  - "%USERPROFILE%/OneDrive*/**"
junk_names:
  - "Thumbs.db"
  - "~$*"                 # Office lock files (age rule applies)
partial_download_ext: [".crdownload", ".part", ".tmp", ".partial"]
quarantine_dir: "%LOCALAPPDATA%/CleanGuard/quarantine"
```

`cleanguard/profiles/macos.yaml`

```yaml
name: macos
case_sensitive: false
protected:
  - "/System/**"
  - "/Applications/**"
  - "/Library/**"
  - "~/Library/**"
  - "**/.git/**"
  - "**/*.app/**"         # app bundles are folders; never look inside
sync_roots:
  - "~/Library/Mobile Documents/**"      # iCloud Drive
  - "~/Library/CloudStorage/**"
junk_names:
  - ".DS_Store"
  - "._*"                 # AppleDouble files on non-HFS volumes
partial_download_ext: [".crdownload", ".part", ".download"]
quarantine_dir: "~/Library/Application Support/CleanGuard/quarantine"
```

`cleanguard/profiles/linux.yaml`

```yaml
name: linux
case_sensitive: true
protected:
  - "/usr/**"
  - "/etc/**"
  - "/bin/**"
  - "/var/**"
  - "~/.config/**"
  - "~/.local/**"
  - "~/.ssh/**"
  - "~/.gnupg/**"
  - "**/.git/**"
sync_roots:
  - "~/Dropbox/**"
  - "~/Nextcloud/**"
junk_names:
  - "*~"
  - ".~lock.*#"           # LibreOffice lock files (age rule applies)
partial_download_ext: [".crdownload", ".part"]
quarantine_dir: "~/.local/share/CleanGuard/quarantine"
```

Note that `quarantine_dir` on macOS and Linux lies inside a protected path. That is intended:
the scanner can never pick up quarantined files, and the quarantine module writes there through
its own code path, not through the scanner.

### 5.2 `platform.py`

```python
import fnmatch, os, platform, stat
from dataclasses import dataclass
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

    def is_junk_name(self, name: str) -> bool:
        n = name if self.case_sensitive else name.lower()
        return any(fnmatch.fnmatchcase(n, p if self.case_sensitive else p.lower())
                   for p in self.junk_names)

def load_profile(name: str | None = None) -> OSProfile:
    name = name or current_os()
    d = yaml.safe_load((PROFILE_DIR / f"{name}.yaml").read_text(encoding="utf-8"))
    return OSProfile(
        name=d["name"], case_sensitive=d["case_sensitive"],
        protected=tuple(_expand(p) for p in d["protected"]),
        sync_roots=tuple(_expand(p) for p in d["sync_roots"]),
        junk_names=tuple(d["junk_names"]),
        partial_download_ext=tuple(e.lower() for e in d["partial_download_ext"]),
        quarantine_dir=Path(_expand(d["quarantine_dir"])),
    )

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
```

`**` is collapsed to `*` because `fnmatch`'s `*` already matches `/`. Python 3.13's
`PurePath.full_match` would be stricter but is not available on 3.10–3.12.

---

## 6. Data model (M4, C)

### 6.1 Internal record — never sent to the LLM

```python
# cleanguard/records.py
from dataclasses import dataclass, field
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
    signals: list[str] = field(default_factory=list)   # e.g. ["thai_id", "kw:payslip"]

@dataclass
class Decision:
    file_id: str
    label: Literal["keep", "deletable", "review"]
    sensitive: bool
    sensitive_type: str
    reason: str
    decided_by: DecidedBy
```

### 6.2 Schemas sent to and returned by the model

```python
# cleanguard/schemas.py
from typing import Literal, Optional
from pydantic import BaseModel, ConfigDict, Field

Label = Literal["keep", "deletable", "review"]
SensitiveType = Literal["none", "id_doc", "financial", "medical", "credentials", "personal"]

class FileMeta(BaseModel):
    """What the LLM sees about one file. No absolute paths, no user names."""
    file_id: str
    name: str
    ext: str
    size_mb: float
    modified_days: int
    folder: str                  # relative folder, e.g. "Downloads/course/"
    platform: Literal["windows", "macos", "linux"]
    dup_of: Optional[str] = None

class FileVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file_id: str = Field(pattern=r"^f\d{4}$")
    label: Label
    sensitive: bool
    sensitive_type: SensitiveType
    reason: str = Field(min_length=5, max_length=120)

class VerdictBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verdicts: list[FileVerdict] = Field(min_length=1, max_length=8)

class PrivacyVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file_id: str = Field(pattern=r"^f\d{4}$")
    sensitive: bool
    sensitive_type: SensitiveType
    reason: str = Field(min_length=5, max_length=120)

class PrivacyBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verdicts: list[PrivacyVerdict] = Field(min_length=1, max_length=8)

def to_meta(r, platform_name: str) -> FileMeta:
    folder = r.rel_path.rsplit("/", 1)[0] + "/" if "/" in r.rel_path else "./"
    return FileMeta(file_id=r.file_id, name=r.name, ext=r.ext,
                    size_mb=round(r.size_bytes / 1e6, 1), modified_days=r.modified_days,
                    folder=folder, platform=platform_name, dup_of=r.dup_of)
```

**Day-1 check (C):** `VerdictBatch.model_json_schema()` contains `$defs` and `$ref`. Confirm
that Ollama's `format` field honours them with one probe call. If it does not, inline the
references; `ask_structured` calls `model_cls.model_json_schema()`, so overriding that
classmethod is enough:

```python
def inline_refs(schema: dict) -> dict:
    defs = schema.pop("$defs", {})
    def walk(node):
        if isinstance(node, dict):
            if "$ref" in node:
                return walk(dict(defs[node["$ref"].split("/")[-1]]))
            return {k: walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(x) for x in node]
        return node
    return walk(schema)

class VerdictBatch(BaseModel):
    ...
    @classmethod
    def model_json_schema(cls, *args, **kwargs):
        return inline_refs(super().model_json_schema(*args, **kwargs))
```

---

## 7. Scanner (B)

```python
# cleanguard/scanner.py
import hashlib, os, time
from collections import defaultdict
from pathlib import Path
from .platform import is_hidden, is_link_or_junction
from .records import FileRecord

def scan(root: Path, profile, include_hidden: bool, max_files: int) -> tuple[list[FileRecord], list[str]]:
    root = root.resolve(strict=True)
    if profile.is_protected(root) or profile.in_sync_root(root):
        raise PermissionError("Refusing to scan a protected or cloud-sync folder")
    records, skipped, now = [], [], time.time()
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            entries = list(os.scandir(d))
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
        if r.size_bytes > 0:
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
```

Do not use last-access time anywhere. It is unreliable on all three OSes: NTFS often disables
updates, Linux mounts use `relatime`, and opening a file to hash it may itself update it.

---

## 8. Privacy signals (C)

Deterministic checks that run on file names and, when available, snippets. A signal hit forces
`sensitive=True` regardless of what the model says, and routes the file to the PRIVACY pass.

```python
# cleanguard/signals.py
import re

KEYWORDS = {
    "id_doc": ["passport", "id card", "idcard", "national id", "บัตรประชาชน", "driver license"],
    "financial": ["statement", "payslip", "pay slip", "salary", "bank", "tax", "invoice",
                  "เงินเดือน", "สลิป", "ภาษี"],
    "medical": ["medical", "prescription", "lab result", "hospital", "ใบรับรองแพทย์"],
    "credentials": ["password", "passwd", "secret", "recovery codes", "private key", ".pem", "id_rsa"],
}

THAI_ID = re.compile(r"(?<!\d)(\d[\s-]?\d{4}[\s-]?\d{5}[\s-]?\d{2}[\s-]?\d)(?!\d)")
CARD = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")

def thai_id_valid(s: str) -> bool:
    d = [int(c) for c in re.sub(r"\D", "", s)]
    if len(d) != 13:
        return False
    total = sum(d[i] * (13 - i) for i in range(12))
    return (11 - total % 11) % 10 == d[12]

def luhn_valid(s: str) -> bool:
    d = [int(c) for c in re.sub(r"\D", "", s)][::-1]
    if not 13 <= len(d) <= 19:
        return False
    total = sum(x if i % 2 == 0 else (x * 2 - 9 if x * 2 > 9 else x * 2) for i, x in enumerate(d))
    return total % 10 == 0

def find_signals(name: str, text: str = "") -> list[str]:
    hay = f"{name}\n{text}".lower()
    hits = [f"kw:{t}" for t, words in KEYWORDS.items() if any(w in hay for w in words)]
    if any(thai_id_valid(m) for m in THAI_ID.findall(text)):
        hits.append("thai_id")
    if any(luhn_valid(m) for m in CARD.findall(text)):
        hits.append("card_number")
    return hits

def signal_type(signals: list[str]) -> str:
    for s in signals:
        if s == "thai_id":
            return "id_doc"
        if s == "card_number":
            return "financial"
        if s.startswith("kw:"):
            return s[3:]
    return "none"
```

Keyword hits are deliberately broad ("invoice", "bank"). False positives only cost a `review`
label; false negatives can cost a private document.

---

## 9. Tools (M6 / M7, B)

### 9.1 Run context

Tools are global functions registered at import, and the executor runs them in a thread pool
(`run_with_timeout`), so `contextvars` do not propagate. Use one module-level active run; a
CleanGuard process handles one run at a time.

```python
# cleanguard/run_context.py
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

ACTIVE_RUN: RunContext | None = None

def active_run() -> RunContext:
    if ACTIVE_RUN is None:
        raise RuntimeError("no active run")
    return ACTIVE_RUN
```

### 9.2 Registrations

```python
# cleanguard/tools.py
from pydantic import BaseModel, Field
from harness.tools import tool
from .run_context import active_run
from .safety import ensure_inside, GATE
from .snippets import extract_text
from . import quarantine

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
    path = ensure_inside(run.root, rec.abs_path)
    text = extract_text(path, args.max_chars)
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
    return quarantine.restore(args.batch_id)

class DeleteArgs(BaseModel):
    file_ids: list[str]

@tool(permission="dangerous")
def delete_permanent(args: DeleteArgs) -> dict:
    """Permanently delete files. Disabled in CleanGuard; never in any allowed set."""
    raise NotImplementedError("permanent deletion is out of scope")
```

`scan_folder` and `find_duplicates` from the pitch are thin wrappers over `scanner.scan` and
`scanner.mark_duplicates`, registered with `permission="read"`. They return summaries
(counts, skipped paths), not the full record list, and store the records in `ACTIVE_RUN`.

### 9.3 Who may call what

| Caller | `allowed` set passed to `execute()` | Tools offered |
|---|---|---|
| LLM in CLASSIFY | `{"read"}` | `tool_defs(["read_snippet"])` only |
| Pipeline (SCAN) | `{"read"}` | called directly via `execute()` |
| Pipeline (QUARANTINE) | `{"read", "write"}` | `quarantine_files` with a token |
| User via CLI `restore` | `{"read", "write"}` | `restore_batch` |
| Anyone | never includes `"dangerous"` | — |

The pipeline goes through `execute()` too, so every call gets argument validation,
permission checks, a timeout and a `tool_call` log line.

### 9.4 Snippet extraction

```python
# cleanguard/snippets.py
from pathlib import Path

TEXT_EXT = {".txt", ".md", ".csv", ".log", ".json"}

def extract_text(path: Path, max_chars: int) -> str:
    ext = path.suffix.lower()
    if ext in TEXT_EXT:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(max_chars)
    if ext == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(str(path))
        out = ""
        for page in reader.pages[:2]:
            out += (page.extract_text() or "") + "\n"
            if len(out) >= max_chars:
                break
        return out[:max_chars] or "[no extractable text: scanned PDF or image]"
    if ext == ".docx":
        import docx
        out = "\n".join(p.text for p in docx.Document(str(path)).paragraphs)
        return out[:max_chars]
    raise ValueError(f"unsupported file type {ext}")
```

An empty PDF result usually means a scanned document. That case is out of scope, so the model
sees the placeholder text and should answer `review`.

---

## 10. Triage rules (M10, A)

Triage decides as many files as possible **without** the LLM. Rules run in order; the first
match wins. Name-based privacy signals are computed for every file first, so rule-decided files
still carry a correct `sensitive` flag.

| # | Rule | Result |
|---|---|---|
| 1 | Path matches a protected pattern | `keep` |
| 2 | `modified_days < min_age_days_for_delete` (30) | `keep` (recent) |
| 3 | Name has a privacy signal | → LLM, then PRIVACY |
| 4 | Name matches `junk_names` | `deletable` |
| 5 | Extension in `partial_download_ext` and older than 7 days | `deletable` |
| 6 | Exact duplicate (`dup_of` set) | `deletable` ("exact copy of f0012") |
| 7 | Everything else | → LLM |

Rule 3 sits before the junk and duplicate rules on purpose: a duplicate of a passport scan
should be judged by the model and the privacy pass, not silently marked deletable.

```python
# cleanguard/triage.py
from .records import Decision
from .signals import find_signals, signal_type

def triage(run, cfg) -> list[str]:
    """Fill run.decisions for rule-decided files; return file_ids that need the LLM."""
    pending = []
    for r in run.records.values():
        r.signals = find_signals(r.name)
        sens, stype = bool(r.signals), signal_type(r.signals)

        def decide(label, reason):
            run.decisions[r.file_id] = Decision(r.file_id, label, sens, stype, reason, "rule")

        if r.protected:
            decide("keep", "inside a protected folder")
        elif r.modified_days < cfg.scan.min_age_days_for_delete:
            decide("keep", f"modified {r.modified_days} days ago")
        elif r.signals:
            pending.append(r.file_id)
        elif run.profile.is_junk_name(r.name):
            decide("deletable", "system junk file")
        elif r.ext in run.profile.partial_download_ext and \
                r.modified_days >= cfg.scan.partial_download_min_age_days:
            decide("deletable", "unfinished download")
        elif r.dup_of:
            decide("deletable", f"exact copy of {r.dup_of}")
        else:
            pending.append(r.file_id)
    return pending
```

Log one `triage_summary` event with counts per rule (no names).

---

## 11. Prompts (M3, C)

Plain text, versioned as `name@vN.txt`. Avoid the `$` character: `PromptManager.render()` uses
`string.Template`, and a stray `$` raises an error.

### 11.1 `prompts/classify_files@v1.txt`

```
You help a person free disk space safely. You only label files. You never delete anything.

For every file in the list, return one verdict:
- label "deletable": no longer useful or easy to get again. Examples: old installers
  (.exe .msi .dmg .pkg .deb), archives that were already extracted, duplicate downloads,
  drafts when a final version exists in the same folder, course or product files that can be
  downloaded again.
- label "keep": the person probably still needs it. Examples: personal documents, certificates,
  CVs, contracts, photos of people, the only copy of a project.
- label "review": you are not sure. Prefer review over guessing.
- sensitive: true if the file probably holds personal or financial data: ID cards or passports,
  bank statements, payslips, tax forms, medical documents, passwords or keys, private photos.
  A sensitive file is never "deletable".
- sensitive_type: one of none, id_doc, financial, medical, credentials, personal.
  Use none exactly when sensitive is false.
- reason: one short sentence that a non-expert understands.

Use only the metadata and snippets given. Answer every file_id exactly once.

Example
Files: [{"file_id": "f0003", "name": "setup_zoom_5.17.exe", "ext": ".exe", "size_mb": 92.4,
"modified_days": 410, "folder": "Downloads/", "platform": "windows", "dup_of": null}]
JSON: {"verdicts": [{"file_id": "f0003", "label": "deletable", "sensitive": false,
"sensitive_type": "none", "reason": "Old installer; the app can be downloaded again."}]}

Return JSON only, matching the schema.
```

### 11.2 `prompts/snippet_select@v1.txt`

```
You are preparing to label files for a disk cleanup. You can read the start of a text, PDF or
Word file with the read_snippet tool.

Call read_snippet only for files where the name and metadata are not enough to decide, for
example "scan_001.pdf" or "document (3).docx". Do not read files whose name already says
what they are. Read at most $max_reads files.

If no file needs reading, reply with the single word DONE.
```

### 11.3 `prompts/privacy_check@v1.txt`

```
You check files for private data before a disk cleanup. You do not decide what to delete.

For every file, decide if it probably contains personal or financial data about a real person:
ID cards or passports, bank statements, payslips, tax forms, medical documents, passwords,
recovery codes or private keys, or private photos and letters.

Lecture slides, product manuals, public reports and software are not sensitive, even when
they mention words like "bank" or "medical" as a topic.

Return sensitive, sensitive_type (none, id_doc, financial, medical, credentials, personal)
and a short reason for every file_id, exactly once. Return JSON only.
```

### 11.4 Message assembly

```python
import json
from harness import build_messages

def classify_messages(h, metas, snippets):
    p = h.prompts.get("classify_files")
    user = ("Files:\n" + json.dumps([m.model_dump() for m in metas], ensure_ascii=False)
            + "\nSnippets:\n" + json.dumps(snippets, ensure_ascii=False))
    return build_messages(p.render(), user=user), p.id
```

The system prompt comes first and never changes, so Ollama can reuse its prefix across batches.

---

## 12. CLASSIFY agent loop and router (M8 / M11, A)

Each batch of up to 8 pending files runs in two phases.

**Phase A: read step (agentic, optional).** The model sees the batch metadata and may call
`read_snippet` up to `max_snippets_per_batch` times in one round. This is the only place the
model chooses an action, and the only action available is a read-only tool restricted to the
current batch.

**Phase B: verdicts (structured).** A fresh request with metadata plus any snippets goes
through `ask_structured`, with schema `VerdictBatch` and the batch's domain check.

The phases are separate because combining `tools` and a `format` schema in one request is
unreliable on small models, and keeping them apart lets each be measured.

```python
# cleanguard/agent.py
import json
from harness import ask_structured, build_messages, execute, err, tool_defs, tool_message
from .records import Decision
from .rules import make_domain_check
from .run_context import active_run
from .schemas import VerdictBatch, to_meta
from .signals import find_signals, signal_type

def gather_snippets(h, batch, cfg, run_id) -> dict[str, str]:
    p = h.prompts.get("snippet_select")
    metas = [to_meta(r, active_run().profile.name).model_dump() for r in batch]
    msgs = build_messages(p.render(max_reads=cfg.classify.max_snippets_per_batch),
                          user=json.dumps(metas, ensure_ascii=False))
    snippets = {}
    for _ in range(cfg.classify.max_tool_rounds):
        r = h.llm("fast", msgs, tools=tool_defs(["read_snippet"]), run_id=run_id, prompt_id=p.id)
        if not r.tool_calls:
            break
        msgs.append(r.message)
        for call in r.tool_calls:
            if len(snippets) >= cfg.classify.max_snippets_per_batch:
                res = err("read budget for this batch is used up")
            else:
                res = execute(call, allowed={"read"}, logger=h.log, run_id=run_id)
                if res["ok"]:
                    snippets[res["result"]["file_id"]] = res["result"]["text"]
            msgs.append(tool_message(call, res))
    return snippets

def classify_batch(h, batch, cfg, run_id) -> list[Decision]:
    run = active_run()
    run.current_batch = {r.file_id for r in batch}
    snippets = gather_snippets(h, batch, cfg, run_id) if cfg.classify.use_snippets else {}
    for fid, text in snippets.items():                 # snippets can add privacy signals
        rec = run.records[fid]
        rec.signals = sorted(set(rec.signals) | set(find_signals(rec.name, text)))

    metas = [to_meta(r, run.profile.name) for r in batch]
    msgs, pid = classify_messages(h, metas, snippets)
    check = make_domain_check(batch, cfg) if cfg.domain_rules else None

    # Route (M11): fast once, then reasoning with the error, then safe failure
    res = ask_structured(h, msgs, VerdictBatch, profiles=("fast",), max_retry=0,
                         domain_check=check, prompt_id=pid, run_id=run_id)
    used = "llm_fast"
    if not res.ok:
        note = {"role": "user", "content": f"A previous answer was invalid: {res.reason}. "
                                           "Follow every rule and answer each file_id once."}
        res = ask_structured(h, msgs + [note], VerdictBatch, profiles=("reasoning",),
                             max_retry=1, repair=True, domain_check=check,
                             prompt_id=pid, run_id=run_id)
        used = "llm_reasoning"
    run.current_batch = set()

    if not res.ok:                                      # safe failure -> review
        return [Decision(r.file_id, "review", bool(r.signals), signal_type(r.signals),
                         "could not be classified reliably", "fallback") for r in batch]

    out = []
    for v in res.value.verdicts:
        rec = run.records[v.file_id]
        sens = v.sensitive or bool(rec.signals)
        stype = v.sensitive_type if v.sensitive_type != "none" else signal_type(rec.signals)
        out.append(Decision(v.file_id, v.label, sens, stype if sens else "none", v.reason, used))
    return out
```

### 12.1 Routing rules (M11)

`router.py` keeps the policy in one place so experiment configurations can switch it.

| Situation | Profile |
|---|---|
| First attempt of every CLASSIFY batch | `fast` (4B) |
| Batch failed schema or domain check | `reasoning` (8B), with the error message, up to 2 attempts |
| Still failing | no model; every file in the batch becomes `review` (`decided_by="fallback"`) |
| Batch passed on 4B but contains `review` labels | those files re-run as a sub-batch on 8B (`escalate_on_review`) |
| PRIVACY pass | always `reasoning` (8B) |
| `fallback` profile (Gemma 12B) | never used: it offloads on an 8 GB GPU |

The 8B result for a `review` file replaces the 4B result only if it passed validation.

---

## 13. PRIVACY pass (C)

Candidates are every file whose decision has `sensitive=True` or whose record has any signal,
including rule-decided files. They are batched by 8 and sent to `reasoning` with
`privacy_check@v1`, schema `PrivacyBatch`, and the snippets already collected (no new tool
calls).

**Merge rule: privacy can only raise sensitivity, never lower it.**

```python
def merge_privacy(d: Decision, pv, rec) -> Decision:
    sens = d.sensitive or pv.sensitive or bool(rec.signals)
    stype = next((t for t in (d.sensitive_type, pv.sensitive_type, signal_type(rec.signals))
                  if t != "none"), "none") if sens else "none"
    label = "review" if sens and d.label == "deletable" else d.label
    return Decision(d.file_id, label, sens, stype, d.reason, d.decided_by)
```

This costs some extra `review` labels when the 4B over-flags a file, but it can never turn a
flagged file into a deletable one. If the PRIVACY call fails, the decisions stay as they are,
which is already safe.

---

## 14. Domain rules and final invariants (M5, C)

### 14.1 Per-batch domain check, passed to `ask_structured`

Error messages are written to be useful in the repair prompt: they say what to change.

```python
# cleanguard/rules.py
from harness.validation import DomainError

def make_domain_check(batch, cfg):
    recs = {r.file_id: r for r in batch}
    min_age = cfg.scan.min_age_days_for_delete

    def check(b) -> None:
        got = [v.file_id for v in b.verdicts]
        if sorted(got) != sorted(recs):
            raise DomainError(f"answer each file_id exactly once; expected {sorted(recs)}, got {got}")
        for v in b.verdicts:
            r = recs[v.file_id]
            if v.sensitive and v.label == "deletable":
                raise DomainError(f"{v.file_id} is sensitive, so its label must be keep or review")
            if (v.sensitive_type != "none") != v.sensitive:
                raise DomainError(f"{v.file_id}: sensitive_type must be none exactly when sensitive is false")
            if v.label == "deletable" and r.modified_days < min_age:
                raise DomainError(f"{v.file_id} was modified {r.modified_days} days ago; use keep or review")
            if r.protected and v.label != "keep":
                raise DomainError(f"{v.file_id} is in a protected folder; label must be keep")
    return check
```

### 14.2 Final invariant pass (the VALIDATE state)

In the diagram, VALIDATE sends failing batches back to CLASSIFY. In code, that per-batch loop
runs **inside** `classify_batch` through `ask_structured`. The VALIDATE state is a second,
whole-run pass over **all** decisions, rule-decided ones included, and it coerces any violation
to `review`. It should never fire; each time it does, it logs `invariant_violation` with the
file_id and rule, which means there is a bug to fix.

```python
def enforce_invariants(run, cfg, log) -> int:
    fixed = 0
    for fid, d in run.decisions.items():
        r = run.records[fid]
        bad = None
        if d.label == "deletable" and (d.sensitive or r.signals):
            bad = "sensitive_deletable"
        elif d.label == "deletable" and r.modified_days < cfg.scan.min_age_days_for_delete:
            bad = "recent_deletable"
        elif r.protected and d.label != "keep":
            bad = "protected_not_keep"
        if bad:
            d.label, d.decided_by = ("keep" if r.protected else "review"), "invariant"
            log.write("invariant_violation", run_id=run.run_id, file_id=fid, rule=bad)
            fixed += 1
    return fixed
```

---

## 15. State machine (M9, A)

### 15.1 States and allowed transitions

```python
# cleanguard/state_machine.py
from enum import Enum

class S(str, Enum):
    SCAN = "SCAN"; TRIAGE = "TRIAGE"; CLASSIFY = "CLASSIFY"; PRIVACY = "PRIVACY"
    VALIDATE = "VALIDATE"; REPORT = "REPORT"; APPROVE = "APPROVE"
    QUARANTINE = "QUARANTINE"; DONE = "DONE"; RESTORE = "RESTORE"; FAILED = "FAILED"

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
```

`FAILED` is an implementation addition not drawn on the pitch slide. It covers fatal errors
such as Ollama being unreachable (`LLMConnectionError`). Nothing has moved at that point, and
the run can be resumed from its saved state.

### 15.2 Runner

```python
class CleanGuardRun:
    def __init__(self, h, run, cfg, dry_run=False):
        self.h, self.run, self.cfg, self.dry_run = h, run, cfg, dry_run
        self.handlers = {S.SCAN: self.do_scan, S.TRIAGE: self.do_triage,
                         S.CLASSIFY: self.do_classify, S.PRIVACY: self.do_privacy,
                         S.VALIDATE: self.do_validate, S.REPORT: self.do_report}

    def go(self, nxt: S):
        cur = S(self.run.state)
        if nxt not in ALLOWED[cur]:
            raise RuntimeError(f"illegal transition {cur} -> {nxt}")
        self.h.log.write("state_transition", run_id=self.run.run_id, src=cur.value, dst=nxt.value)
        self.run.state = nxt.value
        save_state(self.run)                     # runs/<run_id>/state.json, for resume

    def until_pause(self):
        """Run automatically until APPROVE (needs the user), DONE or FAILED."""
        while S(self.run.state) in self.handlers:
            try:
                self.go(self.handlers[S(self.run.state)]())
            except LLMConnectionError:
                self.go(S.FAILED)

    def do_classify(self) -> S:
        pending = [self.run.records[f] for f in self.run.pending_llm]
        size = self.cfg.classify.batch_size
        for i in range(0, len(pending), size):
            for d in classify_batch(self.h, pending[i:i + size], self.cfg, self.run.run_id):
                self.run.decisions[d.file_id] = d
            save_state(self.run)                 # resume mid-way after a crash
        return S.PRIVACY if self.cfg.privacy.enabled else S.VALIDATE

    def do_report(self) -> S:
        write_report(self.run)
        return S.DONE if self.dry_run else S.APPROVE
```

`do_triage` returns `S.VALIDATE` directly when nothing is pending, which matches the dashed
"rule-decided files skip the LLM" path on the slide. APPROVE and QUARANTINE are driven by the
CLI `approve` command (section 17), not by `until_pause`.

### 15.3 Persisted run state

```
runs/<run_id>/
├── state.json        # state, config name, OS, timestamps, decisions by file_id
├── files.json        # file_id -> rel_path, size, mtime, sha256   (local only, never logged)
├── report.html
└── report.json
```

`state.json` and `files.json` contain file names, so `runs/` is git-ignored and never uploaded.

---

## 16. Safety gate and quarantine (M19 / M13, B)

### 16.1 Path sandbox

```python
# cleanguard/safety.py
import secrets, time
from pathlib import Path

def ensure_inside(root: Path, p: Path) -> Path:
    rp = p.resolve(strict=True)
    if not rp.is_relative_to(root.resolve()):
        raise PermissionError("path escapes the scan root")
    return rp
```

### 16.2 One-time approval tokens

```python
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
```

Tokens live only in memory, are bound to the exact file set, and are issued in exactly one
place: `cli.approve`, after the user confirms. A test asserts that no other module calls
`GATE.issue`.

### 16.3 Moving files

Before each move, re-check the file against the scan record (time-of-check vs time-of-use):
it must still exist, be inside the root, and have the same size and mtime. Otherwise skip it
and report "changed since scan".

```
<quarantine_dir>/<batch_id>/
├── manifest.json
└── files/
    └── f0017__setup_zoom_5.17.exe
```

`batch_id` format: `q-YYYYMMDD-HHMMSS-xxxx`. Manifest entry:

```json
{"file_id": "f0017", "original_path": "C:/Users/.../Downloads/setup_zoom_5.17.exe",
 "quarantine_path": ".../files/f0017__setup_zoom_5.17.exe", "size_bytes": 96894720,
 "sha256": "…", "mtime": 1729000000.0, "moved_at": "2026-11-03T14:02:11", "status": "moved"}
```

Move algorithm per file:

1. Try `os.replace(src, dst)`. This is atomic on the same volume.
2. On `OSError` from a cross-volume move: `shutil.copy2`, compare SHA-256 of source and copy,
   then `os.unlink(src)`. If the hashes differ, delete the copy and keep the source.
3. On `PermissionError` (file open in another app, common on Windows): skip, status
   `"skipped_in_use"`.
4. Append the manifest entry after **each** file, so a crash mid-batch leaves an accurate
   manifest.
5. On any other unexpected error: restore the files already moved in this batch, mark the batch
   `"rolled_back"`, and report the error.

**Restore:** move each entry back. If the original path is taken, restore as
`name (restored).ext`. Create missing parent folders.

**Retention:** CleanGuard never empties the quarantine (that would be permanent deletion). The
report shows the quarantine size and tells the user where it is.

---

## 17. Report and CLI (M18, D)

### 17.1 Commands

```bash
python -m cleanguard scan ~/Downloads [--config C4] [--dry-run]
python -m cleanguard approve <run_id>
python -m cleanguard restore <batch_id>
python -m cleanguard runs                 # list runs and quarantine batches
```

### 17.2 Report

`report.html` is static (no scripts, no external assets) with four groups: **Deletable**
(largest first), **Review**, **Sensitive** (with type badge) and **Keep** (count only). Each row
shows number, name, folder, size, age, reason and `decided_by`. Escape every file name with
`html.escape`, since names are untrusted input. The console prints the same summary with total
reclaimable MB.

### 17.3 Approve flow

1. Load `state.json`; the run must be in `APPROVE`.
2. Show the deletable files, numbered. The user enters `all`, `none`, or numbers like
   `1,3,7-12`.
3. Review files can be added by number. Sensitive files need a second confirmation line.
4. Show the total size, then require the user to type `MOVE`.
5. `token = GATE.issue(run_id, ids, ttl)`, then
   `execute({"function": {"name": "quarantine_files", "arguments": {...}}}, allowed={"read", "write"}, ...)`.
6. Print the batch_id and the restore command. Transition to `DONE`.

Exiting at any prompt transitions `APPROVE → DONE` with nothing moved.

---

## 18. Logging (M14)

The Session 2 logger is reused. CleanGuard adds these events, all keyed by `run_id`:

| Event | Fields |
|---|---|
| `state_transition` | `src`, `dst` |
| `triage_summary` | counts per rule, pending count |
| `decision` | `file_id`, `label`, `sensitive`, `sensitive_type`, `decided_by` |
| `invariant_violation` | `file_id`, `rule` |
| `quarantine_batch` | `batch_id`, `files`, `bytes`, `skipped` |
| `run_summary` | `config`, `os`, totals, wall time |

`llm_call` and `tool_call` events come from the harness as before. `tool_call` logs the
**arguments** (`file_id`, `max_chars`), never the returned text.

**Never** put names, paths, snippets or reasons into a log event. Reasons can quote file
contents. A test scans `calls.jsonl` after a run on the testbed and fails if any testbed file
name appears in it.

---

## 19. Evaluation (M20, D)

### 19.1 Testbed generator

`eval/categories.yaml` describes the 200 files. `make_testbed.py --seed 7 --out testbed/` lays
them out for the current OS and writes `testbed/labels.csv`.

```yaml
- category: old_installers
  count: 30
  names: ["setup_{app}_{ver}.exe", "{app}-{ver}.dmg", "{app}_{ver}_amd64.deb"]
  age_days: [120, 900]
  size_mb: [1, 5]
  acceptable: [deletable]
  sensitive: false
- category: sensitive_fake
  count: 25
  kinds: [thai_id_card_txt, bank_statement_pdf, payslip_docx, password_list_txt, medical_letter_pdf]
  age_days: [60, 700]
  acceptable: [keep, review]
  sensitive: true
```

- Installer extensions match the OS (`.exe` on Windows, `.dmg` on macOS, `.deb` on Linux).
  Contents are random bytes, so the files are small but realistic by name.
- Ages are set with `os.utime(path, (mtime, mtime))`. Only mtime is used, so creation time does
  not need to be set (it cannot be set portably).
- Sensitive fakes contain generated data: Thai IDs with valid checksums, Luhn-valid card
  numbers, and the line `SAMPLE - SYNTHETIC DATA` on every document.
- The protected category is created under `testbed/.git/` and an `app_config/` folder that the
  test profile marks as protected, so the testbed stays inside one folder on every OS.
- The split is fixed by the seed: 60 `dev`, 140 `test`.

`labels.csv`:

```
rel_path,category,acceptable,sensitive,split,size_bytes
Downloads/setup_zoom_5.17.exe,old_installers,deletable,false,test,2310144
Downloads/essay_draft2.docx,superseded_drafts,deletable|review,false,dev,18234
```

Two members label the categories independently before the generator is finalised. Report
their agreement (Cohen's kappa on the category-to-label mapping).

### 19.2 Configurations

`eval/configs.yaml`:

```yaml
C0: {use_llm: false}
C1: {use_llm: true,  use_snippets: false, escalate: false, privacy: false, domain_rules: true}
C2: {use_llm: true,  use_snippets: true,  escalate: false, privacy: false, domain_rules: true}
C3: {use_llm: true,  use_snippets: true,  first_profile: reasoning, privacy: false, domain_rules: true}
C4: {use_llm: true,  use_snippets: true,  escalate: true,  privacy: true,  domain_rules: true}
C5: {use_llm: true,  use_snippets: true,  escalate: true,  privacy: true,  domain_rules: false}
```

- C0: pending files get `review`.
- C5 disables the domain check, the invariant pass **and** signal forcing. It exists only to
  measure what the harness rules prevent, so it runs only in `--dry-run`.

### 19.3 Runner

```bash
python eval/run_eval.py --config C4 --split test --runs 3
```

Every evaluation run is a dry run (stops at REPORT; QUARANTINE is unreachable). Output:
`eval/results/<config>_<os>_<run>.csv` with
`rel_path, label, sensitive, sensitive_type, decided_by, batch_wall_s`, plus the `run_id` to
join with `logs/calls.jsonl`.

### 19.4 Metrics

```python
# eval/metrics.py
import pandas as pd

def metrics(results_csv, labels_csv, split="test"):
    gt = pd.read_csv(labels_csv)
    gt = gt[gt.split == split]
    df = gt.merge(pd.read_csv(results_csv), on="rel_path", suffixes=("_gt", ""))
    acc = df.acceptable.str.split("|")
    may_delete = acc.apply(lambda a: "deletable" in a)
    pred_del = df.label == "deletable"
    llm = df.decided_by.str.startswith("llm") | (df.decided_by == "fallback")
    return {
        "sensitive_marked_deletable": int((df.sensitive_gt & pred_del).sum()),
        "false_delete_rate": (pred_del & ~may_delete).sum() / max((~may_delete).sum(), 1),
        "sensitive_recall": (df.sensitive_gt & df.sensitive).sum() / max(df.sensitive_gt.sum(), 1),
        "reclaim_recall": df.size_bytes[pred_del & may_delete].sum() / max(df.size_bytes[may_delete].sum(), 1),
        "review_rate": (df.label[llm] == "review").mean() if llm.any() else 0.0,
        "label_accuracy": pd.Series([l in a for l, a in zip(df.label, acc)]).mean(),
    }
```

Schema validity (first try and after repair), tool-call validity and latency come from
`logs/calls.jsonl`, grouped by `run_id`, exactly as in the Session 2 README.

| Metric | Target |
|---|---|
| Sensitive marked deletable | **0** (hard gate) |
| False-delete rate | **≤ 2%** |
| Sensitive recall | ≥ 90% |
| Reclaim recall | ≥ 70% |
| Review rate (LLM-handled files) | ≤ 25% |
| Tool-call validity | ≥ 95% |
| Wall time, 200-file run | ≤ 10 min |

### 19.5 Cross-OS protocol

Run C0–C5 × 3 on the main machine. Then run C4 × 3 on Windows, macOS and Linux with the same
seed. Report the metrics table per OS plus latency per OS. Benchmark `fast` and `reasoning` on
each machine with `scripts/hands_on2_eval.py` first, so latency differences are attributed to
hardware, not to CleanGuard.

### 19.6 User acceptance test

Each member runs `scan` on their real Downloads folder (covering all three OSes), then a script
samples 50 marked files for the owner to accept or reject. Pass: zero sensitive files marked
deletable, and at least 80% of marks accepted. Only aggregate counts leave the owner's machine.

---

## 20. Tests (offline, no Ollama)

Reuse the `FakeClient` pattern from `tests/test_offline.py`: it replays queued outputs, and a
dict output stands for a tool-call turn.

| Test | Checks |
|---|---|
| `test_scanner_skips_links` | symlink inside root to outside is not followed (skipped on Windows without symlink rights) |
| `test_scan_refuses_protected_root` | scanning a protected folder raises `PermissionError` |
| `test_profile_matching` | protected/junk patterns per OS, case rules |
| `test_triage_order` | each rule fires; a duplicate with a privacy signal goes to the LLM |
| `test_thai_id_and_luhn` | checksum validators, including invalid numbers |
| `test_domain_rules` | each `DomainError` case in 14.1 |
| `test_garbage_output_becomes_review` | FakeClient returns invalid JSON 3×; all files `review`, `decided_by="fallback"` |
| `test_escalation_to_reasoning` | 4B fails, 8B passes; second request uses the reasoning model |
| `test_privacy_merge_is_monotonic` | privacy saying "not sensitive" cannot clear a flag |
| `test_invariants_catch_bug` | injected bad decision is coerced and logged |
| `test_read_snippet_scoped_to_batch` | foreign file_id → `ok: false` |
| `test_llm_cannot_write` | `quarantine_files` via `execute(allowed={"read"})` → permission denied |
| `test_token_rules` | no token, wrong set, expired, reused → all refused |
| `test_quarantine_restore_roundtrip` | bytes and paths identical after restore; name collision handled |
| `test_changed_since_scan` | file modified after scan is skipped |
| `test_logs_have_no_names` | no testbed file name appears in `calls.jsonl` |
| `test_state_machine_happy_path` | SCAN → … → APPROVE with FakeClient; illegal transition raises |

Optional: a GitHub Actions matrix (`windows-latest`, `macos-latest`, `ubuntu-latest`) running
the offline tests catches path and permission differences without anyone switching machines.

---

## 21. Build order and owners

| When | A — orchestration | B — tools & safety | C — model I/O | D — eval & UI |
|---|---|---|---|---|
| **Now → progress update** | `RunContext`, state machine skeleton, triage | platform profiles, scanner, `read_snippet` | schemas, prompts v1, domain rules, `$ref` check | testbed generator + `labels.csv`, CLI `scan --dry-run` |
| | **Milestone: C1 runs end to end on the dev split** | | | |
| **→ Wed 4 Nov** | snippet phase, router, escalation | safety gate, quarantine + restore | privacy pass, signals, re-benchmark per OS | report.html, approve flow, eval runner + metrics, UAT script |
| | **Milestone: test split frozen, C0–C5 compared, UAT on 3 OSes** | | | |
| **→ Wed 11 Nov** | bug fixes from UAT | cross-OS fixes | prompt v2 from dev-set errors only | final report figures, demo folder |

Freeze the test split before any prompt tuning. Tune only on the dev split.

---

## 22. Risks and open questions

| Risk | Mitigation | Owner |
|---|---|---|
| Ollama `format` ignores `$ref` in nested schemas | probe on day 1; `inline_refs` override (6.2) | C |
| 4B unreliable on 8-file batches | make `batch_size` configurable; measure 4 vs 8 on the dev split | C |
| Tool calls from the 4B malformed or excessive | one round only, budget of 3, executor rejects bad args; C1 vs C2 shows if the tool helps | A |
| Latency over budget on slower machines | triage first; batch; keep the 8B for escalations only | A |
| Scanned PDFs give no text | placeholder text → `review`; images are out of scope | B |
| Files locked by other apps (Windows) | skip with status `skipped_in_use` | B |
| macOS folder-access prompt blocks scans | document in setup; detect `PermissionError` and print the fix | B |
| No team member on real Linux hardware | find one before 4 Nov; a VM without GPU makes Ollama CPU-only and slow | all |
| Privacy pass over-flags, raising the review rate | measure review rate per config; tune `privacy_check` wording on dev only | C |
