"""
orchestrator.py — the state machine. This is the one place that knows the
ORDER of the pipeline; every agent module above knows nothing about its
neighbors. That separation is what makes this "software architecture" and
not a hardcoded script: you can reorder, swap, or add a stage here without
touching discovery/scoring/pitch/outreach at all.

    NEW -> ENRICHING -> SCORING -> DRAFTING -> CRITIQUING
         -> PENDING_APPROVAL -> (human approves) -> SENT -> FOLLOWUP_DUE

Every transition is written to job.trace AND flushed to the store's Runs
log immediately — so if this process dies mid-pipeline, the sheet already
shows exactly how far it got and why.
"""
from __future__ import annotations

from .state import LeadJob, Status
from .enrichment import run_waterfall
from .scoring import classify_role, score_fit
from .pitch import draft_pitch, critique_and_revise, _valid_three_line_pitch
from .security import run_checkpoint
from .schemas import SheetRow
from pydantic import ValidationError


def run_pipeline(job: LeadJob, llm=None, store=None) -> LeadJob:
    flushed = 0  # cursor: how many trace entries the store already has

    def flush() -> None:
        nonlocal flushed
        store.append_run_log(job, job.trace[flushed:])
        flushed = len(job.trace)

    job.log("orchestrator", f"job {job.job_id} started (source={job.source}; deterministic workflow)")
    flush()

    if not job.company.strip() or not job.city.strip():
        job.status = Status.FAILED
        job.failure_reason = "Company and city are both required before enrichment"
        job.next_action = "complete_company_and_city"
        job.log("orchestrator", job.failure_reason, "free")
        store.upsert(job)
        flush()
        return job

    if not run_checkpoint(job, store):
        flush()
        return job  # blacklisted or in cooldown — stop before spending anything

    try:
        job.status = Status.ENRICHING
        run_waterfall(job)
        flush()

        # Do not spend model calls or create a misleading pitch when the
        # waterfall found no usable target. The next action is explicit so a
        # Sheet operator knows this row needs manual research.
        if not job.contact_name and not job.email:
            job.status = Status.FAILED
            job.failure_reason = "No contact or email found after enrichment"
            job.next_action = "manual_research"
            job.log("orchestrator", "stopped before scoring/drafting: no usable outreach target", "free")
            store.upsert(job)
            flush()
            return job

        job.status = Status.SCORING
        classify_role(job, llm)
        score_fit(job)
        flush()

        job.status = Status.DRAFTING
        draft_pitch(job, llm)
        flush()

        job.status = Status.CRITIQUING
        critique_and_revise(job, llm)
        if not _valid_three_line_pitch(job.pitch_final or ""):
            job.status = Status.FAILED
            job.failure_reason = "Pitch generation failed validation (expected three complete lines with GoPratle in line 2)"
            job.next_action = "revise_pitch"
            job.log("orchestrator", job.failure_reason, "free")
            store.upsert(job)
            flush()
            return job
        flush()
    except Exception as exc:
        job.status = Status.FAILED
        job.failure_reason = f"{type(exc).__name__}: {str(exc)[:240]}"
        job.log("orchestrator", f"pipeline failed safely: {job.failure_reason}", "free")
        store.upsert(job)
        flush()
        return job

    job.status = Status.PENDING_APPROVAL
    job.log("orchestrator", "awaiting human approval before any outreach is sent")

    # Last line of defense: validate the record against the Sheet's own schema
    # before it's written. If the pitch somehow has 4 lines or the fit score
    # is out of range, this blocks the write rather than letting a human
    # approve a record the system itself can't vouch for.
    try:
        SheetRow(**job.to_row())
    except ValidationError as e:
        job.status = Status.FAILED
        job.log("orchestrator", f"BLOCKED before writing to sheet — record failed schema validation: {e}")
        flush()
        return job

    store.upsert(job)
    flush()

    return job
