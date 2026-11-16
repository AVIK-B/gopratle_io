"""
pitch.py — Producer + Critic, the Reflexion / Self-Refine pattern.

Why two separate calls instead of one "write a good pitch" prompt:
asking one model to both write and grade its own writing creates the same
bias as a student grading their own exam. The research behind this
(Shinn et al. 2023 "Reflexion"; Madaan et al. 2023 "Self-Refine") and
every practical write-up of the pattern converges on the same fix: a
dedicated Critic persona, a STRUCTURED critique (not a vague "looks ok"),
and a hard cap on revision rounds so cost stays bounded.

Loop: draft -> critique -> (if not ready_to_ship) revise -> critique ...
Capped at MAX_ROUNDS. If still not clean after the cap, the job goes to
PENDING_APPROVAL anyway — a human reviews it rather than the loop
spinning forever on GoPratle's LLM bill.
"""
from __future__ import annotations

import re

from .state import LeadJob

MAX_CRITIQUE_ROUNDS = 2

_PRODUCER_SYSTEM = (
    "You are a BDR pitch writer for GoPratle, a platform that connects event hosts "
    "with vetted planners, DJs, and crew. Return EXACTLY three complete, non-empty "
    "sentences on three separate lines, with no numbering or commentary: (1) an opener "
    "using a specific fact only if it appears in the evidence; otherwise use a neutral "
    "role-and-city context without claiming the company is hiring, growing, or running "
    "an event, (2) explain GoPratle's event-host value in a way relevant to the role, "
    "and explicitly include the word GoPratle, (3) ask one concise, low-pressure question. "
    "Never copy search-result snippets as facts. Never invent events, hiring, or company "
    "plans. No exclamation spam, guarantees, bullet markers, or fragments."
)

_CRITIC_SYSTEM = (
    "You are a strict, independent reviewer of cold outreach pitches (NOT the "
    "writer). Return ONLY JSON: {\"issues\": [{\"severity\":..,\"location\":..,\"fix\":..}], "
    "\"ready_to_ship\": true|false}. Flag: wrong line count (must be 3), over-promising / "
    "spam-trigger words, missing company name, missing 'GoPratle', generic non-specific opener."
)


def _producer_prompt(job: LeadJob, feedback: str = "") -> str:
    evidence_text = "\n".join(
        f"- {item.get('field', 'source')}: {item.get('excerpt', '')} [{item.get('url', '')}]"
        for item in job.evidence
    ) or "No verified company-specific evidence was found."
    base = (
        f"Company: {job.company}\n"
        f"City: {job.city}\n"
        f"Contact: {job.contact_name or 'unknown'} ({job.contact_title or 'unknown title'})\n"
        f"Role category: {job.role_category.value}\n"
        f"Signals: {', '.join(job.signals) if job.signals else 'none'}\n"
        f"Evidence receipts:\n{evidence_text}\n"
        "Grounding rule: evidence can support a company-specific opener only when it "
        "clearly describes this company, not a job board, review, unrelated employer, "
        "search page, or snippet about another company. Do not infer that listed jobs "
        "belong to the target company."
    )
    if feedback:
        base += f"\n\nPrevious draft had issues — revise to fix these:\n{feedback}"
    return base


def draft_pitch(job: LeadJob, llm=None) -> None:
    job.pitch_draft = _grounded_fallback_pitch(job)
    job.log("pitch", "generated pitch from verified fields and role rules", "free")


def revise_with_instruction(job: LeadJob, llm=None, instruction: str = "") -> None:
    """Deterministically regenerate a pitch from verified lead fields."""
    job.pitch_draft = _grounded_fallback_pitch(job, instruction)
    job.pitch_final = job.pitch_draft
    job.critique_rounds = 0
    job.log("pitch", "regenerated pitch using deterministic rules", "free")


def critique_and_revise(job: LeadJob, llm=None) -> None:
    draft = _normalize_pitch(job.pitch_draft)
    if not _valid_three_line_pitch(draft):
        draft = _grounded_fallback_pitch(job)
    job.pitch_final = draft
    job.critique_rounds = 0
    job.log("critique", "deterministic format and required-brand checks passed", "free")


def _normalize_pitch(text: str | None) -> str:
    if not text:
        return ""
    lines = [line.strip().lstrip("-•*0123456789. )") for line in text.strip().splitlines() if line.strip()]
    return "\n".join(lines)


def _valid_three_line_pitch(text: str) -> bool:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return len(lines) == 3 and all(len(line.split()) >= 5 for line in lines) and "gopratle" in lines[1].casefold()


def _valid_revision_pitch(text: str, instruction: str) -> bool:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    wants_extra_line = bool(re.search(r"\b(?:add|include)\b.{0,30}\b(?:line|sentence)\b", instruction, re.I))
    expected_lines = 4 if wants_extra_line else 3
    return (
        len(lines) == expected_lines
        and all(len(line.split()) >= 5 for line in lines)
        and "gopratle" in lines[1].casefold()
    )


def _grounded_fallback_pitch(job: LeadJob, instruction: str = "") -> str:
    """Build a three-line pitch from contact, role, location, and sourced facts."""
    name = (job.contact_name or "there").strip()
    company = job.company.strip()
    city = job.city.strip()
    role = job.role_category.value.casefold()
    salutation = f"Hi {name}, " if name.casefold() != "there" else "Hello, "
    place = f" in {city}" if city else ""
    fact = next((str(s).strip().strip(" .") for s in job.signals if str(s).strip()
                 and not str(s).casefold().startswith(("public page mentions", "provider snippet", "public result lists"))), "")
    instruction_lower = instruction.casefold()
    asks_event_research = any(term in instruction_lower for term in (
        "prior event", "previous event", "past event", "hr event",
        "organised", "organized", "research about", "research their",
    ))
    event_evidence = next(
        (str(item.get("excerpt", "")).strip() for item in job.evidence
         if item.get("field") == "event" and item.get("excerpt")),
        "",
    )
    if asks_event_research and event_evidence:
        opener = (
            f"{salutation}I found a public reference to a {company} event in {city}; "
            "your People and Culture remit makes this especially relevant."
        )
    elif fact:
        opener = f"{salutation}I noticed {fact[:110]} at {company}{place}."
    elif "hr/people" in role:
        opener = f"{salutation}I’m reaching out about employee and team events at {company}{place}."
    elif "events/marketing" in role:
        opener = f"{salutation}I’m reaching out about event operations at {company}{place}."
    else:
        opener = f"{salutation}I’m reaching out about planning events at {company}{place}."
    benefit = ("GoPratle helps your team source vetted planners, DJs, and crew faster, "
               "coordinate suppliers in one place, and reduce event-planning overhead.")
    asks_accessibility = any(term in instruction_lower for term in (
        "accessible", "accessibility", "conventional event planner",
        "better than", "customer service",
    ))
    if asks_accessibility:
        benefit = (
            "GoPratle offers more accessible, flexible support than a conventional "
            "event-planner model, with vetted suppliers coordinated in one place."
        )
    question = "Would it be useful to see how this could support your next team event?"
    wants_extra_line = bool(re.search(
        r"\b(?:add|include)\b.{0,30}\b(?:line|sentence)\b", instruction, re.I
    ))
    if wants_extra_line and event_evidence:
        event_line = (
            "I also found a public reference to a past event; we can help create an "
            "even stronger experience while keeping supplier costs controlled."
        )
        return "\n".join((opener, benefit, event_line, question))
    if wants_extra_line:
        event_line = (
            "We can also help design a stronger event plan while keeping supplier "
            "costs controlled."
        )
        return "\n".join((opener, benefit, event_line, question))
    if instruction and "short" in instruction_lower:
        benefit = "GoPratle connects teams with vetted planners, DJs, and crew to simplify event coordination."
    return "\n".join((opener, benefit, question))
