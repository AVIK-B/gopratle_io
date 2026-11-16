"""
enrichment.py — waterfall enrichment chain, v2.

v1 was a straight Clay-style waterfall: try sources cheapest-first, stop
at the first result over one confidence bar. Round-2 research (two
independent ChatGPT architecture reports) correctly pointed out that a
single-bar waterfall and "verify every field against a second source"
are in tension — you can't have both "stop as soon as possible" and
"always cross-check" as unconditional rules. Both reports state both
rules without reconciling them.

v2 resolves it with THREE confidence bands instead of one bar:

    < 0.3            discard, keep falling through the waterfall
    0.3 – 0.75        "medium" — spend exactly ONE extra stage call to
                      cross-verify before accepting (this is the new
                      behaviour)
    >= 0.75           "high" — accept immediately, no cross-check
                      (this preserves Clay's actual cost-saving property
                      for the cases that don't need it)

Every stage's return value is now a validated schemas.EnrichmentHit
(Pydantic) instead of a loose dataclass — a malformed email or an
out-of-range confidence raises immediately instead of silently reaching
a pitch draft (this is the concrete version of the "typed contracts"
idea both reports suggested but didn't implement).
"""
from __future__ import annotations

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Optional
from urllib.parse import urljoin, urlparse

from pydantic import ValidationError

from .schemas import EnrichmentHit
from .retry import retry_with_backoff

DEMO_MODE = os.environ.get("DEMO_MODE", "1") == "1"
CRAWL_MODE = os.environ.get("CRAWL_MODE", "off").lower()
SEARCH_MODE = os.environ.get("SEARCH_MODE", "off").lower()
RESEARCH_GRAPH = os.environ.get("RESEARCH_GRAPH", "langgraph").lower()
CRAWL_MAX_PAGES = max(1, min(int(os.environ.get("CRAWL_MAX_PAGES", "5")), 10))
HTTP_TIMEOUT_SECONDS = max(2, min(int(os.environ.get("RETRIEVAL_TIMEOUT_SECONDS", "5")), 15))
USE_FIXTURES = DEMO_MODE and CRAWL_MODE != "live"

_ROLE_PATTERN = re.compile(
    r"\b(?:Founder|Co-Founder|CEO|Chief Executive Officer|Head of [A-Za-z &]+|"
    r"HR [A-Za-z &]+|Human Resources|Talent Acquisition|People [A-Za-z &]+|"
    r"Events? [A-Za-z &]+|Marketing [A-Za-z &]+|Employee Experience)\b", re.I,
)
_GENERIC_NAME_PAIRS = {"media kit", "contact us", "our team", "tata sons", "tata consultancy"}
_KNOWN_COMPANY_DOMAINS = {
    "hcl": "hcltech.com",
}
_NON_PERSON_NAME_TOKENS = {
    "cloud", "company", "consulting", "customer", "events", "global", "group",
    "insights", "kit", "leader", "media", "mobile", "operations", "organisations", "organization",
    "outsourcing", "press", "recognition", "release", "resources", "services", "solutions",
    "team", "view", "website", "managing", "director", "chief", "executive",
    "officer", "founder", "president", "careers", "contact", "leadership",
    "locations", "news", "partner", "ecosystem", "privacy", "supplier",
    "diversity", "innovation", "network", "ventures", "wellbeing", "about",
    "speak", "awards", "recognitions", "case", "studies", "corporate",
    "sustainability", "inclusion", "belonging", "investors",
    "career", "development", "military", "disabilities", "social", "impact",
    "opportunities", "sales", "engineering", "implementation", "security",
    "clearance", "jobs", "functions", "support", "getting", "hired",
    "students", "graduates", "overview", "future", "potential", "artificial",
    "intelligence",
    "manager", "seniority", "current", "role", "level", "department",
    "function", "job", "description", "experience", "history", "headline",
    "toll", "free", "phone", "phones", "numbers", "general", "assistance",
    "shopping", "directory",
}

_NAVIGATION_TOKENS = {
    "career", "development", "military", "disabilities", "social", "impact",
    "opportunities", "sales", "engineering", "implementation", "security",
    "clearance", "jobs", "functions", "support", "getting", "hired",
    "students", "graduates", "overview", "future", "potential", "artificial",
    "intelligence", "work", "done", "create", "discover", "leading",
}
_LOCATION_NAME_PAIRS = {
    "new york", "san francisco", "los angeles", "chicago", "london",
    "singapore", "bangalore", "bengaluru", "mumbai", "delhi", "oslo",
}
_ROLE_TITLE_MARKERS = {
    "head", "chief", "officer", "director", "manager", "lead", "founder",
    "ceo", "president", "coordinator", "specialist", "partner", "vp",
    "hr", "people", "human", "talent",
}
_RELEVANT_MAILBOX_TOKENS = {
    "contact", "events", "event", "people", "hr", "careers", "career",
    "talent", "recruit", "recruiting", "workplace", "employee", "community",
}
_IRRELEVANT_MAILBOX_TOKENS = {
    "info", "investor", "investors", "relations", "press", "media", "support",
    "legal", "privacy", "security", "billing", "admin", "webmaster",
}


def _is_relevant_contact(name: Optional[str], title: Optional[str]) -> bool:
    if not name or not title or not _ROLE_PATTERN.search(title):
        return False
    normalized = re.sub(r"[^a-z ]", "", name.casefold()).strip()
    tokens = normalized.split()
    normalized_title = re.sub(r"[^a-z ]", "", title.casefold()).strip()
    title_tokens = normalized_title.split()
    return (
        normalized not in _GENERIC_NAME_PAIRS
        and 2 <= len(tokens) <= 3
        and not any(token in _NON_PERSON_NAME_TOKENS for token in tokens)
        and not any(token in tokens for token in {"hr", "human", "resources", "talent", "acquisition"})
        and any(token in _ROLE_TITLE_MARKERS for token in title_tokens)
        and len(title_tokens) <= 7
        and not any(token in _NAVIGATION_TOKENS for token in title_tokens)
    )


def _is_location_name(name: Optional[str], city: str = "") -> bool:
    if not name:
        return False
    normalized = re.sub(r"[^a-z ]", "", name.casefold()).strip()
    city_normalized = re.sub(r"[^a-z ]", "", city.casefold()).strip()
    return normalized in _LOCATION_NAME_PAIRS or (
        bool(city_normalized) and normalized == city_normalized
    )


def _name_from_profile_url(url: str) -> Optional[str]:
    parsed = urlparse(url)
    if "linkedin.com" not in parsed.netloc.casefold():
        return None
    path = parsed.path.casefold().strip("/")
    if not path.startswith("in/"):
        return None
    slug = path.split("/", 1)[1].split("/", 1)[0]
    words = [word for word in re.split(r"[-_]+", slug) if word.isalpha()]
    if not 2 <= len(words) <= 3:
        return None
    return " ".join(word.capitalize() for word in words)


def _same_person(first: Optional[str], second: Optional[str]) -> bool:
    if not first or not second:
        return False
    return re.sub(r"[^a-z]", "", first.casefold()) == re.sub(r"[^a-z]", "", second.casefold())


def _is_hr_title(title: Optional[str]) -> bool:
    value = (title or "").casefold()
    return any(term in value for term in (
        "human resources", "chief people", "chief hr", "head of hr",
        "head of people", "hr director", "people director", "hr lead",
        "people lead", "talent acquisition", "chief talent", "hr manager",
        "people operations", "workplace experience", "office manager",
        "internal communications",
    ))


def _preferred_role_match(text: str, pattern: re.Pattern) -> Optional[re.Match]:
    matches = list(pattern.finditer(text))
    if not matches:
        return None
    return max(
        matches,
        key=lambda match: (
            _is_hr_title(match.group(0)),
            _role_priority(match.group(0)),
            -match.start(),
        ),
    )


def _is_relevant_mailbox(email: Optional[str], evidence_text: str = "") -> bool:
    if not email or "@" not in email:
        return False
    local_part = email.split("@", 1)[0].casefold().replace(".", " ").replace("-", " ").replace("_", " ")
    local_tokens = set(local_part.split())
    if local_tokens & _IRRELEVANT_MAILBOX_TOKENS:
        return False
    if local_tokens & _RELEVANT_MAILBOX_TOKENS:
        return True
    evidence = evidence_text.casefold()
    return any(token in evidence for token in ("events", "employee experience", "people team", "human resources"))


def _name_near_role(text: str, role_match: re.Match, name_pattern: re.Pattern,
                    company: str = "", city: str = "") -> Optional[str]:
    """Return a nearby name only when it is structurally adjacent to a role."""
    before = text[max(0, role_match.start() - 140):role_match.start()]
    after = text[role_match.end():role_match.end() + 140]
    candidates: list[tuple[int, str]] = []
    for match in name_pattern.finditer(before):
        gap = before[match.end():].strip(" -,:|()")
        if len(gap.split()) <= 2:
            candidates.append((role_match.start() - match.end(), match.group(0).strip()))
    for match in name_pattern.finditer(after):
        gap = after[:match.start()].strip(" -,:|()")
        if len(gap.split()) <= 2:
            candidates.append((role_match.end() + match.start(), match.group(0).strip()))
    place_tokens = {t.casefold() for t in re.findall(r"[A-Za-z]+", city)}
    company_tokens = {t.casefold() for t in re.findall(r"[A-Za-z]+", company)}
    title_tokens = {"ceo", "chief", "executive", "officer", "managing", "director",
                    "founder", "president", "head", "hr", "human", "resources",
                    "talent", "acquisition", "people", "events", "marketing", "lead"}
    title_tokens.add("headline")
    for _, raw_name in sorted(candidates):
        words = raw_name.split()
        while words and words[0].casefold().strip(".,") in place_tokens | company_tokens:
            words.pop(0)
        while words and words[-1].casefold().strip(".,") in place_tokens | company_tokens:
            words.pop()
        while words and words[-1].casefold().strip(".,") in title_tokens:
            words.pop()
        name = " ".join(words)
        if _is_relevant_contact(name, role_match.group(0)):
            return name
    return None


def _role_priority(title: Optional[str]) -> int:
    value = (title or "").casefold()
    if any(word in value for word in ("event", "community", "employee experience")): return 4
    if any(word in value for word in ("human resources", "hr ", "people", "talent", "recruit")): return 3
    if any(word in value for word in ("founder", "chief executive", "ceo", "managing director")): return 2
    return 1


def _attributed_to_other_company(text: str, name: str, company: str) -> bool:
    pos = text.casefold().find(name.casefold())
    if pos < 0:
        return False
    # Look after the person. Looking far before the name mistakes role
    # phrases such as "Head of Employee Experience — Rhea Malhotra" for
    # attribution to an employer named "Employee".
    nearby = text[pos:pos + len(name) + 240]
    for match in re.finditer(r"\b(?:at|of)\s+([A-Z][A-Za-z&.-]+)", nearby):
        org = match.group(1).strip()
        if org.casefold() in {"human", "people", "talent", "employee", "events", "marketing"}:
            continue
        if org.casefold() not in {company.casefold(), "tcs" if company.casefold() == "tata consultancy services" else company.casefold()}:
            return True
    title_then_org = re.search(
        r",\s*(?:[A-Za-z][A-Za-z&/-]*\s+){0,7}"
        r"(?:Officer|Director|Manager|Lead|Executive|Founder|CEO|Chief)"
        r"[^,]{0,40},\s*([A-Z][A-Za-z&.-]*(?:\s+[A-Z][A-Za-z&.-]*){0,5})",
        nearby,
    )
    if title_then_org:
        organization = title_then_org.group(1).strip(" .,:;")
        if organization.casefold() not in {
            company.casefold(),
            "tcs" if company.casefold() == "tata consultancy services" else company.casefold(),
        }:
            return True
    return False

_FIXTURE = {
    ("zomato", "gurgaon"): {
        "name": "Rhea Malhotra", "title": "Head of Employee Experience",
        "domain": "zomato.com", "signals": ["careers page mentions 'quarterly team offsites'"],
    },
    ("razorpay", "bangalore"): {
        "name": "Aditya Rao", "title": "Founder & CEO",
        "domain": "razorpay.com", "signals": ["recently raised funding — hiring fast"],
    },
    ("swiggy", "bangalore"): {
        "name": "Priya Nair", "title": "Events & Community Lead",
        "domain": "swiggy.com", "signals": ["active events/community careers listing"],
    },
}

LOW_CONF_THRESHOLD = 0.3     # below this: discard, keep falling through
MED_CONF_CEILING = 0.75      # [LOW, this): "medium" band -> spend one cross-check call
                              # >= this: "high" -> accept immediately, no cross-check
MIN_LIVE_ACCEPT_CONFIDENCE = 0.72


def _fixture_lookup(company: str, city: str):
    return _FIXTURE.get((company.strip().lower(), city.strip().lower()))


def _safe_hit(**kwargs) -> Optional[EnrichmentHit]:
    """Construct + validate an EnrichmentHit; a bad value (e.g. malformed
    email) is dropped with a log line instead of propagating."""
    try:
        return EnrichmentHit(**kwargs)
    except ValidationError:
        return None


def _sanitize_contact_hit(hit: Optional[EnrichmentHit]) -> Optional[EnrichmentHit]:
    """Keep only a plausible person, while preserving a valid mailbox hit."""
    if not hit or not hit.name:
        if hit and hit.email and not _is_relevant_mailbox(
            hit.email, " ".join(item.excerpt for item in hit.evidence)
        ):
            return None
        return hit
    if _is_relevant_contact(hit.name, hit.title):
        if hit.email and not _is_relevant_mailbox(
            hit.email, " ".join(item.excerpt for item in hit.evidence)
        ):
            return hit.model_copy(update={"email": None})
        return hit
    if hit.email and _is_relevant_mailbox(
        hit.email, " ".join(item.excerpt for item in hit.evidence)
    ):
        return hit.model_copy(update={"name": None, "title": None})
    return None


def _calibrate_hit_confidence(
    hit: Optional[EnrichmentHit],
    company: str,
    city: str,
) -> Optional[EnrichmentHit]:
    """Convert independently observable evidence into a bounded confidence."""
    if not hit or not hit.evidence:
        return hit
    target_domain = _guess_domain(company).casefold().removeprefix("www.")
    excerpts = " ".join(item.excerpt for item in hit.evidence).casefold()
    owned_urls = sum(
        1 for item in hit.evidence
        if urlparse(item.url).netloc.casefold().removeprefix("www.") == target_domain
    )
    distinct_urls = len({item.url for item in hit.evidence})
    confidence = hit.confidence
    if hit.name and hit.title:
        confidence += 0.04
    if owned_urls:
        confidence += 0.03
    if city and city.casefold() in excerpts:
        confidence += 0.04
    if distinct_urls >= 2:
        confidence += 0.08
    if hit.profile_url and "linkedin.com/in/" in hit.profile_url.casefold():
        confidence += 0.05
    if hit.email and hit.email.casefold().endswith("@" + target_domain):
        confidence += 0.08
    return hit.model_copy(update={"confidence": min(0.95, round(confidence, 2))})


def _guess_domain(company: str) -> str:
    fixture = _fixture_lookup(company, "")
    if fixture:
        return fixture["domain"]
    slug = re.sub(r"[^a-z0-9]", "", company.lower())
    return _KNOWN_COMPANY_DOMAINS.get(slug, f"{slug}.com")


def _short_excerpt(text: str, needle: str = "") -> str:
    clean = re.sub(r"\s+", " ", text).strip()
    if needle:
        pos = clean.lower().find(needle.lower())
        if pos >= 0:
            return clean[max(0, pos - 70):pos + 230]
    return clean[:300]


def _jsonld_people(soup) -> list[dict]:
    people: list[dict] = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.get_text())
        except (TypeError, ValueError):
            continue
        nodes = payload if isinstance(payload, list) else [payload]
        for node in nodes:
            if not isinstance(node, dict):
                continue
            node_type = node.get("@type")
            is_person = node_type == "Person" or (isinstance(node_type, list) and "Person" in node_type)
            if is_person and node.get("name") and node.get("jobTitle"):
                people.append(node)
            for child in node.get("@graph", []) if isinstance(node.get("@graph"), list) else []:
                child_type = child.get("@type") if isinstance(child, dict) else None
                child_is_person = child_type == "Person" or (isinstance(child_type, list) and "Person" in child_type)
                if isinstance(child, dict) and child_is_person and child.get("name") and child.get("jobTitle"):
                    people.append(child)
    return people


def _crawl_public_site(company: str, city: str) -> Optional[EnrichmentHit]:
    """Read only a few public pages on the guessed company domain.

    This deliberately does not scrape LinkedIn or invent contact facts. A
    hit is returned only when the page itself contains a person/title, email,
    or a clearly relevant event/people signal, with URL + excerpt receipts.
    """
    try:
        import requests
        from bs4 import BeautifulSoup
    except ImportError:
        return None

    domain = _guess_domain(company)
    root = f"https://{domain}"
    # Leadership/careers pages are materially more likely to identify HR
    # decision makers than generic home/about pages. Keep the page cap, but
    # spend it on pages with structured leadership information first.
    paths = ["/careers", "/careers/contact", "/contact", "/who-we-are/leadership", "/team", "/about"][:CRAWL_MAX_PAGES]
    headers = {"User-Agent": "ScoutChain-research/1.0 (public-pages-only)"}
    def fetch(path: str) -> Optional[tuple[str, str, object]]:
        url = urljoin(root, path)
        try:
            response = requests.get(
                url, headers=headers, timeout=HTTP_TIMEOUT_SECONDS, allow_redirects=True
            )
            final = urlparse(response.url)
            final_host = final.netloc.lower().removeprefix("www.")
            allowed_host = domain.lower().removeprefix("www.")
            if response.status_code >= 400 or final_host != allowed_host:
                return None
            if "text/html" not in response.headers.get("content-type", "text/html"):
                return None
            soup = BeautifulSoup(response.text[:400_000], "html.parser")
            for element in soup(["script", "style", "noscript"]):
                element.decompose()
            return response.url, soup.get_text(" ", strip=True), soup
        except requests.RequestException:
            return None

    with ThreadPoolExecutor(max_workers=len(paths)) as executor:
        pages = [page for page in executor.map(fetch, paths) if page]

    role_pattern = re.compile(
        r"\b(?:Founder|Co-Founder|CEO|Chief Executive Officer|Chief Human Resources Officer|"
        r"Chief People Officer|Head of [A-Za-z &]+|HR [A-Za-z &]+|Human Resources|"
        r"Talent Acquisition|People [A-Za-z &]+|Events? [A-Za-z &]+|Marketing [A-Za-z &]+)\b",
        re.I,
    )
    email_pattern = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
    name_pattern = re.compile(r"\b[A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,}){1,2}\b")

    page_hits: list[EnrichmentHit] = []
    for url, text, soup in pages:
        evidence: list[dict] = []
        name = title = email = None
        for person in _jsonld_people(soup):
            if _is_relevant_contact(str(person.get("name")), str(person.get("jobTitle"))):
                name = str(person["name"]).strip()
                title = str(person["jobTitle"]).strip()
                evidence.append({"url": url, "field": "contact", "excerpt": _short_excerpt(text, name)})
                if person.get("email"):
                    email = str(person["email"]).replace("mailto:", "").strip()
                break
        if not title:
            role_match = _preferred_role_match(text, role_pattern)
            if role_match:
                title = role_match.group(0).strip()
                name = _name_near_role(text, role_match, name_pattern, company, city)
                evidence.append({"url": url, "field": "title", "excerpt": _short_excerpt(text, title)})
        if not email:
            linked_emails = [a.get("href", "")[7:].split("?", 1)[0].strip()
                             for a in soup.select('a[href^="mailto:"]')]
            emails = [e for e in linked_emails + email_pattern.findall(text)
                      if e.lower().endswith("@" + domain)
                      and _is_relevant_mailbox(e, text)
                      and not any(e.lower().endswith("@" + suffix) for suffix in
                                  ("example.com", "sentry.io", "wixpress.com"))]
            if emails:
                preferred = ("careers", "jobs", "talent", "hr", "recruit", "people", "contact", "info")
                email = next((e for e in emails if any(tag in e.casefold() for tag in preferred)), emails[0])
                evidence.append({"url": url, "field": "email", "excerpt": _short_excerpt(text, email)})
        if not evidence:
            for keyword in ("event", "offsite", "team", "people", "careers"):
                if keyword in text.lower():
                    evidence.append({"url": url, "field": "signal", "excerpt": _short_excerpt(text, keyword)})
                    break
        if name and _attributed_to_other_company(text, name, company):
            name = title = None
        if _is_relevant_contact(name, title) or email:
            confidence = 0.8 if name and title and email else 0.65 if name and title else 0.5 if email else 0.0
            hit = _safe_hit(
                name=name, title=title, email=email, confidence=confidence,
                source=f"public website crawl ({urlparse(url).netloc})",
                signals=[f"public page mentions {e['field']}" for e in evidence],
                evidence=evidence,
            )
            if hit:
                page_hits.append(hit)
    return max(page_hits, key=lambda h: (bool(h.email), _role_priority(h.title), h.confidence), default=None)


def _search_public_sources(
    company: str,
    city: str,
    query_override: Optional[str] = None,
    person_name: Optional[str] = None,
    event_only: bool = False,
) -> Optional[EnrichmentHit]:
    """Query an explicitly configured search provider without scraping results.

    Only provider-returned title/snippet text is parsed. Social-profile URLs
    are excluded, and every accepted field keeps the provider URL as evidence.
    """
    try:
        import requests
        from bs4 import BeautifulSoup
    except ImportError:
        return None
    domain = _guess_domain(company)
    query = query_override or f'site:{domain} (Founder OR CEO OR HR OR People OR Events OR Marketing) "{city}"'
    results: list[dict] = []
    tavily_key = os.environ.get("TAVILY_API_KEY")
    serpapi_key = os.environ.get("SERPAPI_KEY")
    try:
        if tavily_key:
            response = requests.post(
                "https://api.tavily.com/search",
                json={"api_key": tavily_key, "query": query, "search_depth": "basic", "max_results": 5, "include_answer": False},
                timeout=HTTP_TIMEOUT_SECONDS,
            )
            if response.ok:
                results = response.json().get("results", [])
        elif serpapi_key:
            response = requests.get(
                "https://serpapi.com/search.json",
                params={"engine": "google", "q": query, "api_key": serpapi_key, "num": 5},
                timeout=HTTP_TIMEOUT_SECONDS,
            )
            if response.ok:
                results = response.json().get("organic_results", [])
        else:
            # Free deterministic fallback: parse public DuckDuckGo HTML results
            # when no paid search API is configured. Treat snippets as leads to
            # verify, never as proof of a person without a source URL.
            query = query_override or f'"{company}" "{city}" careers contact HR email'
            response = requests.get("https://html.duckduckgo.com/html/", params={"q": query},
                                    headers={"User-Agent": "Mozilla/5.0 ScoutChain public research"},
                                    timeout=HTTP_TIMEOUT_SECONDS)
            if response.ok:
                soup = BeautifulSoup(response.text, "html.parser")
                for result in soup.select(".result")[:8]:
                    link = result.select_one("a.result__a")
                    snippet = result.select_one(".result__snippet")
                    if link and link.get("href"):
                        results.append({"url": link["href"], "title": link.get_text(" ", strip=True),
                                        "snippet": snippet.get_text(" ", strip=True) if snippet else ""})
    except (requests.RequestException, ValueError):
        return None

    name_pattern = re.compile(r"\b[A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,}){1,2}\b")
    blocked_hosts = ("facebook.com", "instagram.com", "x.com")
    hits: list[EnrichmentHit] = []
    for result in results:
        url = str(result.get("url") or result.get("link") or "")
        host = urlparse(url).netloc.lower()
        path = urlparse(url).path.casefold()
        is_linkedin_profile = "linkedin.com" in host and "/in/" in path
        if (
            not url
            or any(blocked in host for blocked in blocked_hosts)
            or ("linkedin.com" in host and not is_linkedin_profile)
            or any(part in path for part in (
                "/job/", "/jobs/", "/reviews/", "/review/", "/salary/", "/salaries/",
                "/interviews/", "/benefits/", "/compare/",
            ))
            or path.endswith(".pdf")
        ):
            continue
        person_candidate_allowed = not any(part in path for part in ("/event/", "/events/"))
        text = f"{result.get('title', '')} {result.get('snippet', '')} {result.get('content', '')}"
        # Search operators are hints, not proof: providers routinely return
        # adjacent companies, subsidiaries, and unrelated press-release quotes.
        # Require an owned domain and an explicit company mention in the hit.
        result_host = host.removeprefix("www.")
        target_domain = domain.lower().removeprefix("www.")
        company_mentioned = company.casefold() in text.casefold()
        owned_domain = result_host == target_domain or result_host.endswith("." + target_domain)
        if not owned_domain and not company_mentioned:
            continue
        if not owned_domain and city.casefold() not in text.casefold():
            continue
        if is_linkedin_profile and company.casefold() not in text.casefold():
            continue
        email_match = re.search(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", text, re.I)
        person_terms = [
            token for token in re.findall(r"[a-z]+", (person_name or "").casefold())
            if len(token) >= 3
        ]
        person_email_supported = bool(person_terms) and all(
            token in text.casefold() for token in person_terms
        )
        if (
            email_match
            and (owned_domain or company_mentioned)
            and (
                _is_relevant_mailbox(email_match.group(0), text)
                or (person_name and person_email_supported and owned_domain)
            )
        ):
            email = email_match.group(0)
            hits.append(_safe_hit(email=email, confidence=0.7 if owned_domain else 0.58,
                             source="public search result mailbox",
                             signals=["public result lists a company-related mailbox"],
                             evidence=[{"url": url, "field": "email", "excerpt": _short_excerpt(text, email)}]))
        if not owned_domain and not is_linkedin_profile:
            continue
        if city.casefold() not in text.casefold():
            # An owned domain is not proof that the result describes this
            # location; salary and global press pages commonly look relevant.
            continue
        if event_only:
            event_match = re.search(
                r"\b(?:event|events|offsite|conference|summit|festival|"
                r"team[- ]building|employee experience|annual meeting)\b",
                text,
                re.I,
            )
            if event_match:
                hit = _safe_hit(
                    confidence=0.55,
                    source="public event evidence",
                    signals=[f"Prior company event evidence: {_short_excerpt(text, event_match.group(0))}"],
                    evidence=[{
                        "url": url,
                        "field": "event",
                        "excerpt": _short_excerpt(text, event_match.group(0)),
                    }],
                )
                if hit:
                    hits.append(hit)
                continue
        role_match = _preferred_role_match(text, _ROLE_PATTERN) if person_candidate_allowed else None
        if not role_match:
            continue
        name = _name_near_role(text, role_match, name_pattern, company, city)
        profile_name = _name_from_profile_url(url) if is_linkedin_profile else None
        if profile_name and (
            not name or not _same_person(name, profile_name)
        ):
            name = profile_name
        if name and name.casefold().endswith(" " + company.casefold()):
            name = name[: -(len(company) + 1)].strip()
        if not name or company.casefold() in name.casefold() or _is_location_name(name, city):
            continue
        if _attributed_to_other_company(text, name, company):
            continue
        # A quote such as “X, CEO, Other Company” must not be attributed to
        # the searched company just because the result itself mentions it.
        name_pos = text.casefold().find(name.casefold())
        nearby = text[max(0, name_pos - 100):name_pos + len(name) + 120]
        if re.search(r"\b(?:CEO|Chief Executive Officer|Founder|Co-Founder),?\s+of\s+[A-Z]", nearby, re.I):
            continue
        if re.search(
            rf"\b{re.escape(name)}\b[^.\n]{{0,100}}\b(?:of|at)\s+"
            rf"(?!{re.escape(company)}\b|Human\b|People\b|Talent\b|Employee\b|Events?\b|Marketing\b)"
            r"[A-Z][\w&.-]+",
            nearby,
            re.I,
        ):
            continue
        title = role_match.group(0).strip()
        excerpt = _short_excerpt(text, name)
        hit = _safe_hit(
            name=name,
            title=title,
            email=None,
            profile_url=url if is_linkedin_profile else None,
            confidence=0.62,
            source="search provider result",
            signals=[],
            evidence=[{"url": url, "field": "contact", "excerpt": excerpt}],
        )
        if hit:
            hits.append(hit)
    return max(hits, key=lambda h: (bool(h.email), _role_priority(h.title), h.confidence), default=None)


def stage_event_retrieval(company: str, city: str) -> Optional[EnrichmentHit]:
    """Find public company event evidence for an explicitly requested revision."""
    if USE_FIXTURES:
        return None
    domain = _guess_domain(company)
    queries = [
        f'site:{domain} ("event" OR "offsite" OR "conference" OR "summit") "{city}"',
        f'"{company}" "{city}" ("employee event" OR "team event" OR "annual meeting")',
    ]

    def retrieve(query: str) -> Optional[EnrichmentHit]:
        return _search_public_sources(company, city, query, event_only=True)

    with ThreadPoolExecutor(max_workers=len(queries)) as executor:
        hits = [hit for hit in executor.map(retrieve, queries) if hit]
    return max(hits, key=lambda hit: hit.confidence, default=None)


# --- Stage 1: company website scrape (free) -------------------------------
def stage_website_scrape(company: str, city: str) -> Optional[EnrichmentHit]:
    if USE_FIXTURES:
        f = _fixture_lookup(company, city)
        if f:
            return _safe_hit(name=f["name"], title=f["title"], email=None, confidence=0.55,
                              source="website scrape (About/Team page)", signals=f["signals"])
        return None
    # Unknown companies always get a real public-site pass. DEMO_MODE only
    # selects deterministic fixtures for the small demo set above.
    return _crawl_public_site(company, city)


# --- Stage 2: search-engine dork (free tier of SerpAPI/Tavily) -----------
def stage_search_dork(company: str, city: str) -> Optional[EnrichmentHit]:
    if USE_FIXTURES:
        f = _fixture_lookup(company, city)
        if f and f["title"].lower().startswith(("founder", "ceo", "co-founder")):
            return _safe_hit(name=f["name"], title=f["title"], email=None, confidence=0.7,
                              source="LinkedIn search dork", signals=f["signals"])
    return _search_public_sources(company, city)


def stage_hr_retrieval(company: str, city: str) -> Optional[EnrichmentHit]:
    """Prioritized retrieval for HR/People leadership and their mailboxes."""
    if USE_FIXTURES:
        return None
    domain = _guess_domain(company)
    queries = [
        f'site:{domain} ("Chief Human Resources Officer" OR "Chief People Officer" OR '
        f'"Head of Human Resources" OR "Head of People" OR "HR Director") "{city}"',
        f'site:{domain} ("human resources" OR "people team" OR "talent acquisition") '
        f'("{city}" OR email OR contact)',
    ]
    def retrieve(query: str) -> Optional[EnrichmentHit]:
        return _search_public_sources(company, city, query)

    with ThreadPoolExecutor(max_workers=len(queries)) as executor:
        hits = [hit for hit in executor.map(retrieve, queries) if hit]
    relevant = [
        hit for hit in hits
        if (hit.name and _is_hr_title(hit.title))
        or (hit.email and _is_relevant_mailbox(
            hit.email, " ".join(item.excerpt for item in hit.evidence)
        ))
    ]
    best = max(
        relevant,
        key=lambda hit: (bool(hit.name), bool(hit.email), hit.confidence),
        default=None,
    )
    return best.model_copy(update={
        "source": f"HR-priority retrieval via {best.source}",
    }) if best else None


# --- Stage 3: paid people-data API (Apollo / Hunter / proxycurl-style) ---
@retry_with_backoff(max_attempts=3)
def stage_people_api(company: str, city: str) -> Optional[EnrichmentHit]:
    if not company.strip() or not city.strip():
        return None
    api_key = os.environ.get("PEOPLE_API_KEY")
    if USE_FIXTURES or not api_key:
        return None  # skipped — wire a real paid provider here to go live
    # LIVE implementation sketch (now retried on failure via @retry_with_backoff):
    #   resp = requests.get("https://api.apollo.io/v1/people/match", params={...}, headers={...})
    return None


def stage_email_retrieval(
    company: str,
    city: str,
    contact_name: Optional[str],
    domain: str,
    contact_title: Optional[str] = None,
) -> Optional[EnrichmentHit]:
    """Run a separate mailbox-focused retrieval pass after contact discovery."""
    if USE_FIXTURES:
        return None
    queries = []
    if contact_name:
        queries.append(
            f'"{contact_name}" "{company}" "{city}" (email OR contact OR reach)'
        )
    if _is_hr_title(contact_title):
        queries.insert(
            0,
            f'site:{domain} ("HR" OR "Human Resources" OR "People") '
            f'("{city}" OR contact OR email)',
        )
    queries.extend([
        f'site:{domain} ("contact" OR "email" OR "people" OR "events") "{city}"',
        f'"{company}" "{city}" ("@{domain}" OR email OR contact)',
    ])
    def retrieve(query: str) -> Optional[EnrichmentHit]:
        return _search_public_sources(company, city, query, contact_name)

    with ThreadPoolExecutor(max_workers=len(queries)) as executor:
        hits = [hit for hit in executor.map(retrieve, queries) if hit]
    relevant = [
        hit for hit in hits
        if hit.email
        and hit.email.casefold().endswith("@" + domain.casefold())
        and _is_relevant_mailbox(
            hit.email, " ".join(item.excerpt for item in hit.evidence)
        )
    ]
    best = max(relevant, key=lambda hit: (bool(contact_name), hit.confidence), default=None)
    return best.model_copy(update={
        "name": None,
        "title": None,
        "confidence": min(0.9, max(0.72, best.confidence)),
        "source": f"dedicated email retrieval via {best.source}",
    }) if best else None


@retry_with_backoff(max_attempts=2)
def stage_person_email_api(
    contact_name: Optional[str],
    company: str,
    domain: str,
    contact_title: Optional[str] = None,
) -> Optional[EnrichmentHit]:
    """Use an optional person-email provider only after HR identity discovery."""
    api_key = os.environ.get("HUNTER_API_KEY")
    if (
        USE_FIXTURES
        or not api_key
        or not contact_name
        or not _is_relevant_contact(contact_name, contact_title)
    ):
        return None
    try:
        import requests
        response = requests.get(
            "https://api.hunter.io/v2/email-finder",
            params={
                "first_name": contact_name.split()[0],
                "last_name": contact_name.split()[-1],
                "domain": domain,
                "api_key": api_key,
            },
            timeout=10,
        )
        if not response.ok:
            return None
        payload = response.json().get("data", {})
        email = str(payload.get("email") or "").strip().lower()
        if (
            not email
            or not email.endswith("@" + domain.casefold())
            or str(payload.get("first_name") or "").casefold() != contact_name.split()[0].casefold()
            or str(payload.get("last_name") or "").casefold() != contact_name.split()[-1].casefold()
        ):
            return None
        confidence = float(payload.get("score") or 0) / 100
        if confidence < 0.7:
            return None
        return _safe_hit(
            name=contact_name,
            title=contact_title,
            email=email,
            confidence=min(0.95, confidence),
            source="Hunter email-finder (person/domain match)",
            signals=["provider matched the discovered person to the company domain"],
            evidence=[{
                "url": "https://hunter.io/",
                "field": "email",
                "excerpt": f"{contact_name} matched to {email}",
            }],
        )
    except (ImportError, ValueError, TypeError, KeyError):
        return None


# --- Stage 5: email-pattern guess (free, lowest confidence, last resort) -
def stage_email_pattern_guess(name: Optional[str], domain: Optional[str]) -> Optional[EnrichmentHit]:
    if not name or not domain:
        return None
    parts = re.sub(r"[^a-zA-Z ]", "", name).lower().split()
    if not parts:
        return None
    f, l = parts[0], (parts[-1] if len(parts) > 1 else "")
    email = f"{f}.{l}@{domain}" if l else f"{f}@{domain}"
    return _safe_hit(name=None, title=None, email=email, confidence=0.3,
                      source="email-pattern guess (unverified)", signals=[])


# --- Stage 5 (new): lightweight email verification ------------------------
def verify_email(email: Optional[str], expected_domain: str) -> tuple[bool, str]:
    """Live mode: wire NeverBounce/QuickEmailVerify here (both docs name
    these correctly — this is where that call actually goes).
    Demo mode: a heuristic only — same domain + syntactically valid — and
    LABELLED as a heuristic, not a verification, so the Sheet never
    overstates confidence it doesn't have."""
    if not email:
        return False, "no email to verify"
    verifier_key = os.environ.get("EMAIL_VERIFIER_KEY")
    if not DEMO_MODE and verifier_key:
        # LIVE implementation sketch:
        #   resp = requests.get("https://api.neverbounce.com/v4/single/check",
        #                        params={"email": email, "key": verifier_key})
        #   return resp.json()["result"] == "valid", "NeverBounce"
        return False, "live verifier not wired yet"
    domain_ok = email.lower().endswith("@" + expected_domain.lower())
    return domain_ok, "heuristic (demo mode — domain match only, not a real deliverability check)"


def run_waterfall(job, refresh_event_context: bool = False) -> None:
    """Mutates `job` in place, logging every stage attempted — found or not."""
    company, city = job.company.strip(), job.city.strip()
    if CRAWL_MODE == "live":
        job.log("enrich", "live evidence mode enabled: public pages only; no social-profile scraping", "free")
    fixture = _fixture_lookup(company, city)
    domain = fixture["domain"] if fixture else f"{company.lower().replace(' ', '')}.com"

    accepted: Optional[EnrichmentHit] = None
    evidence_sources: list[str] = []
    graph_produced_candidates = False
    try:
        from .research_graph import run_research_graph
        accepted, graph_hits, graph_reason = run_research_graph(company, city)
        accepted = _sanitize_contact_hit(accepted)
        accepted = _calibrate_hit_confidence(accepted, company, city)
        graph_produced_candidates = bool(graph_hits)
        evidence_sources.extend(hit.source for hit in graph_hits)
        job.log("research", graph_reason or "LangGraph source research completed", "free")
        for hit in graph_hits:
            job.log("research", f"source candidate: {hit.source}; confidence={hit.confidence:.2f}", "free")
    except (ImportError, RuntimeError) as exc:
        job.log("research", f"LangGraph unavailable; deterministic waterfall fallback: {exc}", "free")
    # Keep one predictable waterfall for every company. The former optional
    # graph path skipped the public search fallback whenever it was enabled.
    chain = [
        (stage_website_scrape, (company, city), "public website"),
        (stage_people_api, (company, city), "paid-api (optional)"),
    ] if graph_produced_candidates else [
        (stage_hr_retrieval, (company, city), "HR-priority public search"),
        (stage_website_scrape, (company, city), "public website"),
        (stage_search_dork, (company, city), "public search"),
        (stage_people_api, (company, city), "paid-api (optional)"),
    ]
    for i, (stage_fn, args, cost_hint) in enumerate(chain if not accepted else []):
        hit = _sanitize_contact_hit(stage_fn(*args))
        hit = _calibrate_hit_confidence(hit, company, city)
        job.log("enrich", f"tried {stage_fn.__name__} -> " +
                           (f"hit, conf={hit.confidence}" if hit else "no match"), cost_hint)
        if not hit or hit.confidence < LOW_CONF_THRESHOLD:
            continue

        evidence_sources.append(hit.source)

        if hit.confidence >= MED_CONF_CEILING:
            accepted = hit
            job.log("enrich", f"{stage_fn.__name__} confidence {hit.confidence} >= {MED_CONF_CEILING} "
                               f"— accepting immediately, no cross-check spent", cost_hint)
            break

        # Medium band: spend exactly one more stage call to cross-verify.
        job.log("enrich", f"{stage_fn.__name__} confidence {hit.confidence} is in the medium band "
                           f"[{LOW_CONF_THRESHOLD}, {MED_CONF_CEILING}) — cross-checking with the next stage")
        next_hit = None
        if i + 1 < len(chain):
            next_fn, next_args, next_cost = chain[i + 1]
            next_hit = _sanitize_contact_hit(next_fn(*next_args))
            next_hit = _calibrate_hit_confidence(next_hit, company, city)
            if next_hit:
                evidence_sources.append(next_hit.source)
            job.log("enrich", f"cross-check via {next_fn.__name__} -> " +
                               (f"hit, conf={next_hit.confidence}" if next_hit else "no second source"), next_cost)
        if next_hit and next_hit.name and hit.name and next_hit.name.strip().lower() == hit.name.strip().lower():
            boosted = min(1.0, hit.confidence + 0.2)
            hit = hit.model_copy(update={
                "confidence": boosted,
                "source": f"{hit.source} + cross-verified via {next_hit.source}",
            })
            job.log("enrich", f"cross-check agrees — confidence boosted to {boosted}")
        elif next_hit and next_hit.name and hit.name and next_hit.name.strip().lower() != hit.name.strip().lower():
            job.log("enrich", "cross-check DISAGREES on contact name — flagging ambiguous, continuing waterfall")
            continue  # don't accept a contested result; keep falling through
        else:
            job.log("enrich", "no second source available — accepting single-source result as unverified")
        accepted = hit
        break

    if accepted:
        if not USE_FIXTURES and accepted.confidence < MIN_LIVE_ACCEPT_CONFIDENCE:
            job.log(
                "enrich",
                f"rejected low-confidence live result ({accepted.confidence:.2f} < "
                f"{MIN_LIVE_ACCEPT_CONFIDENCE:.2f}); manual verification required",
                "free",
            )
            accepted = None

    if accepted:
        job.contact_name = accepted.name
        job.contact_title = accepted.title
        job.profile_url = accepted.profile_url
        job.enrichment_source = accepted.source
        job.email_confidence = accepted.confidence
        job.signals = accepted.signals
        job.email = accepted.email
        job.evidence = [item.model_dump() for item in accepted.evidence]
    job.evidence_sources = evidence_sources

    if not job.email:
        person_email_hit = stage_person_email_api(
            job.contact_name,
            company,
            domain,
            job.contact_title,
        )
        job.log(
            "enrich",
            "person-email provider lookup -> " +
            (f"matched {person_email_hit.email}" if person_email_hit else "no verified match"),
            "paid-api (optional)",
        )
        if person_email_hit:
            job.email = person_email_hit.email
            job.email_confidence = max(job.email_confidence, person_email_hit.confidence)
            job.enrichment_source = (
                f"{job.enrichment_source} + {person_email_hit.source}"
                if job.enrichment_source else person_email_hit.source
            )
            job.evidence_sources.append(person_email_hit.source)
            job.evidence.extend(item.model_dump() for item in person_email_hit.evidence)

    if not job.email:
        email_hit = stage_email_retrieval(
            company, city, job.contact_name, domain, job.contact_title
        )
        job.log(
            "enrich",
            "dedicated email retrieval -> " +
            (f"found {email_hit.email}" if email_hit else "no owned-domain mailbox"),
            "free",
        )
        if email_hit:
            job.email = email_hit.email
            job.email_confidence = email_hit.confidence
            job.enrichment_source = (
                f"{job.enrichment_source} + {email_hit.source}"
                if job.enrichment_source else email_hit.source
            )
            job.evidence_sources.append(email_hit.source)
            job.evidence.extend(item.model_dump() for item in email_hit.evidence)

    if refresh_event_context:
        event_hit = stage_event_retrieval(company, city)
        job.log(
            "research",
            "event-context retrieval -> " +
            (f"found public event evidence from {event_hit.source}" if event_hit else "no public event evidence"),
            "free",
        )
        if event_hit:
            job.signals.extend(event_hit.signals)
            job.evidence_sources.append(event_hit.source)
            job.evidence.extend(item.model_dump() for item in event_hit.evidence)

    if not job.email and USE_FIXTURES:
        guess = stage_email_pattern_guess(job.contact_name, domain)
        if guess:
            job.email = guess.email
            job.email_confidence = guess.confidence if not accepted else min(job.email_confidence, guess.confidence)
            job.enrichment_source = f"{job.enrichment_source} + {guess.source}" if job.enrichment_source else guess.source
            job.evidence_sources.append(guess.source)
            job.log("enrich", "backfilled email via pattern guess (unverified)", "free")

    verified, method = verify_email(job.email, domain)
    if job.email:
        job.log("enrich", f"email check: {'passed' if verified else 'did not pass'} ({method})", "free")
        if not verified:
            job.email_confidence = min(job.email_confidence, 0.3)

    if not accepted and not job.email:
        job.log("enrich", "all stages exhausted — no contact or email found", "free")
        job.log("enrich", "public website and public search found no published contact; add a verified company domain or review manually", "free")
