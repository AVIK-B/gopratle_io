"""
llm_client.py — one call-site for every LLM call in the pipeline.

Why this matters for a 48-hour take-home:
Reviewers need to see the workflow run end-to-end *today*, without you
first provisioning paid enrichment APIs and LLM billing. So this client
has two backends behind one interface:

  - OpenRouter: calls its OpenAI-compatible chat completions API when
                OPENROUTER_API_KEY is set (preferred provider).
  - GeminiLLM: calls Google's generateContent REST API when
               GEMINI_API_KEY is set and is not an OpenRouter token.
  - LiveLLM  : calls Anthropic's Messages API (claude-sonnet-4-6) if
               ANTHROPIC_API_KEY is set and Gemini is not configured.
  - MockLLM  : deterministic, rule-based stand-in used automatically when
               no key is present (or DEMO_MODE=1), so `python demo.py`
               works completely offline for your screen recording.

Swapping backends is an env var, not a code change — that's the point.
"""
from __future__ import annotations

import json
import os
import re

import requests


class LLMClient:
    def __init__(self):
        self.provider = "mock"
        self.live = False
        self._openrouter_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        self._openrouter_model = os.environ.get("OPENROUTER_MODEL", "").strip()
        self._gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
        self._gemini_model = os.environ.get("GEMINI_MODEL", "gemini-3-flash-preview").strip()
        # Older .env files may still use the Gemini variable names after a
        # provider switch. OpenRouter keys begin with sk-or-; recognize them
        # without exposing or rewriting the secret.
        if not self._openrouter_key and self._gemini_key.startswith("sk-or-"):
            self._openrouter_key = self._gemini_key
            self._openrouter_model = self._openrouter_model or self._gemini_model
        if self._openrouter_key and os.environ.get("OPENROUTER_MODE", "1") != "0":
            self.provider = "openrouter"
            self.live = True
            self._openrouter_model = self._openrouter_model or "perplexity/pplx-decider-flash-v1.3:free"
        elif self._gemini_key and os.environ.get("GEMINI_MODE", "1") != "0":
            self.live = True
            self.provider = "gemini"
        elif bool(os.environ.get("ANTHROPIC_API_KEY")) and os.environ.get("DEMO_MODE", "0") != "1":
            self.provider = "anthropic"
            try:
                import anthropic  # pip install anthropic
                self._client = anthropic.Anthropic()
                self._model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
            except Exception:
                self.live = False
                self.provider = "mock"

    def mode(self) -> str:
        if self.provider == "openrouter":
            return f"openrouter/{self._openrouter_model}"
        if self.provider == "gemini":
            return f"gemini/{self._gemini_model}"
        return "anthropic" if self.live else "mock"

    def complete(self, system: str, user: str, json_mode: bool = False) -> str:
        if self.provider == "openrouter":
            return self._openrouter_complete(system, user, json_mode)
        if self.provider == "gemini":
            return self._gemini_complete(system, user, json_mode)
        if self.live:
            resp = self._client.messages.create(
                model=self._model,
                max_tokens=600,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
            text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
            return text.strip()
        return _mock_complete(system, user, json_mode)

    def _openrouter_complete(self, system: str, user: str, json_mode: bool) -> str:
        """OpenRouter's OpenAI-compatible chat completions endpoint."""
        if "decider" in self._openrouter_model.casefold():
            raise RuntimeError(
                f"OpenRouter model '{self._openrouter_model}' is a structured Decisions model, "
                "not a text-generation model. Set OPENROUTER_MODEL to a chat model such as perplexity/sonar."
            )
        # Perplexity Sonar currently rejects response_format. Its JSON tasks
        # are constrained in the system instruction instead. Other chat
        # models keep OpenRouter's structured JSON response option.
        is_sonar = "perplexity/sonar" in self._openrouter_model.casefold()
        if json_mode:
            system = system.rstrip() + "\n\nReturn exactly one valid JSON object and no markdown or surrounding prose."
        payload = {
            "model": self._openrouter_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.2,
            "max_tokens": 1800,
        }
        if json_mode and not is_sonar:
            payload["response_format"] = {"type": "json_object"}
        headers = {
            "Authorization": f"Bearer {self._openrouter_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": os.environ.get("OPENROUTER_SITE_URL", "https://gopratle.com"),
            "X-Title": os.environ.get("OPENROUTER_APP_NAME", "GoPratle ScoutChain"),
        }
        response = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers=headers,
            json=payload,
            timeout=60,
        )
        if not response.ok:
            detail = response.text[:400].replace(self._openrouter_key, "[redacted]")
            raise RuntimeError(f"OpenRouter API error {response.status_code}: {detail}")
        try:
            content = response.json()["choices"][0]["message"]["content"]
            if isinstance(content, list):
                content = "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
            if not isinstance(content, str) or not content.strip():
                raise ValueError("empty response content")
            return content.strip()
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("OpenRouter returned no usable text choice") from exc

    def _gemini_complete(self, system: str, user: str, json_mode: bool) -> str:
        """Call Gemini's REST generateContent endpoint without an SDK.

        Keeping this behind the existing LLMClient makes the pipeline
        provider-neutral and avoids putting the API key in a URL or prompt.
        """
        model = self._gemini_model.removeprefix("models/")
        generation_config = {"maxOutputTokens": 600, "temperature": 0.2}
        if json_mode:
            generation_config["responseMimeType"] = "application/json"
        payload = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": generation_config,
        }
        response = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            headers={"x-goog-api-key": self._gemini_key, "Content-Type": "application/json"},
            json=payload,
            timeout=45,
        )
        if not response.ok:
            detail = response.text[:300].replace(self._gemini_key, "[redacted]")
            raise RuntimeError(f"Gemini API error {response.status_code}: {detail}")
        try:
            data = response.json()
            return data["candidates"][0]["content"]["parts"][0]["text"].strip()
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("Gemini returned no usable text candidate") from exc


# --------------------------------------------------------------------------
# MockLLM: small, deterministic, rule-based stand-ins.
# Good enough to prove the ARCHITECTURE end-to-end; swap in the live key
# for real wording quality.
# --------------------------------------------------------------------------

_LINE2_BY_ROLE = {
    "Founder/Leadership": "We're GoPratle — we run event logistics end-to-end with vetted planners, DJs, and "
                           "crew, so your team's time stays on the roadmap, not on vendor calls.",
    "HR/People": "We're GoPratle — we connect people teams with vetted planners, DJs, and crew, so your next "
                 "offsite or townhall runs itself.",
    "Events/Marketing": "We're GoPratle — we give events teams a vetted, on-call bench of planners, DJs, and "
                         "crew, so you're never scrambling before a show.",
    "Other": "We're GoPratle — we connect teams like yours with vetted planners, DJs, and crew for every event, "
             "big or small.",
    "Unknown": "We're GoPratle — we connect teams like yours with vetted planners, DJs, and crew for every "
               "event, big or small.",
}


def _mock_complete(system: str, user: str, json_mode: bool) -> str:
    # Order matters: the Critic's system prompt also contains the word "pitch"
    # (it's reviewing a pitch), so the reviewer/critic check must come FIRST
    # or every critique call silently gets routed to the pitch-writer instead.
    # (This exact bug shipped in v1 — a bare `except Exception: default to
    # ready_to_ship=True` swallowed it silently. v2's schema validation
    # surfaced it immediately, which is the whole point of validating.)
    s = system.lower()
    if "intent parser" in s:
        return _mock_intent(user)
    if "reviewer" in s or "critique" in s:
        return _mock_critique(user)
    if "pitch" in s:
        return _mock_pitch(user)
    if "classify" in s or "role" in s:
        return _mock_role(user)
    return "[mock-llm] no rule matched for this prompt"


def _mock_intent(user: str) -> str:
    m = re.search(r"for\s+(.+?)\s+in\s+([A-Za-z .]+)", user, re.I)
    company = m.group(1).strip() if m else "Unknown Co"
    city = m.group(2).strip().rstrip(".?! ") if m else "Unknown City"
    return json.dumps({"company": company, "city": city})


def _mock_role(user: str) -> str:
    title = user.lower()
    if any(k in title for k in ["founder", "ceo", "co-founder", "director"]):
        role = "Founder/Leadership"
    elif any(k in title for k in ["hr", "people", "talent", "culture", "employee"]):
        role = "HR/People"
    elif any(k in title for k in ["event", "marketing", "brand", "community"]):
        role = "Events/Marketing"
    else:
        role = "Other"
    return json.dumps({"role_category": role})


def _mock_pitch(user: str) -> str:
    m_company = re.search(r"Company:\s*(.+)", user)
    m_role = re.search(r"Role category:\s*(.+)", user)
    m_sig = re.search(r"Signals:\s*(.+)", user)
    company = m_company.group(1).strip() if m_company else "there"
    role = m_role.group(1).strip() if m_role else "Other"
    signal = m_sig.group(1).strip() if m_sig and m_sig.group(1).strip().lower() != "none" else None

    line1 = f"Hey — saw {company} is growing fast" + (f" ({signal})" if signal else "") + "; congrats!"
    line2 = _LINE2_BY_ROLE.get(role, _LINE2_BY_ROLE["Other"])
    line3 = "Worth a 10-min call this week to see if we're a fit for your next event?"
    return f"{line1}\n{line2}\n{line3}"


def _mock_critique(user: str) -> str:
    m = re.search(r"Pitch:\s*\n(.+)", user, re.S)
    pitch = m.group(1).strip() if m else ""
    lines = [l for l in pitch.splitlines() if l.strip()]
    issues = []
    if len(lines) > 3:
        issues.append({"severity": "medium", "location": "length", "fix": "trim to exactly 3 lines"})
    if "guarantee" in pitch.lower() or "100%" in pitch:
        issues.append({"severity": "high", "location": "tone", "fix": "remove over-promising / spam-trigger language"})
    if "gopratle" not in pitch.lower():
        issues.append({"severity": "low", "location": "branding", "fix": "mention GoPratle by name"})
    ready = len(issues) == 0
    return json.dumps({"issues": issues, "ready_to_ship": ready})
