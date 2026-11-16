"""
security.py — the checkpoint that runs BEFORE any enrichment or LLM call.

Two concrete, implementable mitigations (both just narrated as prose in
the ChatGPT research docs, so this is them actually built):

1. A domain/company blacklist. This is explicitly the "excessive agency"
   risk from the OWASP Top 10 for LLM Applications (LLM08): an agent
   that's handed company+city and told "go find and contact someone"
   has, by default, no concept of a target it SHOULDN'T touch —
   competitors, GoPratle's own team, previously-opted-out companies. A
   blacklist is the cheapest possible guardrail and it runs for free,
   before a single paid lookup or LLM call is spent on a lead that was
   always going to be rejected.

2. A send cooldown. Neither ChatGPT doc actually implements idempotency
   beyond "use a unique row ID" — that stops a double-trigger on the
   SAME sheet row, but not a human (or a flaky Telegram client retry)
   re-running "Find event leads for Zomato in Gurgaon" twice in one
   afternoon. This checks the store for a recent SENT job to the same
   company+city and blocks a second send inside the cooldown window.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .state import LeadJob, Status

# Add real competitor / do-not-contact domains here before going live.
BLOCKED_DOMAINS: set[str] = {
    "gopratle.com",   # never let the agent "discover" and pitch its own company
}
COOLDOWN_DAYS = 7


def _domain_for(company: str, fixture_domain: str | None) -> str:
    return (fixture_domain or f"{company.lower().replace(' ', '')}.com").lower()


def run_checkpoint(job: LeadJob, store, fixture_domain: str | None = None) -> bool:
    """Returns True if the job may proceed. Mutates job.status + logs either way."""
    domain = _domain_for(job.company, fixture_domain)
    if domain in BLOCKED_DOMAINS:
        job.status = Status.FAILED
        job.failure_reason = f"Blocked by do-not-contact domain: {domain}"
        job.log("security", f"blocked — {domain} is on the do-not-contact list", "free")
        store.upsert(job)
        return False

    if _in_cooldown(job, store):
        job.status = Status.FAILED
        job.failure_reason = f"Blocked by {COOLDOWN_DAYS}-day outreach cooldown for {job.company} ({job.city})"
        job.log("security", f"blocked — {job.company} ({job.city}) was already contacted within "
                             f"the last {COOLDOWN_DAYS} days (idempotency / anti-spam guard)", "free")
        store.upsert(job)
        return False

    job.log("security", "checkpoint passed — not blacklisted, not in cooldown", "free")
    return True


def _in_cooldown(job: LeadJob, store) -> bool:
    rows = store.read_all_rows() if hasattr(store, "read_all_rows") else []
    now = datetime.now(timezone.utc)
    for r in rows:
        if r.get("company", "").lower() != job.company.lower():
            continue
        if r.get("city", "").lower() != job.city.lower():
            continue
        sent_at = r.get("sent_at")
        if not sent_at:
            continue
        try:
            sent_dt = datetime.fromisoformat(sent_at)
        except ValueError:
            continue
        if now - sent_dt < timedelta(days=COOLDOWN_DAYS):
            return True
    return False
