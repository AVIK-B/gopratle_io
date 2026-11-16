"""
outreach.py — send + schedule follow-up + remember the outcome.

Trust model note: the hackathon-winning "Deals Machine" project (Milan AI
Week 2026) won specifically on its trust thesis — the agent prepares and
schedules, but a human approves every send. We copy that exact gate here:
nothing leaves this function as an actual message unless job.approved is
True. In sandbox/test-account mode (the brief explicitly allows this),
"sending" just means writing a clearly-labelled dry-run entry — safe to
record on video, safe to show in the interview, no real messages fired at
real companies from a take-home project.

The lightweight "memory" at the bottom is the honest, buildable version of
the "self-improving" agents that keep winning hackathons: it doesn't train
a model, it just logs outcome -> informs next pitch's few-shot context.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .state import LeadJob, Status

FOLLOWUP_DAYS = 3
_MEMORY_PATH = Path("data/outcome_memory.jsonl")


def send_outreach(job: LeadJob, channel: str = "telegram") -> bool:
    """channel: "telegram" | "whatsapp" | "email" — all sandbox/dry-run here."""
    if not job.approved:
        job.log("outreach", "blocked — awaiting human approval before send", "free")
        return False

    if not job.email and not job.contact_name:
        # Caught by testing, not by either reference design: a lead with no
        # enriched contact and no email has nowhere to actually send a
        # message. "Sending" anyway (even as a dry-run) would print a
        # message that *looks* successful but represents nothing real.
        job.log("outreach", "blocked — no contact or email was ever found for this lead; "
                             "nothing to send to. Needs manual research, not automated outreach.", "free")
        return False

    if not job.pitch_final:
        job.log("outreach", "blocked: no approved pitch is available", "free")
        return False

    now = datetime.now(timezone.utc)
    dry_run = os.environ.get("LIVE_SEND", "0") != "1"
    if dry_run:
        job.log("outreach", f"[DRY-RUN via {channel}] would send to {job.email or job.contact_name}: "
                             f"\"{job.pitch_final[:60]}...\"", "sandbox")
    else:
        try:
            _really_send(job, channel)  # LIVE implementation sketch below
        except Exception as exc:
            job.status = Status.FAILED
            job.failure_reason = f"outreach transport failed: {type(exc).__name__}: {str(exc)[:180]}"
            job.log("outreach", f"blocked: {job.failure_reason}", "sandbox/paid-api")
            return False
        job.log("outreach", f"sent live via {channel}", "sandbox/paid-api")

    # Only commit SENT after the transport succeeds. This makes retries safe:
    # a failed live provider call cannot poison the cooldown or look sent in
    # the Sheet.
    job.sent_at = now.isoformat(timespec="seconds")
    job.status = Status.SENT
    job.followup_at = (now + timedelta(days=FOLLOWUP_DAYS)).isoformat(timespec="seconds")
    job.log("outreach", f"follow-up scheduled for {job.followup_at}", "free")
    return True


def _really_send(job: LeadJob, channel: str) -> None:
    # LIVE implementation sketches — fill in with real sandbox credentials:
    #
    # Telegram:  requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
    #                           json={"chat_id": CHAT_ID, "text": job.pitch_final})
    #
    # WhatsApp (Twilio sandbox): client.messages.create(
    #                           from_="whatsapp:+14155238886", to=f"whatsapp:{job.phone}",
    #                           body=job.pitch_final)
    #
    # Email (smtplib / sandbox SMTP): straightforward MIME send to job.email
    raise NotImplementedError("Wire a real sandbox credential before setting LIVE_SEND=1")


def record_outcome(job: LeadJob, outcome: str) -> None:
    """outcome: "replied" | "cold". Appends to a tiny JSONL memory so future
    pitch drafts for a similar role/company can be given this as context —
    the cheap, honest version of 'learns from outcomes'."""
    if outcome not in {"replied", "cold"}:
        raise ValueError("outcome must be 'replied' or 'cold'")
    job.outcome = outcome
    job.status = Status.REPLIED if outcome == "replied" else Status.COLD
    _MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_MEMORY_PATH, "a") as f:
        f.write(json.dumps({
            "company": job.company, "role_category": job.role_category.value,
            "pitch": job.pitch_final, "outcome": outcome,
        }) + "\n")
    job.log("outreach", f"outcome recorded: {outcome} (written to outcome memory)", "free")


def recall_similar_outcomes(role_category: str, limit: int = 3) -> list[dict]:
    """Pulled by pitch.py (optionally) to few-shot the Producer with what has
    actually worked for this role category before."""
    if not _MEMORY_PATH.exists():
        return []
    rows = [json.loads(l) for l in _MEMORY_PATH.read_text().splitlines() if l.strip()]
    matches = [r for r in rows if r["role_category"] == role_category and r["outcome"] == "replied"]
    return matches[-limit:]
