from __future__ import annotations

import tempfile
import unittest
import os
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from src.intent_parser import parse_command
from src.enrichment import (
    _calibrate_hit_confidence,
    _is_relevant_contact,
    _is_relevant_mailbox,
    _search_public_sources,
)
from src.llm_client import LLMClient
from src.orchestrator import run_pipeline
from src.outreach import record_outcome, send_outreach
from src.pitch import _valid_revision_pitch, revise_with_instruction
from src.schemas import CriticVerdict, EnrichmentHit, SheetRow
from src.security import run_checkpoint
from src.state import LeadJob, Status
from src.store import CsvSheetStore, get_store
from src.store import GoogleSheetStore
from src.terminal_runner import run_terminal_message
from telegram_bot import handle_command


class CountingMock(LLMClient):
    def __init__(self):
        super().__init__()
        self.calls = 0

    def complete(self, system: str, user: str, json_mode: bool = False) -> str:
        self.calls += 1
        return super().complete(system, user, json_mode)


def make_store(tmp: str) -> CsvSheetStore:
    root = Path(tmp)
    return CsvSheetStore(str(root / "leads.csv"), str(root / "runs.csv"))


class AgentTests(unittest.TestCase):
    def setUp(self):
        self._offline_env = patch.dict(
            os.environ,
            {"GEMINI_MODE": "0", "OPENROUTER_MODE": "0", "OPENROUTER_API_KEY": "",
             "GEMINI_API_KEY": "", "ANTHROPIC_API_KEY": "", "DEMO_MODE": "1"},
            clear=False,
        )
        self._offline_env.start()

    def tearDown(self):
        self._offline_env.stop()

    def test_known_lead_runs_to_approval_with_evidence_and_next_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = make_store(tmp)
            llm = CountingMock()
            job = run_pipeline(LeadJob("Zomato", "Gurgaon"), llm, store)

            self.assertEqual(job.status, Status.PENDING_APPROVAL)
            self.assertEqual(job.contact_name, "Rhea Malhotra")
            self.assertGreaterEqual(len(job.evidence_sources), 1)
            self.assertEqual(job.next_action, "verify_email_before_send")
            self.assertEqual(len(job.pitch_final.splitlines()), 3)
            self.assertEqual(llm.calls, 0)  # normal workflow must never call an LLM

    def test_revision_adds_requested_event_line_when_event_evidence_exists(self):
        job = LeadJob("Infosys", "Melbourne", contact_name="Associate Vice")
        job.evidence = [{
            "url": "https://www.infosys.com/events/melbourne",
            "field": "event",
            "excerpt": "Infosys employee event in Melbourne",
        }]
        instruction = "retrieve previous organised events and add another line about offering a better event at lower expense"

        revise_with_instruction(job, instruction=instruction)

        self.assertEqual(len(job.pitch_final.splitlines()), 4)
        self.assertIn("stronger experience", job.pitch_final)
        self.assertTrue(_valid_revision_pitch(job.pitch_final, instruction))

    def test_revision_applies_accessibility_instruction(self):
        job = LeadJob("HCL", "Melbourne", contact_name="Swetha Veeramally")
        instruction = "include the fact that our customer services are more accessible and better than conventional event planners"

        revise_with_instruction(job, instruction=instruction)

        self.assertIn("more accessible", job.pitch_final)
        self.assertIn("conventional event-planner model", job.pitch_final)
        self.assertTrue(_valid_revision_pitch(job.pitch_final, instruction))

    def test_unknown_lead_stops_before_model_spend(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = make_store(tmp)
            llm = CountingMock()
            job = run_pipeline(LeadJob("UnknownCo", "Pune"), llm, store)

            self.assertEqual(job.status, Status.FAILED)
            self.assertEqual(job.failure_reason, "No contact or email found after enrichment")
            self.assertEqual(job.next_action, "manual_research")
            self.assertIsNone(job.pitch_final)
            self.assertEqual(llm.calls, 0)

    def test_enrichment_rejects_press_release_phrases_as_people(self):
        self.assertFalse(_is_relevant_contact("Outsourcing Services", "Human Resources"))
        self.assertFalse(_is_relevant_contact("Everest Group", "Human Resources"))
        self.assertFalse(_is_relevant_contact("Managing Director", "Chief Executive Officer"))
        self.assertFalse(_is_relevant_contact("Corporate Sustainability", "Events"))
        self.assertFalse(_is_relevant_contact(
            "Career Development Military",
            "People with Disabilities Social Impact Work We Do Career Opportunities",
        ))
        self.assertFalse(_is_relevant_contact("World Tour", "event Meet Our Experts"))
        self.assertFalse(_is_relevant_contact("Toll Free", "HR Phone numbers General"))
        self.assertTrue(_is_relevant_contact("Rhea Malhotra", "Head of Employee Experience"))

    def test_company_domain_alias_uses_hcltech_domain(self):
        from src.enrichment import _guess_domain

        self.assertEqual(_guess_domain("HCL"), "hcltech.com")

    def test_enrichment_sanitizes_navigation_contact_but_keeps_mailbox(self):
        from src.enrichment import _sanitize_contact_hit

        garbage = EnrichmentHit(
            name="Career Development Military",
            title="People with Disabilities Social Impact Work We Do Career Opportunities",
            confidence=0.65,
            source="public website crawl",
        )
        self.assertIsNone(_sanitize_contact_hit(garbage))

        mailbox = EnrichmentHit(
            name="Career Development Military",
            title="People with Disabilities Social Impact Work We Do Career Opportunities",
            email="events@example.com",
            confidence=0.5,
            source="public search result mailbox",
        )
        sanitized = _sanitize_contact_hit(mailbox)
        self.assertIsNotNone(sanitized)
        self.assertIsNone(sanitized.name)
        self.assertEqual(sanitized.email, "events@example.com")

    def test_mailbox_classifier_rejects_generic_and_investor_addresses(self):
        self.assertFalse(_is_relevant_mailbox("info@glassdoor.com", "Glassdoor company information"))
        self.assertFalse(_is_relevant_mailbox("investor.relations@wipro.com", "Investor Relations"))
        self.assertTrue(_is_relevant_mailbox("events@zomato.com", "Events and employee experience"))

    def test_search_rejects_foreign_domain_even_when_snippet_names_target(self):
        class FakeResponse:
            ok = True
            @staticmethod
            def json():
                return {"results": [{
                    "url": "https://example-news.com/story",
                    "title": "TCS Mumbai update",
                    "snippet": "TCS Mumbai CEO Rhea Malhotra leads the team.",
                }]}

        with patch("src.enrichment.SEARCH_MODE", "live"), patch.dict(
            "os.environ", {"TAVILY_API_KEY": "test", "SERPAPI_KEY": ""}, clear=False
        ), patch("requests.post", return_value=FakeResponse()):
            self.assertIsNone(_search_public_sources("TCS", "Mumbai"))

    def test_search_accepts_owned_domain_city_and_named_role(self):
        class FakeResponse:
            ok = True
            @staticmethod
            def json():
                return {"results": [{
                    "url": "https://www.zomato.com/blog/team",
                    "title": "Head of Employee Experience — Rhea Malhotra",
                    "snippet": "Zomato Mumbai team information.",
                }]}

        with patch("src.enrichment.SEARCH_MODE", "live"), patch.dict(
            "os.environ", {"TAVILY_API_KEY": "test", "SERPAPI_KEY": ""}, clear=False
        ), patch("requests.post", return_value=FakeResponse()):
            hit = _search_public_sources("Zomato", "Mumbai")
        self.assertIsNotNone(hit)
        self.assertEqual(hit.name, "Rhea Malhotra")

    def test_confidence_calibration_rewards_independent_evidence(self):
        hit = EnrichmentHit(
            name="Rhea Malhotra",
            title="Head of Employee Experience",
            confidence=0.62,
            source="public search result",
            evidence=[
                {"url": "https://zomato.com/team", "field": "contact", "excerpt": "Rhea Malhotra Mumbai"},
                {"url": "https://zomato.com/contact", "field": "contact", "excerpt": "Rhea Malhotra Mumbai"},
            ],
        )
        calibrated = _calibrate_hit_confidence(hit, "Zomato", "Mumbai")
        self.assertIsNotNone(calibrated)
        self.assertGreater(calibrated.confidence, 0.7)

    def test_search_accepts_specialized_query_override(self):
        class FakeResponse:
            ok = True

            @staticmethod
            def json():
                return {"results": [{
                    "url": "https://www.zomato.com/team",
                    "title": "Events and Community Lead — Rhea Malhotra",
                    "snippet": "Zomato Mumbai people team.",
                }]}

        with patch("src.enrichment.SEARCH_MODE", "live"), patch.dict(
            "os.environ", {"TAVILY_API_KEY": "test", "SERPAPI_KEY": ""}, clear=False
        ), patch("requests.post", return_value=FakeResponse()) as post:
            hit = _search_public_sources(
                "Zomato",
                "Mumbai",
                'site:zomato.com ("Events" OR "People") "Mumbai"',
            )
        self.assertIsNotNone(hit)
        self.assertIn('"Events"', post.call_args.kwargs["json"]["query"])

    def test_search_rejects_salary_pages_and_location_as_person(self):
        class FakeResponse:
            ok = True

            @staticmethod
            def json():
                return {"results": [{
                    "url": "https://www.glassdoor.com/Salaries/new-york-ny-employee-experience-manager-salary.htm",
                    "title": "New York — Employee Experience Manager salary",
                    "snippet": "Employee Experience Manager in New York, NY.",
                }]}

        with patch("src.enrichment.SEARCH_MODE", "live"), patch.dict(
            "os.environ", {"TAVILY_API_KEY": "test", "SERPAPI_KEY": ""}, clear=False
        ), patch("requests.post", return_value=FakeResponse()):
            self.assertIsNone(_search_public_sources("Glassdoor", "NY"))

    def test_search_rejects_person_quoted_for_another_employer(self):
        class FakeResponse:
            ok = True

            @staticmethod
            def json():
                return {"results": [{
                    "url": "https://www.oracle.com/report",
                    "title": "Guadalupe Huerta, Chief Financial Officer, Seguros Monterrey New York Life",
                    "snippet": "Oracle report discussing enterprise planning in New York.",
                }]}

        with patch("src.enrichment.SEARCH_MODE", "live"), patch.dict(
            "os.environ", {"TAVILY_API_KEY": "test", "SERPAPI_KEY": ""}, clear=False
        ), patch("requests.post", return_value=FakeResponse()):
            self.assertIsNone(_search_public_sources("Oracle", "New York"))

    def test_search_rejects_person_without_requested_city(self):
        class FakeResponse:
            ok = True

            @staticmethod
            def json():
                return {"results": [{
                    "url": "https://www.tcs.com/newsroom/press-release",
                    "title": "Milind Lakkad, Chief HR Officer",
                    "snippet": "TCS offices are growing in India.",
                }]}

        with patch("src.enrichment.SEARCH_MODE", "live"), patch.dict(
            "os.environ", {"TAVILY_API_KEY": "test", "SERPAPI_KEY": ""}, clear=False
        ), patch("requests.post", return_value=FakeResponse()):
            self.assertIsNone(_search_public_sources("TCS", "Oslo"))

    def test_search_rejects_event_landing_page_as_person(self):
        class FakeResponse:
            ok = True

            @staticmethod
            def json():
                return {"results": [{
                    "url": "https://www.wipro.com/events/join-wipro-at-oracle-ai-world-tour-mumbai",
                    "title": "World Tour — event Meet Our Experts",
                    "snippet": "Join Wipro at Oracle AI World Tour Mumbai.",
                }]}

        with patch("src.enrichment.SEARCH_MODE", "live"), patch.dict(
            "os.environ", {"TAVILY_API_KEY": "test", "SERPAPI_KEY": ""}, clear=False
        ), patch("requests.post", return_value=FakeResponse()):
            self.assertIsNone(_search_public_sources("Wipro", "Mumbai"))

    def test_search_extracts_hr_linkedin_profile_from_company_and_city(self):
        class FakeResponse:
            ok = True

            @staticmethod
            def json():
                return {"results": [{
                    "url": "https://www.linkedin.com/in/anita-rao",
                    "title": "Anita Rao - Head of Human Resources at Wipro | LinkedIn",
                    "snippet": "Anita Rao, Head of Human Resources at Wipro, Mumbai.",
                }]}

        with patch("src.enrichment.SEARCH_MODE", "live"), patch.dict(
            "os.environ", {"TAVILY_API_KEY": "test", "SERPAPI_KEY": ""}, clear=False
        ), patch("requests.post", return_value=FakeResponse()):
            hit = _search_public_sources(
                "Wipro",
                "Mumbai",
                'site:linkedin.com/in/ "Head of Human Resources" "Wipro" "Mumbai"',
            )
        self.assertIsNotNone(hit)
        self.assertEqual(hit.name, "Anita Rao")
        self.assertEqual(hit.profile_url, "https://www.linkedin.com/in/anita-rao")
        self.assertTrue(hit.title.casefold().startswith("head of human resources"))

    def test_linkedin_slug_replaces_metadata_label_name(self):
        class FakeResponse:
            ok = True

            @staticmethod
            def json():
                return {"results": [{
                    "url": "https://www.linkedin.com/in/neha-narula",
                    "title": "Neha Narula - HR Manager at TCS | LinkedIn",
                    "snippet": "Current role: HR Manager Seniority: Manager, Human Resources at TCS, London.",
                }]}

        with patch("src.enrichment.SEARCH_MODE", "live"), patch.dict(
            "os.environ", {"TAVILY_API_KEY": "test", "SERPAPI_KEY": ""}, clear=False
        ), patch("requests.post", return_value=FakeResponse()):
            hit = _search_public_sources(
                "TCS",
                "London",
                'site:linkedin.com/in/ "HR Manager" "TCS" "London"',
            )
        self.assertIsNotNone(hit)
        self.assertEqual(hit.name, "Neha Narula")
        self.assertNotEqual(hit.name, "Manager Seniority")

    def test_dedicated_email_retrieval_requires_owned_domain(self):
        from src.enrichment import stage_email_retrieval

        with patch("src.enrichment.USE_FIXTURES", False), patch(
            "src.enrichment._search_public_sources",
            return_value=EnrichmentHit(
                email="events@zomato.com",
                confidence=0.7,
                source="public search result mailbox",
                evidence=[{
                    "url": "https://www.zomato.com/contact",
                    "field": "email",
                    "excerpt": "events@zomato.com",
                }],
            ),
        ):
            hit = stage_email_retrieval("Zomato", "Mumbai", "Rhea Malhotra", "zomato.com")
        self.assertIsNotNone(hit)
        self.assertEqual(hit.email, "events@zomato.com")
        self.assertIn("dedicated email retrieval", hit.source)

    def test_person_search_accepts_matching_company_email(self):
        class FakeResponse:
            ok = True

            @staticmethod
            def json():
                return {"results": [{
                    "url": "https://www.zomato.com/leadership",
                    "title": "Anita Rao Head of Human Resources at Zomato",
                    "snippet": "Anita Rao Mumbai anita.rao@zomato.com",
                }]}

        with patch("src.enrichment.SEARCH_MODE", "live"), patch.dict(
            "os.environ", {"TAVILY_API_KEY": "test", "SERPAPI_KEY": ""}, clear=False
        ), patch("requests.post", return_value=FakeResponse()):
            hit = _search_public_sources(
                "Zomato",
                "Mumbai",
                '"Anita Rao" "Zomato" "Head of Human Resources" email',
                "Anita Rao",
            )
        self.assertIsNotNone(hit)
        self.assertEqual(hit.email, "anita.rao@zomato.com")

    def test_hunter_person_email_requires_name_domain_and_score(self):
        from src.enrichment import stage_person_email_api

        class FakeResponse:
            ok = True

            @staticmethod
            def json():
                return {"data": {
                    "email": "anita.rao@zomato.com",
                    "first_name": "Anita",
                    "last_name": "Rao",
                    "score": 93,
                }}

        with patch("src.enrichment.USE_FIXTURES", False), patch.dict(
            "os.environ", {"HUNTER_API_KEY": "test"}, clear=False
        ), patch("requests.get", return_value=FakeResponse()) as get:
            hit = stage_person_email_api(
                "Anita Rao", "Zomato", "zomato.com", "Head of Human Resources"
            )
        self.assertIsNotNone(hit)
        self.assertEqual(hit.email, "anita.rao@zomato.com")
        self.assertEqual(get.call_args.kwargs["params"]["domain"], "zomato.com")

    def test_hunter_does_not_enrich_directory_labels(self):
        from src.enrichment import stage_person_email_api

        with patch.dict("os.environ", {"HUNTER_API_KEY": "test"}, clear=False), patch(
            "requests.get"
        ) as get:
            hit = stage_person_email_api(
                "Toll Free", "IBM", "ibm.com", "HR Phone numbers General"
            )
        self.assertIsNone(hit)
        get.assert_not_called()

    def test_hr_retrieval_falls_through_until_hr_role_is_found(self):
        from src.enrichment import stage_hr_retrieval

        event_hit = EnrichmentHit(
            name="Rhea Malhotra",
            title="Events & Community Lead",
            confidence=0.72,
            source="public search result",
            evidence=[{
                "url": "https://zomato.com/events",
                "field": "contact",
                "excerpt": "Rhea Malhotra Events Mumbai",
            }],
        )
        hr_hit = EnrichmentHit(
            name="Anita Rao",
            title="Head of Human Resources",
            confidence=0.72,
            source="public search result",
            evidence=[{
                "url": "https://zomato.com/leadership",
                "field": "contact",
                "excerpt": "Anita Rao Head of Human Resources Mumbai",
            }],
        )
        with patch("src.enrichment.USE_FIXTURES", False), patch(
            "src.enrichment._search_public_sources",
            side_effect=[event_hit, hr_hit],
        ):
            hit = stage_hr_retrieval("Zomato", "Mumbai")
        self.assertEqual(hit.name, "Anita Rao")
        self.assertIn("HR-priority retrieval", hit.source)

    def test_contact_parser_removes_city_and_rejects_other_employer(self):
        from src.enrichment import _name_near_role, _attributed_to_other_company
        import re
        text = "TCS inaugurates facility in Mumbai Rajesh Gopinathan, Chief Executive Officer, TCS said."
        role = re.search(r"Chief Executive Officer", text)
        name = _name_near_role(text, role, re.compile(r"\b[A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,}){1,2}\b"), "TCS", "Mumbai")
        self.assertEqual(name, "Rajesh Gopinathan")
        unrelated = "Kimberly Mathisen, Head of Innovation & Research at MUNCH in Oslo."
        self.assertTrue(_attributed_to_other_company(unrelated, "Kimberly Mathisen", "TCS"))

    def test_blacklist_blocks_before_enrichment(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = make_store(tmp)
            job = LeadJob("GoPratle", "Gurgaon")
            self.assertFalse(run_checkpoint(job, store))
            self.assertEqual(job.status, Status.FAILED)
            self.assertIn("do-not-contact", job.failure_reason)

    def test_cooldown_blocks_recent_sent_company_city(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = make_store(tmp)
            existing = LeadJob("Zomato", "Gurgaon")
            existing.sent_at = "2999-01-01T00:00:00+00:00"
            existing.status = Status.SENT
            store.upsert(existing)
            retry = LeadJob("zomato", "gurgaon")
            self.assertFalse(run_checkpoint(retry, store))
            self.assertEqual(retry.status, Status.FAILED)
            self.assertIn("cooldown", retry.failure_reason)

    def test_gemini_provider_uses_configured_model(self):
        class FakeResponse:
            ok = True
            text = ""

            @staticmethod
            def json():
                return {"candidates": [{"content": {"parts": [{"text": "{\"ok\":true}"}]}}]}

        with patch.dict("os.environ", {"GEMINI_API_KEY": "test-secret", "GEMINI_MODEL": "gemini-3-flash-preview", "GEMINI_MODE": "1"}, clear=False):
            with patch("src.llm_client.requests.post", return_value=FakeResponse()) as post:
                client = LLMClient()
                self.assertEqual(client.mode(), "gemini/gemini-3-flash-preview")
                self.assertEqual(client.complete("system", "user", json_mode=True), '{"ok":true}')
                self.assertIn("gemini-3-flash-preview:generateContent", post.call_args.args[0])
                self.assertEqual(post.call_args.kwargs["headers"]["x-goog-api-key"], "test-secret")

    def test_send_requires_approval_and_only_commits_after_success(self):
        job = LeadJob("Acme", "Delhi", contact_name="Asha", email="asha@acme.com")
        job.pitch_final = "One\nTwo\nThree"
        self.assertFalse(send_outreach(job))
        self.assertIsNone(job.sent_at)

        job.approved = True
        with patch("src.outreach._really_send", side_effect=RuntimeError("provider down")):
            with patch.dict("os.environ", {"LIVE_SEND": "1"}):
                self.assertFalse(send_outreach(job))
        self.assertIsNone(job.sent_at)
        self.assertEqual(job.status, Status.FAILED)

    def test_dry_run_send_returns_success_and_schedules_followup(self):
        job = LeadJob("Acme", "Delhi", contact_name="Asha", email="asha@acme.com")
        job.pitch_final = "One\nTwo\nThree"
        job.approved = True
        with patch.dict("os.environ", {"LIVE_SEND": "0"}):
            self.assertTrue(send_outreach(job))
        self.assertEqual(job.status, Status.SENT)
        self.assertIsNotNone(job.followup_at)

    def test_deterministic_pitch_and_schema_guards(self):
        from src.pitch import draft_pitch, critique_and_revise
        job = LeadJob("Acme", "Delhi", contact_name="Asha", contact_title="HR Director")
        draft_pitch(job)
        critique_and_revise(job)
        self.assertEqual(len(job.pitch_final.splitlines()), 3)
        self.assertIn("GoPratle", job.pitch_final)
        with self.assertRaises(ValidationError):
            EnrichmentHit(source="bad", confidence=1.5)
        with self.assertRaises(ValidationError):
            SheetRow(job_id="1", company="A", city="B", status="X", fit_score=101)

    def test_trigger_parser_handles_regex_path(self):
        company, city = parse_command("Find event leads for Razorpay in Bangalore")
        self.assertEqual((company, city), ("Razorpay", "Bangalore"))

    def test_telegram_reports_failed_enrichment_without_queuing_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            messages = []
            store = make_store(tmp)
            with patch("telegram_bot.send_message", side_effect=lambda chat, text, reply_markup=None: messages.append(text)):
                handle_command(7, "Find event leads for UnknownCo in Pune", LLMClient(), store)
            self.assertEqual(len(messages), 1)
            self.assertIn("Could not prepare outreach", messages[0])

    def test_terminal_trigger_reads_back_sheet_row_and_supports_edit(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = make_store(tmp)
            answers = iter(["no", "2", "Hello Zomato|GoPratle can help|Open to a quick chat?", "yes"])
            job = run_terminal_message(
                "Find event leads for Zomato in Gurgaon",
                CountingMock(),
                store,
                input_fn=lambda _prompt: next(answers),
                output=lambda _text: None,
            )
            self.assertEqual(job.status, Status.SENT)
            self.assertEqual(store.read_all_rows()[0]["status"], Status.SENT.value)

    def test_google_sheet_header_mapping_preserves_existing_title_case(self):
        row = {"company": "Acme", "city": "Delhi", "status": "NEW"}
        self.assertEqual(
            GoogleSheetStore._values_for_header(row, ["Company", "City", "Status"]),
            ["Acme", "Delhi", "NEW"],
        )

    def test_google_sheet_header_normalizes_blank_and_duplicate_columns(self):
        class FakeWorksheet:
            def __init__(self):
                self.header = ["job_id", "company", "", "", "city", "company"]
                self.updated = None

            def row_values(self, _row):
                return self.header

            def update(self, values, _range):
                self.updated = values[0]
                self.header = values[0]

        worksheet = FakeWorksheet()
        header = GoogleSheetStore._ensure_header(
            worksheet, ["job_id", "company", "city", "status"]
        )
        self.assertEqual(len(header), len(set(item.casefold() for item in header)))
        self.assertNotIn("", header)
        self.assertIn("status", {item.casefold() for item in header})
        self.assertEqual(worksheet.updated, header)

    def test_server_skips_rows_present_before_startup(self):
        from server import AgentServerState

        class FakeStore:
            def __init__(self):
                self.rows = [{"company": "OldCo", "city": "London", "status": "NEW"}]

            def read_all_rows(self):
                return list(self.rows)

            def read_new_rows(self):
                return [row for row in self.rows if row.get("status") in ("", "NEW")]

        state = AgentServerState(FakeStore(), None)
        self.assertEqual(state.poll_once(), [])

    def test_server_processes_row_added_after_startup(self):
        from server import AgentServerState

        class FakeStore:
            def __init__(self):
                self.rows = [{"company": "OldCo", "city": "London", "status": "NEW"}]

            def read_all_rows(self):
                return list(self.rows)

            def read_new_rows(self):
                return [row for row in self.rows if row.get("status") in ("", "NEW")]

        store = FakeStore()
        state = AgentServerState(store, None)
        store.rows.append({"company": "NewCo", "city": "Paris", "status": "NEW"})
        with patch("server.run_pipeline") as pipeline:
            pipeline.side_effect = lambda job, _llm, _store: setattr(job, "status", Status.FAILED)
            self.assertEqual(len(state.poll_once()), 1)

    def test_sheet_id_without_credentials_fails_loudly(self):
        with patch.dict("os.environ", {"GOOGLE_SHEET_ID": "sheet-id", "GOOGLE_SHEETS_CREDS": "", "GOOGLE_OAUTH_CLIENT_SECRETS": ""}, clear=False):
            with self.assertRaisesRegex(RuntimeError, "no Google credentials"):
                get_store()

    def test_invalid_outcome_is_rejected(self):
        with self.assertRaises(ValueError):
            record_outcome(LeadJob("Acme", "Delhi"), "maybe")


if __name__ == "__main__":
    unittest.main()
