#!/usr/bin/env python3
"""
telegram_bot.py — the Telegram/WhatsApp-command trigger surface.

Deliberately built on raw `requests` long-polling instead of a heavier
bot framework, so there's exactly one new dependency (requests, which
you almost certainly already have) and the whole file is readable in one
sitting during the HR walkthrough.

Run:
    export TELEGRAM_BOT_TOKEN=xxxx   # a free @BotFather sandbox bot
    python3 telegram_bot.py

Then message the bot: "Find event leads for Zomato in Gurgaon"
It replies with the enriched lead + pitch, and an Approve/Reject choice.
Approve triggers send_outreach(); Reject just logs the rejection.

For WhatsApp, swap this polling loop for the WhatsApp Business Cloud API
webhook (or Twilio's WhatsApp sandbox) — same parse_command() /
run_pipeline() call underneath; only the transport changes.
"""
from __future__ import annotations

import os
import time

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import requests

from src.intent_parser import parse_command
from src.orchestrator import run_pipeline
from src.outreach import send_outreach
from src.state import LeadJob
from src.store import get_store

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
API = f"https://api.telegram.org/bot{TOKEN}"

_PENDING: dict[str, LeadJob] = {}  # job_id -> job, awaiting this chat's approval


def send_message(chat_id: int, text: str, reply_markup: dict | None = None) -> None:
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    requests.post(f"{API}/sendMessage", json=payload, timeout=10)


def handle_command(chat_id: int, text: str, llm=None, store=None) -> None:
    company, city = parse_command(text, llm)
    if not company or not city:
        send_message(chat_id, "Couldn't parse a company + city from that. Try: "
                               "\"Find event leads for Zomato in Gurgaon\"")
        return

    job = LeadJob(company=company, city=city, source="telegram")
    run_pipeline(job, llm, store)
    if job.status.value == "FAILED":
        send_message(chat_id, f"Could not prepare outreach for {job.company} ({job.city}). "
                             f"{job.failure_reason or 'The security or enrichment guard stopped the run.'}")
        return
    _PENDING[job.job_id] = job

    reply = (
        f"Lead found for {job.company} ({job.city}):\n"
        f"{job.contact_name} — {job.contact_title}\n"
        f"Fit score: {job.fit_score}/100\n\n"
        f"Draft pitch:\n{job.pitch_final}\n\n"
        f"Reply /approve_{job.job_id} to send, or /reject_{job.job_id} to skip."
    )
    send_message(chat_id, reply)


def handle_approval(chat_id: int, text: str, store) -> None:
    if text.startswith("/approve_"):
        job_id = text[len("/approve_"):]
        job = _PENDING.get(job_id)
        if not job:
            send_message(chat_id, "Couldn't find that job — it may have expired.")
            return
        job.approved = True
        sent = send_outreach(job, channel="telegram")
        store.upsert(job)
        if sent:
            send_message(chat_id, f"Sent (sandbox dry-run) to {job.contact_name or job.email}. "
                                   f"Follow-up scheduled for {job.followup_at}.")
        else:
            send_message(chat_id, f"Not sent — {job.failure_reason or 'the outreach guard blocked this lead'}. "
                                   "No message left the sandbox.")
    elif text.startswith("/reject_"):
        job_id = text[len("/reject_"):]
        _PENDING.pop(job_id, None)
        send_message(chat_id, "Skipped — no outreach sent.")


def poll_loop() -> None:
    if not TOKEN:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN first (get a free one from @BotFather).")
    store = get_store()
    offset = None
    print("Telegram trigger live. Deterministic workflow. Listening...")
    while True:
        resp = requests.get(f"{API}/getUpdates", params={"timeout": 30, "offset": offset}, timeout=35).json()
        for update in resp.get("result", []):
            offset = update["update_id"] + 1
            msg = update.get("message", {})
            text = msg.get("text", "")
            chat_id = msg.get("chat", {}).get("id")
            if not text or chat_id is None:
                continue
            if text.startswith("/approve_") or text.startswith("/reject_"):
                handle_approval(chat_id, text, store)
            else:
                handle_command(chat_id, text, llm, store)
        time.sleep(1)


if __name__ == "__main__":
    poll_loop()
