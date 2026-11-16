"""
state.py — the single canonical object that flows through the whole pipeline.

Design note (why this exists):
Most hackathon / take-home BDR bots are a chain of prompts glued together —
"do not just build a prompt script, build software architecture" is literally
the #1 piece of advice from AI Agent Olympics 2026 winners. A typed, explicit
state object is what turns this from a prompt script into software: every
agent reads a LeadJob and returns a LeadJob, every transition is logged, and
the whole pipeline can be paused/resumed/replayed from the trace alone.

It is also what lets TWO different trigger surfaces (Google Sheet row,
Telegram/WhatsApp command) feed the exact same orchestrator instead of being
two separate, diverging pipelines.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Optional
import json
import uuid


class Status(str, Enum):
    NEW = "NEW"
    DISCOVERING = "DISCOVERING"
    ENRICHING = "ENRICHING"
    SCORING = "SCORING"
    DRAFTING = "DRAFTING"
    CRITIQUING = "CRITIQUING"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    SCHEDULED = "SCHEDULED"
    SENT = "SENT"
    FOLLOWUP_DUE = "FOLLOWUP_DUE"
    REPLIED = "REPLIED"
    COLD = "COLD"
    FAILED = "FAILED"


class RoleCategory(str, Enum):
    FOUNDER = "Founder/Leadership"
    HR = "HR/People"
    EVENTS = "Events/Marketing"
    OTHER = "Other"
    UNKNOWN = "Unknown"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class TraceEntry:
    ts: str
    stage: str
    detail: str
    cost_hint: str = ""  # cheap / llm-call / paid-api — for the observability log


@dataclass
class LeadJob:
    company: str
    city: str
    source: str = "sheet"          # "sheet" | "telegram" | "whatsapp"
    job_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    sheet_row: Optional[int] = None
    status: Status = Status.NEW

    # --- discovery / enrichment outputs ---
    contact_name: Optional[str] = None
    contact_title: Optional[str] = None
    role_category: RoleCategory = RoleCategory.UNKNOWN
    email: Optional[str] = None
    email_confidence: float = 0.0
    enrichment_source: Optional[str] = None
    profile_url: Optional[str] = None
    signals: list = field(default_factory=list)   # e.g. ["mentions 'offsite'", "50-200 employees"]
    evidence_sources: list[str] = field(default_factory=list)
    evidence: list[dict] = field(default_factory=list)

    # --- scoring ---
    fit_score: int = 0
    next_action: str = ""
    failure_reason: Optional[str] = None

    # --- pitch ---
    pitch_draft: Optional[str] = None
    pitch_final: Optional[str] = None
    critique_rounds: int = 0
    critique_log: list = field(default_factory=list)

    # --- outreach ---
    approved: Optional[bool] = None
    sent_at: Optional[str] = None
    followup_at: Optional[str] = None
    outcome: Optional[str] = None  # "replied" | "cold" | None

    trace: list = field(default_factory=list)

    def log(self, stage: str, detail: str, cost_hint: str = "") -> None:
        self.trace.append(asdict(TraceEntry(ts=_now(), stage=stage, detail=detail, cost_hint=cost_hint)))

    def to_row(self) -> dict:
        """Flat dict for writing back to the Sheet — one row per lead."""
        return {
            "job_id": self.job_id,
            "company": self.company,
            "city": self.city,
            "source": self.source,
            "status": self.status.value,
            "contact_name": self.contact_name or "",
            "contact_title": self.contact_title or "",
            "profile_url": self.profile_url or "",
            "role_category": self.role_category.value,
            "email": self.email or "",
            "email_confidence": round(self.email_confidence, 2),
            "enrichment_source": self.enrichment_source or "",
            "evidence_sources": " | ".join(self.evidence_sources),
            "evidence": json.dumps(self.evidence, ensure_ascii=False),
            "fit_score": self.fit_score,
            "next_action": self.next_action,
            "failure_reason": self.failure_reason or "",
            "pitch_final": self.pitch_final or self.pitch_draft or "",
            "critique_rounds": self.critique_rounds,
            "approved": "" if self.approved is None else str(self.approved),
            "sent_at": self.sent_at or "",
            "followup_at": self.followup_at or "",
            "outcome": self.outcome or "",
        }
