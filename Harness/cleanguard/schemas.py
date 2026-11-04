"""Schemas sent to and returned by the model (M4, C)."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

Label = Literal["keep", "deletable", "review"]
SensitiveType = Literal["none", "id_doc", "financial", "medical", "credentials", "personal"]


def inline_refs(schema: dict) -> dict:
    """Replace $ref with the referenced definition, for runtimes that ignore $defs."""
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


class FileMeta(BaseModel):
    """What the LLM sees about one file. No absolute paths, no user names."""
    file_id: str
    name: str
    ext: str
    size: str                    # "540 B", "12.3 KB", "4.9 MB": small files must not look empty
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

    @classmethod
    def model_json_schema(cls, *args, **kwargs):
        return inline_refs(super().model_json_schema(*args, **kwargs))


class PrivacyVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file_id: str = Field(pattern=r"^f\d{4}$")
    sensitive: bool
    sensitive_type: SensitiveType
    reason: str = Field(min_length=5, max_length=120)


class PrivacyBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verdicts: list[PrivacyVerdict] = Field(min_length=1, max_length=8)

    @classmethod
    def model_json_schema(cls, *args, **kwargs):
        return inline_refs(super().model_json_schema(*args, **kwargs))


def human_size(n: int) -> str:
    """size_mb rounded to 0.1 showed every file under 50 KB as 0.0, and the model read
    that as an empty file (C4 run 1 marked 8 small ID and bank documents deletable)."""
    if n < 1000:
        return f"{n} B"
    if n < 1_000_000:
        return f"{n / 1e3:.1f} KB"
    if n < 1_000_000_000:
        return f"{n / 1e6:.1f} MB"
    return f"{n / 1e9:.2f} GB"


def to_meta(r, platform_name: str) -> FileMeta:
    folder = r.rel_path.rsplit("/", 1)[0] + "/" if "/" in r.rel_path else "./"
    return FileMeta(file_id=r.file_id, name=r.name, ext=r.ext,
                    size=human_size(r.size_bytes), modified_days=r.modified_days,
                    folder=folder, platform=platform_name, dup_of=r.dup_of)
