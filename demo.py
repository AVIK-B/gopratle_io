#!/usr/bin/env python3
"""
demo.py — run the whole BDR agent end-to-end from the command line.

    python demo.py "Zomato" "Gurgaon"
    python demo.py --telegram "Find event leads for Razorpay in Bangalore"

Works with ZERO API keys out of the box (mock LLM + offline enrichment
fixture) — good enough to record the required screen-capture today.
Set ANTHROPIC_API_KEY and DEMO_MODE=0 (+ a real search/people API key) to
run it live against the real internet.

This script is intentionally the thin layer: it just wires together the
same `src/` agents a real Google-Sheets-triggered run or Telegram-triggered
run would use — proving both trigger surfaces drive one orchestrator.
"""
from __future__ import annotations

import sys

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from src.intent_parser import parse_command
from src.orchestrator import run_pipeline
from src.outreach import send_outreach, record_outcome
from src.state import LeadJob
from src.store import get_store
from src.terminal_runner import run_terminal_message, watch_sheet


def _print_header(title: str) -> None:
    print("\n" + "=" * 60)
    print(title)
    print("=" * 60)


def main(argv: list[str]) -> None:
    llm = None
    try:
        store = get_store()
    except RuntimeError as exc:
        raise SystemExit(f"Configuration error: {exc}") from exc

    if argv and argv[0] == "--check-sheet":
        rows = store.read_all_rows()
        print(f"Store connected: {type(store).__name__}; rows readable: {len(rows)}")
        if hasattr(store, "leads_ws"):
            print(f"Leads tab: {store.leads_ws.title}")
        return

    if argv and argv[0] == "--watch-sheets":
        interval = float(argv[1]) if len(argv) > 1 else 5.0
        watch_sheet(store, llm, interval=interval)
        return

    if argv and argv[0] == "--terminal":
        message = " ".join(argv[1:]).strip() or input("Message trigger: ")
        _print_header("TERMINAL TRIGGER [deterministic workflow]")
        try:
            run_terminal_message(message, llm, store)
        except (RuntimeError, ValueError) as exc:
            print(f"Trigger failed: {exc}")
        return

    _print_header("GoPratle BDR Lead Enrichment & Outreach Agent  [deterministic workflow]")

    if argv and argv[0] == "--telegram":
        text = " ".join(argv[1:]) or "Find event leads for Zomato in Gurgaon"
        company, city = parse_command(text, llm)
        source = "telegram"
        print(f"Trigger: Telegram command -> \"{text}\"")
    else:
        company = argv[0] if len(argv) > 0 else "Zomato"
        city = argv[1] if len(argv) > 1 else "Gurgaon"
        source = "sheet"
        print(f"Trigger: Google Sheet row -> Company='{company}', City='{city}'")

    job = LeadJob(company=company, city=city, source=source)

    _print_header("STAGE: Security checkpoint -> public-source research -> deterministic score and pitch")
    run_pipeline(job, llm, store)
    for entry in job.trace:
        print(f"  [{entry['stage']:<12}] {entry['detail']}  ({entry['cost_hint']})" if entry['cost_hint']
              else f"  [{entry['stage']:<12}] {entry['detail']}")

    if job.status.value == "FAILED":
        _print_header("Stopped — see the security/orchestrator log line above for why")
        return

    _print_header("RESULT: Enriched lead + tailored pitch")
    print(f"  Contact      : {job.contact_name} — {job.contact_title}")
    print(f"  Role category: {job.role_category.value}")
    print(f"  Email        : {job.email}  (confidence {job.email_confidence})")
    print(f"  Fit score    : {job.fit_score}/100")
    print(f"  Evidence     : {len(job.evidence_sources)} source(s)")
    print(f"  Next action  : {job.next_action}")
    print(f"  Critique rds : {job.critique_rounds}")
    print(f"  Pitch:\n    " + job.pitch_final.replace("\n", "\n    "))

    _print_header("HUMAN APPROVAL GATE (sandbox: auto-approving for the demo)")
    job.approved = True  # in production this flips to True only via the Sheet/Telegram approve action
    send_outreach(job, channel="telegram")
    store.upsert(job)
    for entry in job.trace[-2:]:
        print(f"  [{entry['stage']}] {entry['detail']}")

    if job.sent_at:
        _print_header("Feedback loop (simulated reply, for the demo)")
        record_outcome(job, "replied")
        store.upsert(job)
        print(f"  Outcome recorded -> {job.outcome}. Future {job.role_category.value} pitches can "
              f"now few-shot off this one via outreach.recall_similar_outcomes().")
    else:
        _print_header("Feedback loop")
        print("  Skipped — nothing was actually sent (see the outreach log line above), "
              "so there's no outcome to simulate.")

    _print_header(f"Done. Full audit trail written to data/run_log.csv, row written to data/leads_sheet.csv")


if __name__ == "__main__":
    main(sys.argv[1:])
