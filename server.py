#!/usr/bin/env python3
"""Always-on Sheet watcher with local HTTP and terminal approval controls.

Run from the repository directory:
    python server.py

The process polls pending Sheet rows, runs the pipeline, writes the result
back, and keeps PENDING_APPROVAL jobs in memory for human approval. The HTTP
surface binds to localhost by default; the terminal console is usually the
safest approval surface for a demo.
"""
from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from src.enrichment import run_waterfall
from src.orchestrator import run_pipeline
from src.outreach import send_outreach
from src.pitch import revise_with_instruction, _valid_revision_pitch
from src.state import LeadJob, Status
from src.store import get_store
from src.terminal_runner import job_from_row


class AgentServerState:
    def __init__(self, store, llm):
        self.store = store
        self.llm = None
        self.pending: dict[str, LeadJob] = {}
        self.claimed_rows: set[str] = set()
        self.incomplete_notified: set[str] = set()
        self.startup_row_keys = self._snapshot_row_keys()
        self.lock = threading.RLock()
        self.stop_event = threading.Event()

    @staticmethod
    def _base_row_key(row: dict) -> str:
        return str(
            row.get("job_id")
            or f"{row.get('company', '').casefold()}|{row.get('city', '').casefold()}"
        )

    def _snapshot_row_keys(self) -> set[str]:
        counts: dict[str, int] = {}
        keys: set[str] = set()
        for row in self.store.read_all_rows():
            base = self._base_row_key(row)
            counts[base] = counts.get(base, 0) + 1
            keys.add(f"{base}#{counts[base]}")
        return keys

    def poll_once(self) -> list[str]:
        processed: list[str] = []
        with self.lock:
            occurrences: dict[str, int] = {}
            for row in self.store.read_new_rows():
                base_key = self._base_row_key(row)
                occurrences[base_key] = occurrences.get(base_key, 0) + 1
                row_key = f"{base_key}#{occurrences[base_key]}"
                if row_key in self.startup_row_keys:
                    continue
                if row_key in self.claimed_rows:
                    continue
                company = str(row.get("company", "")).strip()
                city = str(row.get("city", "")).strip()
                if not company or not city:
                    # Sheets are often filled cell-by-cell. Leave incomplete
                    # NEW rows untouched and reconsider them on the next poll.
                    # Do not claim or fail them; once both fields exist they
                    # naturally pass through this same pipeline.
                    if row_key not in self.incomplete_notified:
                        missing = "company and city" if not company else "city"
                        print(
                            f"[server] not processing Sheet row {row_key}: "
                            f"missing {missing} (company={company or '<empty>'}; "
                            f"city={city or '<empty>'}); fill it in to start retrieval"
                        )
                        self.incomplete_notified.add(row_key)
                    continue
                self.incomplete_notified.discard(row_key)
                self.claimed_rows.add(row_key)
                job = job_from_row(row, source="sheet")
                run_pipeline(job, self.llm, self.store)
                processed.append(job.job_id)
                if job.status != Status.FAILED:
                    self.pending[job.job_id] = job
                    # Show the completed, evidence-grounded draft before presenting
                    # the approval action. Operators otherwise see only a job ID
                    # and can approve without reviewing the actual message.
                    print("\n" + "=" * 64)
                    print(f"READY FOR HUMAN REVIEW: {job.job_id}")
                    print(f"Company: {job.company} | City: {job.city}")
                    print(f"Contact: {job.contact_name or 'unknown'} — {job.contact_title or 'unknown title'}")
                    if job.profile_url:
                        print(f"Profile: {job.profile_url}")
                    print(f"Email: {job.email or 'unknown'} (confidence {job.email_confidence:.2f})")
                    if job.evidence:
                        print("Evidence:")
                        for item in job.evidence:
                            print(f"  - {item.get('field', 'source')}: {item.get('url', '')}")
                            if item.get("excerpt"):
                                print(f"    {item['excerpt'][:220]}")
                    print("\n3-line pitch to review:")
                    pitch_lines = (job.pitch_final or "").strip().splitlines()
                    if pitch_lines:
                        for line_number, line in enumerate(pitch_lines, 1):
                            print(f"  {line_number}. {line}")
                    else:
                        print("  [No pitch was generated; approval is unavailable]")
                    print("\nApproval commands: yes <job_id> | no <job_id> | prompt <job_id> <revision>")
                    print("=" * 64)
                else:
                    print(f"[server] stopped {job.job_id}: {job.failure_reason}")
        return processed

    def get_pending(self) -> list[dict]:
        with self.lock:
            return [
                {
                    "job_id": job.job_id,
                    "company": job.company,
                    "city": job.city,
                    "contact_name": job.contact_name,
                    "pitch": job.pitch_final,
                    "next_action": job.next_action,
                }
                for job in self.pending.values()
            ]

    def approve(self, job_id: str) -> tuple[bool, str]:
        with self.lock:
            job = self.pending.get(job_id)
            if not job:
                return False, "unknown or expired job_id"
            job.approved = True
            sent = send_outreach(job, channel="server")
            self.store.upsert(job)
            if sent:
                self.pending.pop(job_id, None)
                return True, "sandbox outreach sent"
            return False, job.failure_reason or "outreach guard blocked the send"

    def reject(self, job_id: str) -> tuple[bool, str]:
        with self.lock:
            job = self.pending.pop(job_id, None)
            if not job:
                return False, "unknown or expired job_id"
            job.approved = False
            job.status = Status.PENDING_APPROVAL
            job.log("server", "human rejected outreach; no message sent", "free")
            self.store.upsert(job)
            return True, "rejected; no message sent"

    def revise(self, job_id: str, prompt: str) -> tuple[bool, str]:
        with self.lock:
            job = self.pending.get(job_id)
            if not job:
                return False, "unknown or expired job_id"
            original_draft = job.pitch_draft
            original_final = job.pitch_final
            if any(word in prompt.casefold() for word in (
                "crawl", "research", "enrich", "source", "verify", "event",
                "events", "organized", "organised", "previous", "past",
                "retriev", "evidence",
            )):
                try:
                    run_waterfall(job, refresh_event_context=True)
                except Exception as exc:
                    return False, f"Evidence refresh failed; existing pitch is unchanged: {type(exc).__name__}: {str(exc)[:240]}"
            try:
                revise_with_instruction(job, self.llm, prompt)
            except Exception as exc:
                # Keep the server alive and preserve the last reviewable pitch
                # when OpenRouter is rate/credit limited or unavailable.
                job.pitch_draft = original_draft
                job.pitch_final = original_final
                return False, (
                    "Revision failed; the previous pitch is still pending approval. "
                    f"Provider error: {type(exc).__name__}: {str(exc)[:300]}"
                )
            if not _valid_revision_pitch(job.pitch_final or "", prompt):
                job.pitch_draft = original_draft
                job.pitch_final = original_final
                return False, "Revision rejected: model did not return three complete lines with GoPratle in line 2. Try a more specific prompt."
            self.store.upsert(job)
            return True, "revised pitch (approval is still required):\n" + job.pitch_final


class Handler(BaseHTTPRequestHandler):
    state: AgentServerState

    def _send(self, status: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path
        if path == "/health":
            self._send(200, {"ok": True, "workflow": "deterministic", "pending": len(self.state.pending)})
        elif path == "/pending":
            self._send(200, {"pending": self.state.get_pending()})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path.strip("/").split("/")
        if len(path) != 2 or path[0] not in {"approve", "reject", "revise"}:
            self._send(404, {"error": "use /approve/<job_id>, /reject/<job_id>, or /revise/<job_id>"})
            return
        job_id = unquote(path[1])
        length = min(int(self.headers.get("Content-Length", "0")), 50_000)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self._send(400, {"error": "body must be JSON"})
            return
        if path[0] == "approve":
            ok, message = self.state.approve(job_id)
        elif path[0] == "reject":
            ok, message = self.state.reject(job_id)
        else:
            ok, message = self.state.revise(job_id, str(body.get("prompt", "")).strip())
        self._send(200 if ok else 409, {"ok": ok, "message": message})

    def log_message(self, format, *args):  # noqa: A002
        return


def _console(state: AgentServerState) -> None:
    print("Console commands: list | yes <job_id> | no <job_id> | prompt <job_id> <text> | quit")
    while not state.stop_event.is_set():
        try:
            command = input("server> ").strip()
        except (EOFError, KeyboardInterrupt):
            state.stop_event.set()
            return
        if not command:
            continue
        parts = command.split(maxsplit=2)
        action = parts[0].casefold()
        if action in {"quit", "exit"}:
            state.stop_event.set()
            return
        if action == "list":
            print(json.dumps(state.get_pending(), ensure_ascii=False, indent=2))
        elif action in {"yes", "approve"} and len(parts) >= 2:
            print(state.approve(parts[1])[1])
        elif action in {"no", "reject"} and len(parts) >= 2:
            print(state.reject(parts[1])[1])
        elif action in {"prompt", "revise"} and len(parts) == 3:
            ok, message = state.revise(parts[1], parts[2])
            print(message)
        elif action in {"prompt", "revise"} and len(parts) == 1:
            if len(state.pending) == 1:
                job_id = next(iter(state.pending))
                instruction = input("Revision instruction: ").strip()
                if instruction:
                    ok, message = state.revise(job_id, instruction)
                    print(f"{job_id}: {message}")
            else:
                print("Use: prompt <job_id> <revision>. Pending leads:")
                print(json.dumps(state.get_pending(), ensure_ascii=False, indent=2))
        elif len(state.pending) == 1:
            # Treat a plain sentence as a revision instruction when exactly
            # one lead is awaiting review, so operators need not memorize IDs.
            job_id = next(iter(state.pending))
            ok, message = state.revise(job_id, command)
            print(f"{job_id}: {message}")
        elif len(state.pending) > 1:
            pending_ids = ", ".join(state.pending)
            print(
                f"Multiple leads are pending ({pending_ids}); choose one explicitly:\n"
                f"prompt {next(iter(state.pending))} {command}"
            )
        else:
            print("Use: list | yes <job_id> | no <job_id> | prompt <job_id> <text> | quit")


def main() -> None:
    host = os.environ.get("SERVER_HOST", "127.0.0.1")
    port = int(os.environ.get("SERVER_PORT", "8787"))
    interval = max(1.0, float(os.environ.get("SHEET_POLL_SECONDS", "5")))
    try:
        store = get_store()
    except RuntimeError as exc:
        raise SystemExit(f"Configuration error: {exc}") from exc
    state = AgentServerState(store, None)

    def worker():
        while not state.stop_event.is_set():
            try:
                state.poll_once()
            except Exception as exc:
                print(f"[server] poll error: {type(exc).__name__}: {exc}")
            state.stop_event.wait(interval)

    Handler.state = state
    httpd = ThreadingHTTPServer((host, port), Handler)
    http_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker_thread = threading.Thread(target=worker, daemon=True)
    http_thread.start()
    worker_thread.start()
    print(f"ScoutChain server listening on http://{host}:{port}; polling every {interval:g}s; workflow=deterministic")
    try:
        _console(state)
    finally:
        state.stop_event.set()
        httpd.shutdown()
        httpd.server_close()


if __name__ == "__main__":
    main()
