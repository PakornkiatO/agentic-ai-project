"""Deterministic privacy signals (C), on file names and, when available, snippets.

A signal hit forces sensitive=True whatever the model says, and routes the file to PRIVACY.
Keywords are deliberately broad: a false positive costs a "review", a false negative can
cost a private document.
"""
from __future__ import annotations

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
    if any(luhn_valid(m.strip(" -")) for m in CARD.findall(text)):
        hits.append("card_number")
    return hits


def signal_type(signals: list[str]) -> str:
    # strongest evidence first: a checksum-valid number beats a keyword
    if "thai_id" in signals:
        return "id_doc"
    if "card_number" in signals:
        return "financial"
    for s in signals:
        if s.startswith("kw:"):
            return s[3:]
    return "none"
