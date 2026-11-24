"""Map a job onto one of a fixed set of categories.

Every source uses its own taxonomy (or none), so we classify from the title
first, then fall back to whatever category hints the source gave us.
"""
from __future__ import annotations

import re

CATEGORIES = [
    "Software Engineering",
    "Data & Machine Learning",
    "Design",
    "Product",
    "Security",
    "Sales",
    "Marketing",
    "Writing & Content",
    "Customer Support",
    "Finance & Accounting",
    "HR & Recruiting",
    "Legal & Compliance",
    "Healthcare",
    "Education",
    "Operations",
    "Other",
]

# Order matters: the first matching rule wins.
_RULES: list[tuple[str, str]] = [
    ("Legal & Compliance", r"legal|counsel|lawyer|attorney|paralegal|compliance|regulatory|privacy officer"),
    ("Security", r"security|abuse|fraud|trust (and|&) safety|threat|soc analyst|pentest|infosec"),
    ("Data & Machine Learning", r"data|machine learning|\bml\b|\bai\b|analytics|scientist|research engineer|\bbi\b|statistic|llm"),
    ("Design", r"design|\bux\b|\bui\b|illustrat|animator|art director|creative director|graphic"),
    ("Product", r"product manager|product owner|product lead|head of product|\bpm\b|product operations"),
    ("Sales", r"sales|account executive|business development|\bbdr\b|\bsdr\b|account manager|partnerships|revenue|deal desk"),
    ("Software Engineering", r"engineer|developer|software|front[- ]?end|back[- ]?end|full[- ]?stack|devops|\bsre\b|mobile|\bios\b|android|\bqa\b|tester|programmer|architect|firmware|infrastructure"),
    ("Marketing", r"marketing|\bseo\b|growth|brand|social media|communications|\bpr\b|demand gen|community|campaign"),
    ("Writing & Content", r"writer|editor|copywrit|content|journalist|translator|localization"),
    ("Customer Support", r"support|customer success|customer service|help ?desk|customer experience|onboarding specialist"),
    ("Finance & Accounting", r"financ|accountant|accounting|\btax\b|payroll|controller|fp&a|treasury|audit|bookkeep|billing"),
    ("HR & Recruiting", r"recruit|talent|people (ops|partner|operations)|\bhr\b|human resources|sourcer"),
    ("Healthcare", r"nurse|medical|clinical|physician|therapist|pharmac|dental|health ?care|\brn\b|caregiver"),
    ("Education", r"teacher|tutor|instructor|educat|curriculum|professor|lecturer|coach"),
    ("Operations", r"operations|logistics|supply chain|project manager|program manager|office|administrat|assistant|coordinator|procurement|facilities|warehouse|driver"),
]
_COMPILED = [(cat, re.compile(pat, re.I)) for cat, pat in _RULES]


def categorize(title: str, hints: list[str] | None = None) -> str:
    for cat, rx in _COMPILED:
        if rx.search(title or ""):
            return cat
    hint_text = " ".join(hints or [])
    for cat, rx in _COMPILED:
        if rx.search(hint_text):
            return cat
    return "Other"
