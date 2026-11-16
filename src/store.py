"""
store.py — the Google Sheet, behind one small interface.

Two implementations, same interface, so the orchestrator never knows which
one it's talking to:

  - CsvSheetStore    : a local CSV file. Zero setup. Default for demos.
  - GoogleSheetStore : real Google Sheets via gspread + a service account.
                       Flip to this for the actual GoPratle sheet by setting
                       GOOGLE_SHEETS_CREDS and GOOGLE_SHEET_ID env vars.

Both also keep a "Runs" log (append-only trace of every pipeline step) —
this is the audit trail a reviewer will actually want to see when you walk
through the build in the interview: it answers "what did the agent try,
and why did it pick this contact / this email / this pitch".
"""
from __future__ import annotations

import csv
import os
from pathlib import Path
from typing import Iterable

from .state import LeadJob, Status

LEADS_FIELDS = [
    "job_id", "company", "city", "source", "status", "contact_name", "contact_title", "profile_url",
    "role_category", "email", "email_confidence", "enrichment_source", "fit_score",
    "evidence_sources", "evidence", "next_action", "failure_reason", "pitch_final", "critique_rounds",
    "approved", "sent_at", "followup_at", "outcome",
]
RUN_LOG_FIELDS = ["ts", "job_id", "stage", "detail", "cost_hint"]


def _env_value(name: str) -> str:
    """Read .env-style values while tolerating an inline comment."""
    return os.environ.get(name, "").split("#", 1)[0].strip()


class CsvSheetStore:
    def __init__(self, path: str = "data/leads_sheet.csv", log_path: str = "data/run_log.csv"):
        self.path = Path(path)
        self.log_path = Path(log_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write_header(self.path, LEADS_FIELDS)
        if not self.log_path.exists():
            self._write_header(self.log_path, RUN_LOG_FIELDS)

    @staticmethod
    def _write_header(path: Path, fields: list) -> None:
        with open(path, "w", newline="") as f:
            csv.DictWriter(f, fieldnames=fields).writeheader()

    def add_trigger_row(self, company: str, city: str, source: str = "sheet") -> None:
        """Simulates a human typing a new Company+City row into the sheet."""
        exists = self.path.exists() and self.path.stat().st_size > 0
        with open(self.path, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=LEADS_FIELDS)
            if not exists:
                w.writeheader()
            w.writerow({"company": company, "city": city, "source": source, "status": Status.NEW.value})

    def read_new_rows(self) -> list[dict]:
        return [r for r in self.read_all_rows()
                if str(r.get("status", "")).upper() in ("", Status.NEW.value) and r.get("company")]

    def read_all_rows(self) -> list[dict]:
        """Used by security.py's cooldown check — needs every row, not just NEW ones."""
        if not self.path.exists():
            return []
        with open(self.path, newline="") as f:
            return list(csv.DictReader(f))

    def upsert(self, job: LeadJob) -> None:
        rows = []
        if self.path.exists():
            with open(self.path, newline="") as f:
                rows = list(csv.DictReader(f))
        row = job.to_row()
        matched = False
        for i, r in enumerate(rows):
            if r.get("job_id") == job.job_id or (not r.get("job_id") and r.get("company") == job.company and r.get("city") == job.city):
                rows[i] = row
                matched = True
                break
        if not matched:
            rows.append(row)
        with open(self.path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=LEADS_FIELDS)
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, "") for k in LEADS_FIELDS})

    def append_run_log(self, job: LeadJob, entries: Iterable[dict]) -> None:
        with open(self.log_path, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=RUN_LOG_FIELDS)
            for e in entries:
                w.writerow({"job_id": job.job_id, **{k: e[k] for k in ["ts", "stage", "detail", "cost_hint"]}})


class GoogleSheetStore:
    """Real backend — same interface as CsvSheetStore. Requires:
         pip install gspread google-auth
         env GOOGLE_SHEETS_CREDS = path to service account json
         env GOOGLE_SHEET_ID     = the target spreadsheet id
    Left deliberately thin: swap this in only once you're past the demo
    and wiring the real GoPratle sheet.
    """

    def __init__(self):
        import gspread  # noqa: F401 (import here so CSV mode never needs this dep)
        import requests
        from gspread.exceptions import APIError, WorksheetNotFound
        sheet_id = _env_value("GOOGLE_SHEET_ID")
        creds_path = _env_value("GOOGLE_SHEETS_CREDS")
        oauth_client_path = _env_value("GOOGLE_OAUTH_CLIENT_SECRETS")
        oauth_token_path = _env_value("GOOGLE_SHEETS_TOKEN") or "data/google_authorized_user.json"
        try:
            sheets_timeout = float(_env_value("GOOGLE_SHEETS_TIMEOUT_SECONDS") or "15")
        except ValueError as exc:
            raise RuntimeError("GOOGLE_SHEETS_TIMEOUT_SECONDS must be a positive number.") from exc
        if sheets_timeout <= 0:
            raise RuntimeError("GOOGLE_SHEETS_TIMEOUT_SECONDS must be a positive number.")
        if oauth_client_path:
            if not Path(oauth_client_path).expanduser().is_file():
                raise RuntimeError(f"OAuth client JSON not found at '{oauth_client_path}'.")
            try:
                gc = gspread.oauth(
                    credentials_filename=oauth_client_path,
                    authorized_user_filename=oauth_token_path,
                )
            except Exception as exc:
                raise RuntimeError(f"Google OAuth sign-in failed: {exc}") from exc
        else:
            from google.oauth2.service_account import Credentials
            scopes = ["https://www.googleapis.com/auth/spreadsheets"]
            creds = Credentials.from_service_account_file(creds_path, scopes=scopes)
            gc = gspread.authorize(creds)
        gc.http_client.timeout = sheets_timeout
        try:
            sh = gc.open_by_key(sheet_id)
            leads_title = os.environ.get("GOOGLE_SHEET_LEADS_TAB", "Leads")
            runs_title = os.environ.get("GOOGLE_SHEET_RUNS_TAB", "Runs")
            try:
                self.leads_ws = sh.worksheet(leads_title)
            except WorksheetNotFound:
                # A new Google Sheet normally starts with Sheet1. Reuse it when
                # the requested Leads tab is absent, otherwise create the tab.
                if leads_title == "Leads" and len(sh.worksheets()) == 1:
                    self.leads_ws = sh.worksheets()[0]
                else:
                    self.leads_ws = sh.add_worksheet(title=leads_title, rows=100, cols=len(LEADS_FIELDS))
            try:
                self.runs_ws = sh.worksheet(runs_title)
            except WorksheetNotFound:
                self.runs_ws = sh.add_worksheet(title=runs_title, rows=100, cols=len(RUN_LOG_FIELDS))
            self._ensure_header(self.leads_ws, LEADS_FIELDS)
            self._ensure_header(self.runs_ws, RUN_LOG_FIELDS)
        except requests.exceptions.Timeout as exc:
            raise RuntimeError(
                f"Google Sheets request timed out after {sheets_timeout:g}s. "
                "Check network access or increase GOOGLE_SHEETS_TIMEOUT_SECONDS."
            ) from exc
        except requests.exceptions.ConnectionError as exc:
            raise RuntimeError(
                "Google Sheets connection failed. Check network access and OAuth configuration."
            ) from exc
        except APIError as exc:
            raise RuntimeError(f"Google Sheets API request failed: {exc}") from exc

    @staticmethod
    def _ensure_header(ws, required: list[str]) -> list[str]:
        raw_header = [str(v).strip() for v in ws.row_values(1)]
        if not any(raw_header):
            ws.update([required], "A1")
            return list(required)

        missing = [
            field for field in required
            if field.casefold() not in {h.casefold() for h in raw_header if h}
        ]
        header: list[str] = []
        used: set[str] = set()
        for index, value in enumerate(raw_header, start=1):
            key = value.casefold()
            if not key or key in used:
                replacement = missing.pop(0) if missing else f"extra_{index}"
                value = replacement
                key = value.casefold()
            header.append(value)
            used.add(key)
        header.extend(missing)
        if header != raw_header:
            ws.update([header], "A1")
        return header

    @staticmethod
    def _canonical_record(record: dict) -> dict:
        aliases = {str(k).strip().casefold(): v for k, v in record.items()}
        return {field: aliases.get(field.casefold(), "") for field in LEADS_FIELDS}

    @staticmethod
    def _values_for_header(row: dict, header: list[str]) -> list:
        values = {str(k).casefold(): v for k, v in row.items()}
        return [values.get(str(h).casefold(), "") for h in header]

    def read_new_rows(self) -> list[dict]:
        return [r for r in self.read_all_rows()
                if str(r.get("status", "")).upper() in ("", Status.NEW.value) and r.get("company")]

    def read_all_rows(self) -> list[dict]:
        header = self._ensure_header(self.leads_ws, LEADS_FIELDS)
        return [
            {**self._canonical_record(record), "_sheet_row": row_number}
            for row_number, record in enumerate(
                self.leads_ws.get_all_records(expected_headers=header), start=2
            )
        ]

    def add_trigger_row(self, company: str, city: str, source: str = "sheet") -> None:
        header = self._ensure_header(self.leads_ws, LEADS_FIELDS)
        row = {"company": company, "city": city, "source": source, "status": Status.NEW.value}
        self.leads_ws.append_row(self._values_for_header(row, header))

    def upsert(self, job: LeadJob) -> None:
        header = self._ensure_header(self.leads_ws, LEADS_FIELDS)
        records = self.leads_ws.get_all_records(expected_headers=header)
        row = job.to_row()
        if job.sheet_row and 2 <= job.sheet_row <= len(records) + 1:
            self.leads_ws.update(
                [self._values_for_header(row, header)],
                f"A{job.sheet_row}",
            )
            return
        for i, r in enumerate(records, start=2):  # row 1 = header
            same_id = str(r.get("job_id", "")) == job.job_id
            same_blank_trigger = not str(r.get("job_id", "")).strip() and str(r.get("company", "")).casefold() == job.company.casefold() and str(r.get("city", "")).casefold() == job.city.casefold()
            if same_id or same_blank_trigger:
                self.leads_ws.update([self._values_for_header(row, header)], f"A{i}")
                return
        self.leads_ws.append_row(self._values_for_header(row, header))

    def append_run_log(self, job: LeadJob, entries: Iterable[dict]) -> None:
        for e in entries:
            self.runs_ws.append_row([e["ts"], job.job_id, e["stage"], e["detail"], e["cost_hint"]])


def get_store():
    creds_path = _env_value("GOOGLE_SHEETS_CREDS")
    oauth_client_path = _env_value("GOOGLE_OAUTH_CLIENT_SECRETS")
    sheet_id = _env_value("GOOGLE_SHEET_ID")
    if sheet_id and not creds_path and not oauth_client_path:
        raise RuntimeError(
            "GOOGLE_SHEET_ID is set but no Google credentials are configured. "
            "Set GOOGLE_OAUTH_CLIENT_SECRETS to a Desktop OAuth client JSON "
            "or GOOGLE_SHEETS_CREDS to a service-account JSON."
        )
    if (creds_path or oauth_client_path) and not sheet_id:
        raise RuntimeError("Google credentials are set but GOOGLE_SHEET_ID is empty.")
    if creds_path and sheet_id:
        normalized = creds_path.replace("/", "\\").lower()
        if "path\\to\\service-account.json" in normalized or normalized.startswith("c:\\path\\"):
            raise RuntimeError(
                "GOOGLE_SHEETS_CREDS still points to the template placeholder. "
                "Set it to the real service-account JSON file path in .env."
            )
        if not Path(creds_path).expanduser().is_file():
            raise RuntimeError(
                f"Google credentials file not found at '{creds_path}'. "
                "Use an absolute path to the downloaded service-account JSON."
            )
        return GoogleSheetStore()
    if oauth_client_path and sheet_id:
        return GoogleSheetStore()
    return CsvSheetStore()
