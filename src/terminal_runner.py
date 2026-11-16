"""Terminal trigger and human approval loop.

The terminal is intentionally a thin transport layer: it parses a message,
retrieves or creates the matching row in the configured Sheet store, then
hands the same LeadJob to the existing orchestrator. The approval loop never
auto-sends; a human can approve, edit the full pitch, request a guided
revision, or reject it.
"""
from __future__ import annotations

import time
import uuid
from typing import Callable

from pydantic import ValidationError

from .enrichment import run_waterfall
from .intent_parser import parse_command
from .orchestrator import run_pipeline
from .outreach import send_outreach
from .pitch import revise_with_instruction
from .schemas import SheetRow
from .state import LeadJob, Status

InputFn = Callable[[str], str]
OutputFn = Callable[[str], None]


def _same_lead(row: dict, company: str, city: str) -> bool:
    return (
        str(row.get("company", "")).strip().casefold() == company.strip().casefold()
        and str(row.get("city", "")).strip().casefold() == city.strip().casefold()
    )


def _pending_row(row: dict) -> bool:
    return str(row.get("status", "")).strip().upper() in {"", Status.NEW.value}


def job_from_row(row: dict, source: str = "sheet") -> LeadJob:
    """Normalize a Sheet row into the canonical LeadJob object."""
    return LeadJob(
        company=str(row.get("company", "")).strip(),
        city=str(row.get("city", "")).strip(),
        source=str(row.get("source") or source),
        job_id=str(row.get("job_id") or uuid.uuid4().hex[:8]),
        sheet_row=int(row["_sheet_row"]) if row.get("_sheet_row") else None,
    )


def get_or_create_trigger_job(store, company: str, city: str) -> LeadJob:
    """Use a pending Sheet row; otherwise append one and retrieve it again.

    Re-reading after append is important for Google Sheets because the Sheet
    remains the source of truth instead of a terminal-only in-memory object.
    """
    rows = [r for r in store.read_all_rows() if _same_lead(r, company, city) and _pending_row(r)]
    if not rows:
        store.add_trigger_row(company, city, source="terminal")
        rows = [r for r in store.read_all_rows() if _same_lead(r, company, city) and _pending_row(r)]
    if not rows:
        raise RuntimeError("The trigger row was not readable after it was added to the Sheet")
    return job_from_row(rows[-1], source="terminal")


def _print_job(job: LeadJob, output: OutputFn) -> None:
    output(f"\nLead: {job.company} ({job.city})")
    output(f"Contact: {job.contact_name or 'unknown'} — {job.contact_title or 'unknown title'}")
    output(f"Email: {job.email or 'unknown'} (confidence {job.email_confidence:.2f})")
    output(f"Fit score: {job.fit_score}/100 | Next action: {job.next_action}")
    if job.evidence:
        output("Evidence receipts:")
        for item in job.evidence:
            output(f"  - {item.get('field')}: {item.get('url')} — {item.get('excerpt', '')[:180]}")
    output("\nCurrent pitch:\n" + (job.pitch_final or "<no pitch available>"))


def _read_full_text(input_fn: InputFn, output: OutputFn) -> str:
    output("Paste the replacement pitch. Use | between the 3 lines, or enter multiple lines and finish with a blank line.")
    first = input_fn("Pitch: ")
    if "|" in first:
        return "\n".join(part.strip() for part in first.split("|"))
    lines = [first]
    while True:
        line = input_fn("")
        if not line:
            break
        lines.append(line)
    return "\n".join(lines).strip()


def _validate_pitch(job: LeadJob) -> str | None:
    try:
        SheetRow(**job.to_row())
    except ValidationError as exc:
        return str(exc).splitlines()[0]
    return None


def interactive_approval(
    job: LeadJob,
    store,
    llm=None,
    input_fn: InputFn = input,
    output: OutputFn = print,
) -> bool:
    """Wait for yes/no and support edit or prompt-driven revision."""
    while True:
        _print_job(job, output)
        decision = input_fn("\nApprove and send? [yes/no]: ").strip().lower()
        if decision in {"yes", "y", "approve", "send"}:
            job.approved = True
            sent = send_outreach(job, channel="terminal")
            store.upsert(job)
            output("Sent in sandbox." if sent else "Not sent: the outreach guard blocked this action.")
            return sent
        if decision not in {"no", "n", "reject", "edit", "prompt"}:
            output("Please type yes or no.")
            continue

        output("No send occurred. Choose: [1] reject, [2] edit the full text, [3] revise with a prompt")
        choice = input_fn("Choice: ").strip().lower()
        if choice in {"1", "reject", "cancel", "no"}:
            job.approved = False
            job.status = Status.PENDING_APPROVAL
            job.log("terminal", "human rejected outreach; no message sent", "free")
            store.upsert(job)
            output("Rejected. No message was sent.")
            return False
        if choice in {"2", "edit", "text"}:
            replacement = _read_full_text(input_fn, output)
            if replacement:
                job.pitch_final = replacement
                error = _validate_pitch(job)
                if error:
                    output(f"Edit rejected: {error}")
                else:
                    job.log("terminal", "human edited the pitch before approval", "free")
            continue
        if choice in {"3", "prompt", "revise"}:
            instruction = input_fn("Revision prompt: ").strip()
            if not instruction:
                output("Empty prompt; nothing changed.")
                continue
            if any(word in instruction.casefold() for word in (
                "crawl", "research", "enrich", "source", "verify", "event",
                "events", "organized", "organised", "previous", "past",
                "retriev", "evidence",
            )):
                output("Refreshing public evidence before revising. Only cited page evidence may enter the pitch.")
                run_waterfall(job, refresh_event_context=True)
            revise_with_instruction(job, llm, instruction)
            store.upsert(job)
            continue
        output("Choose 1, 2, or 3.")


def run_terminal_message(message: str, llm=None, store=None, input_fn: InputFn = input, output: OutputFn = print) -> LeadJob:
    company, city = parse_command(message, llm)
    if not company or not city:
        raise ValueError("Could not parse company and city. Try: Find event leads for Zomato in Gurgaon")
    job = get_or_create_trigger_job(store, company, city)
    run_pipeline(job, llm, store)
    if job.status == Status.FAILED:
        output(f"Stopped: {job.failure_reason or 'security or enrichment guard blocked the run'}")
        return job
    interactive_approval(job, store, llm=llm, input_fn=input_fn, output=output)
    return job


def watch_sheet(store, llm=None, interval: float = 5.0, input_fn: InputFn = input, output: OutputFn = print) -> None:
    """Poll pending rows so manually added Sheet rows become triggers."""
    output(f"Watching the Sheet for new rows every {interval:g}s. Press Ctrl+C to stop.")
    while True:
        for row in store.read_new_rows():
            job = job_from_row(row, source="sheet")
            output(f"\nNew Sheet trigger: {job.company} ({job.city})")
            run_pipeline(job, llm, store)
            if job.status != Status.FAILED:
                interactive_approval(job, store, llm=llm, input_fn=input_fn, output=output)
        time.sleep(interval)
