"""Shared helpers for the CleanGuard offline tests (no Ollama)."""
import json
import os
import re
import shutil
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from harness import ChatResult, Harness  # noqa: E402

from cleanguard import run_context  # noqa: E402
from cleanguard.platform import load_profile  # noqa: E402
from cleanguard.records import FileRecord  # noqa: E402
from cleanguard.run_context import RunContext  # noqa: E402
from cleanguard.settings import Settings, PathsCfg  # noqa: E402

# Temp folders live inside the repo: on Windows the system temp folder is under
# %LOCALAPPDATA%, which the windows profile protects.
TMP_BASE = ROOT / ".test_tmp"
DAY = 86400


def tmpdir(testcase) -> Path:
    TMP_BASE.mkdir(exist_ok=True)
    d = Path(tempfile.mkdtemp(dir=TMP_BASE))
    testcase.addCleanup(shutil.rmtree, d, ignore_errors=True)
    return d


def make_file(root: Path, rel: str, data: bytes | str = b"x", age_days: int = 100) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data.encode() if isinstance(data, str) else data)
    t = time.time() - age_days * DAY
    os.utime(p, (t, t))
    return p


def settings(tmp: Path, **kw) -> Settings:
    return replace(Settings(paths=PathsCfg(runs_dir=tmp / "runs")), **kw)


def profile(tmp: Path, **kw):
    return replace(load_profile(), quarantine_dir=tmp / "quarantine", **kw)


def record(fid, rel="Downloads/a.bin", size=10, days=100, root=Path("/x"), **kw) -> FileRecord:
    p = root / rel
    return FileRecord(file_id=fid, abs_path=p, rel_path=rel, name=p.name, ext=p.suffix.lower(),
                      size_bytes=size, mtime=0.0, modified_days=days, **kw)


def make_run(tmp: Path, records=(), prof=None, root=None) -> RunContext:
    run = RunContext(run_id="r-test0001", root=root or tmp, profile=prof or profile(tmp),
                     config_name="test")
    run.records = {r.file_id: r for r in records}
    run_context.set_active(run)
    return run


# --------------------------------------------------------------- fake model
def _files_in(messages) -> list[dict]:
    for m in reversed(messages):
        c = m.get("content", "")
        if m.get("role") == "user" and c.startswith("Files:\n"):
            return json.loads(c.split("\n", 2)[1])
        if m.get("role") == "user" and c.startswith("["):
            return json.loads(c)
    return []


def auto_reply(model, messages, schema=None, tools=None, **kw):
    """A well-behaved model: reads one snippet, then answers every file validly."""
    files = _files_in(messages)
    if tools:
        if any(m.get("role") == "tool" for m in messages):
            return "DONE"
        doc = next((f for f in files if f["ext"] in (".txt", ".pdf", ".docx")), None)
        if doc is None:
            return "DONE"
        return {"tool_calls": [{"function": {"name": "read_snippet",
                                             "arguments": {"file_id": doc["file_id"]}}}]}
    is_privacy = "label" not in json.dumps(schema or {})
    out = []
    for f in files:
        sens = bool(re.search(r"id card|bank|payslip|password|medical|scan", f["name"], re.I))
        v = {"file_id": f["file_id"], "sensitive": sens,
             "sensitive_type": "personal" if sens else "none", "reason": "Fake model answer."}
        if not is_privacy:
            v["label"] = "review" if sens else ("keep" if f["modified_days"] < 30 else "deletable")
        out.append(v)
    return json.dumps({"verdicts": out})


class FakeClient:
    """Replays queued outputs, then falls back to `auto` (if given). A dict output stands for
    a tool-call turn. Records every request."""

    def __init__(self, outputs=(), auto=None):
        self.outputs = list(outputs)
        self.auto = auto
        self.requests = []

    def chat(self, model, messages, **kw):
        self.requests.append({"model": model, "messages": list(messages), **kw})
        if self.outputs:
            out = self.outputs.pop(0)
        elif self.auto:
            out = self.auto(model, messages, **kw)
        else:
            raise AssertionError("FakeClient ran out of outputs")
        if isinstance(out, dict):
            return ChatResult(model=model, content="", tool_calls=out["tool_calls"],
                              message={"role": "assistant", "content": "", **out}, wall_s=0.01)
        return ChatResult(model=model, content=out, message={"role": "assistant", "content": out},
                          prompt_tokens=10, output_tokens=5, wall_s=0.01)


def make_harness(testcase, outputs=(), auto=None) -> Harness:
    return Harness(client=FakeClient(outputs, auto), log_file=tmpdir(testcase) / "calls.jsonl")


def verdicts(*items) -> str:
    """verdicts(("f0001", "deletable"), ("f0002", "keep", True, "financial"))"""
    out = []
    for it in items:
        fid, label, sens, stype = (list(it) + [False, "none"])[:4]
        out.append({"file_id": fid, "label": label, "sensitive": sens, "sensitive_type": stype,
                    "reason": "Because the test says so."})
    return json.dumps({"verdicts": out})
