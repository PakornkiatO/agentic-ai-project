"""Text extraction for read_snippet (B): txt/md/csv/log/json, pdf, docx."""
from __future__ import annotations

import logging
from pathlib import Path

logging.getLogger("pypdf").setLevel(logging.ERROR)   # broken PDFs are expected; no stderr noise

TEXT_EXT = {".txt", ".md", ".csv", ".log", ".json"}
SUPPORTED = TEXT_EXT | {".pdf", ".docx"}


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
        return out.strip()[:max_chars] or "[no extractable text: scanned PDF or image]"
    if ext == ".docx":
        import docx
        out = "\n".join(p.text for p in docx.Document(str(path)).paragraphs)
        return out[:max_chars]
    raise ValueError(f"unsupported file type {ext}")
