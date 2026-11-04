"""Loads cleanguard.yaml into a frozen dataclass. Eval configurations override fields."""
from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SETTINGS = ROOT / "cleanguard.yaml"
EVAL_CONFIGS = ROOT / "eval" / "configs.yaml"


@dataclass(frozen=True)
class ScanCfg:
    max_files: int = 5000
    include_hidden: bool = False
    min_age_days_for_delete: int = 30
    partial_download_min_age_days: int = 7


@dataclass(frozen=True)
class ClassifyCfg:
    batch_size: int = 8
    use_snippets: bool = True
    max_snippets_per_batch: int = 3
    snippet_chars: int = 1000
    max_tool_rounds: int = 1


@dataclass(frozen=True)
class PrivacyCfg:
    enabled: bool = True


@dataclass(frozen=True)
class RoutingCfg:
    first_profile: str = "fast"
    escalation_profile: str = "reasoning"
    escalate_on_review: bool = True


@dataclass(frozen=True)
class ApprovalCfg:
    token_ttl_minutes: int = 30


@dataclass(frozen=True)
class PathsCfg:
    runs_dir: Path = ROOT / "runs"


@dataclass(frozen=True)
class Settings:
    scan: ScanCfg = ScanCfg()
    classify: ClassifyCfg = ClassifyCfg()
    privacy: PrivacyCfg = PrivacyCfg()
    routing: RoutingCfg = RoutingCfg()
    approval: ApprovalCfg = ApprovalCfg()
    paths: PathsCfg = PathsCfg()
    use_llm: bool = True
    domain_rules: bool = True
    name: str = "default"


def load_settings(path: str | Path = DEFAULT_SETTINGS, config: str | None = None) -> Settings:
    d = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    runs_dir = Path(d.get("paths", {}).get("runs_dir", "runs"))
    s = Settings(
        scan=ScanCfg(**d.get("scan", {})),
        classify=ClassifyCfg(**d.get("classify", {})),
        privacy=PrivacyCfg(**d.get("privacy", {})),
        routing=RoutingCfg(**d.get("routing", {})),
        approval=ApprovalCfg(**d.get("approval", {})),
        paths=PathsCfg(runs_dir=runs_dir if runs_dir.is_absolute() else ROOT / runs_dir),
        use_llm=d.get("use_llm", True),
        domain_rules=d.get("domain_rules", True),
    )
    if not 1 <= s.classify.batch_size <= 8:
        raise ValueError("classify.batch_size must be 1..8 (VerdictBatch allows at most 8)")
    return apply_config(s, config) if config else s


def apply_config(s: Settings, name: str, configs_path: str | Path = EVAL_CONFIGS) -> Settings:
    """Apply one eval configuration (C0..C5) from eval/configs.yaml (section 19.2)."""
    configs = yaml.safe_load(Path(configs_path).read_text(encoding="utf-8"))
    if name not in configs:
        raise KeyError(f"unknown config {name!r}; known: {sorted(configs)}")
    return with_overrides(s, configs[name], name)


def with_overrides(s: Settings, o: dict, name: str = "custom") -> Settings:
    """Map the short eval keys onto settings fields."""
    classify, privacy, routing = s.classify, s.privacy, s.routing
    if "use_snippets" in o:
        classify = replace(classify, use_snippets=o["use_snippets"])
    if "privacy" in o:
        privacy = replace(privacy, enabled=o["privacy"])
    if "escalate" in o:
        routing = replace(routing, escalate_on_review=o["escalate"])
    if "first_profile" in o:
        routing = replace(routing, first_profile=o["first_profile"])
    return replace(s, classify=classify, privacy=privacy, routing=routing, name=name,
                   use_llm=o.get("use_llm", s.use_llm),
                   domain_rules=o.get("domain_rules", s.domain_rules))
