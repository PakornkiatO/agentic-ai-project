"""Router (M11, A). The profile policy lives here so eval configurations can switch it.

| Situation                                        | Profile                                  |
| first attempt of every CLASSIFY batch            | routing.first_profile (fast, 4B)         |
| batch failed schema or domain check              | escalation profile with the error, 2 tries |
| still failing                                    | no model: every file -> review (fallback) |
| batch passed on 4B but has review labels         | those files re-run on 8B (escalate_on_review) |
| PRIVACY pass                                     | always the escalation profile (8B)       |
| fallback profile (Gemma 12B)                     | never used                               |
"""
from __future__ import annotations

from dataclasses import dataclass

DECIDED_BY = {"fast": "llm_fast", "reasoning": "llm_reasoning"}


@dataclass(frozen=True)
class Route:
    first: str
    escalation: str
    escalation_retries: int        # max_retry on the escalation profile (2 attempts)
    escalate_on_review: bool
    privacy: str


def plan(cfg) -> Route:
    r = cfg.routing
    return Route(first=r.first_profile, escalation=r.escalation_profile, escalation_retries=1,
                 escalate_on_review=r.escalate_on_review and r.first_profile != r.escalation_profile,
                 privacy=r.escalation_profile)


def decided_by(profile: str) -> str:
    return DECIDED_BY.get(profile, "llm_reasoning")
