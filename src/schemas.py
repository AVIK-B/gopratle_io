"""
schemas.py — typed contracts between agents, enforced with Pydantic.

This is the concrete version of a real suggestion from the ChatGPT research
docs: don't let one agent hand the next a loose dict that might be missing
a field or have a malformed email. Every inter-agent boundary in the
waterfall now passes through one of these models; a bad value raises
immediately instead of silently propagating into a pitch or a sent
message.

Kept intentionally small — this is a "lightweight" agent, not an attempt
to typed-model the whole CRM. Three contracts cover every boundary that
actually matters:
  EnrichmentHit   — what one waterfall stage returns
  CriticVerdict   — what the Critic agent returns (structured, not prose)
  SheetRow        — what gets written back, i.e. the thing a human reads
"""
from __future__ import annotations

import re
from typing import Optional

from pydantic import BaseModel, Field, field_validator

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class EvidenceItem(BaseModel):
    """A small, human-readable receipt for a discovered fact.

    Live crawling is allowed to pass facts downstream only with a public URL
    and a short excerpt. This keeps the LLM grounded and makes the Sheet
    auditable instead of presenting generated claims as research.
    """
    url: str
    field: str
    excerpt: str = Field(min_length=1, max_length=300)


class EnrichmentHit(BaseModel):
    """What a single waterfall stage (website scrape / search dork / paid
    API / pattern guess) hands back. Validated at the boundary so a
    malformed email or an out-of-range confidence fails fast and loud,
    instead of silently reaching a pitch draft."""
    name: Optional[str] = None
    title: Optional[str] = None
    email: Optional[str] = None
    profile_url: Optional[str] = None
    confidence: float = Field(ge=0.0, le=1.0)
    source: str
    signals: list[str] = Field(default_factory=list)
    evidence: list[EvidenceItem] = Field(default_factory=list)

    @field_validator("email")
    @classmethod
    def email_looks_like_email(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not _EMAIL_RE.match(v):
            raise ValueError(f"'{v}' doesn't look like a valid email — rejecting rather than propagating it")
        return v


class CriticIssue(BaseModel):
    severity: str = Field(pattern="^(low|medium|high)$")
    location: str
    fix: str


class CriticVerdict(BaseModel):
    issues: list[CriticIssue] = Field(default_factory=list)
    ready_to_ship: bool

    @field_validator("ready_to_ship")
    @classmethod
    def no_high_severity_if_ready(cls, v: bool, info) -> bool:
        issues = info.data.get("issues", [])
        if v and any(i.severity == "high" for i in issues):
            raise ValueError("ready_to_ship=True but a high-severity issue is still open — rejecting this verdict")
        return v


class SheetRow(BaseModel):
    """The final, human-facing record. This is what actually gets written
    to the Sheet — validating it here (not just LeadJob.to_row()) is the
    last line of defense before anything reaches a human reviewer."""
    job_id: str
    company: str
    city: str
    status: str
    contact_name: Optional[str] = None
    email: Optional[str] = None
    email_confidence: float = Field(ge=0.0, le=1.0, default=0.0)
    fit_score: int = Field(ge=0, le=100)
    next_action: str = ""
    evidence: str = ""
    pitch_final: Optional[str] = None

    @field_validator("pitch_final")
    @classmethod
    def pitch_is_at_most_three_lines(cls, v: Optional[str]) -> Optional[str]:
        if v and len([l for l in v.splitlines() if l.strip()]) > 3:
            raise ValueError("pitch has more than 3 lines — Critic should have caught this; blocking the write")
        return v
