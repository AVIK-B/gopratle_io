"""
scoring.py — role classification + fit score.

Mirrors the role-specialized pattern several 2026 hackathon-winning sales
agents use (e.g. a CrewAI "Business Intelligence Specialist" feeding a
"Senior Sales Director" who assigns a 0-100 priority score): don't treat
every lead the same, and don't treat every contact title the same either.
A Founder, an HR lead and an Events lead each need a different pitch angle
(see pitch.py) — so this stage's job is to decide WHICH angle, and HOW
hard to chase this lead.
"""
from __future__ import annotations

from .state import LeadJob, RoleCategory


def classify_role(job: LeadJob, llm=None) -> None:
    if not job.contact_title:
        job.role_category = RoleCategory.UNKNOWN
        job.log("score", "no title found, role = Unknown", "free")
        return
    title = job.contact_title.casefold()
    if any(word in title for word in ("human resources", "hr ", "people", "talent", "recruit")):
        job.role_category = RoleCategory.HR
    elif any(word in title for word in ("event", "community", "marketing", "brand")):
        job.role_category = RoleCategory.EVENTS
    elif any(word in title for word in ("founder", "chief executive", "ceo", "managing director", "president")):
        job.role_category = RoleCategory.FOUNDER
    else:
        job.role_category = RoleCategory.OTHER
    job.log("score", f"classified role as {job.role_category.value} using title rules", "free")


def score_fit(job: LeadJob) -> None:
    """Simple, explainable scoring — no black box. Judges/HR can see exactly
    why a lead got the score it got, which matters more than squeezing out
    a fancier model for a lightweight internal tool."""
    score = 40  # baseline: we found *a* company+city
    if job.contact_name:
        score += 20
    if job.email:
        score += 10
    if job.email_confidence >= 0.6:
        score += 10
    if job.role_category in (RoleCategory.FOUNDER, RoleCategory.HR, RoleCategory.EVENTS):
        score += 15
    if job.signals:
        score += 5
    job.fit_score = min(score, 100)
    if not job.contact_name and not job.email:
        job.next_action = "manual_research"
    elif job.email_confidence < 0.6:
        job.next_action = "verify_email_before_send"
    elif job.fit_score >= 80:
        job.next_action = "approve_and_send"
    else:
        job.next_action = "review_and_personalize"
    job.log("score", f"fit_score={job.fit_score} (name={bool(job.contact_name)}, "
                      f"email_conf={job.email_confidence}, role={job.role_category.value}, "
                      f"signals={len(job.signals)}, next_action={job.next_action})", "free")
