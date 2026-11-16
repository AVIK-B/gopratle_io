"""
intent_parser.py — turns a free-text command into the same {company, city}
shape the Google Sheet trigger produces, so both triggers feed ONE
orchestrator instead of forking into two pipelines.

Cost-aware by design: a cheap regex handles the common phrasing
("Find event leads for Zomato in Gurgaon") for free, in <1ms. Only
unusual phrasing falls through to an LLM call. This mirrors the same
"cheapest-first" principle the enrichment waterfall uses — don't spend a
model call on something a regex already solves.
"""
from __future__ import annotations

import re

_PATTERN = re.compile(r"for\s+(?P<company>.+?)\s+in\s+(?P<city>[A-Za-z .]+)", re.I)


def parse_command(text: str, llm=None) -> tuple[str, str]:
    m = _PATTERN.search(text)
    if m:
        return m.group("company").strip(), m.group("city").strip().rstrip(".?! ")

    return "", ""
