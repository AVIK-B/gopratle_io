"""Evidence-first LangGraph retrieval network.

Retrieval is split into independent agents with different search intent:
leadership, people/event ownership, and contact/mailbox discovery. The graph
merges their URL-backed evidence before selecting anything, so one noisy
snippet cannot become a contact by itself.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Optional, TypedDict

from .schemas import EnrichmentHit


class ResearchState(TypedDict, total=False):
    company: str
    city: str
    website_hit: Optional[EnrichmentHit]
    search_hit: Optional[EnrichmentHit]
    search_hits: list[EnrichmentHit]
    query_plan: list[str]
    selected: Optional[EnrichmentHit]
    candidates: list[EnrichmentHit]
    source_documents: list[dict]
    score: float
    conflicts: list[str]
    quality: str
    buying_signals: list[str]
    reason: str


def _build_graph():
    from langgraph.graph import END, START, StateGraph
    from .enrichment import (
        USE_FIXTURES,
        _guess_domain,
        _search_public_sources,
        stage_search_dork,
        stage_website_scrape,
    )

    def plan(state: ResearchState) -> ResearchState:
        company = state["company"]
        city = state["city"]
        domain = _guess_domain(company)
        queries = [
            f'site:{domain} ("Chief Human Resources Officer" OR "Chief People Officer" OR '
            f'"Head of Human Resources" OR "Head of People" OR "HR Director") "{city}"',
            f'site:{domain} ("human resources" OR "people team" OR "talent acquisition") '
            f'("{city}" OR email OR contact)',
            f'site:linkedin.com/in/ ("HR" OR "Human Resources" OR "Chief People Officer" OR '
            f'"Head of People") "{company}" "{city}"',
            f'site:{domain} ("Events" OR "Employee Experience" OR "People") "{city}"',
            f'site:{domain} ("team" OR "leadership" OR "contact" OR "media") "{city}"',
        ]
        return {
            "query_plan": queries,
            "reason": f"parallel public-source research for {company} in {city}",
        }

    def crawl(state: ResearchState) -> ResearchState:
        return {"website_hit": stage_website_scrape(state["company"], state["city"])}

    def search(state: ResearchState) -> ResearchState:
        if USE_FIXTURES:
            hit = stage_search_dork(state["company"], state["city"])
            return {"search_hit": hit, "search_hits": [hit] if hit else []}

        def retrieve(query: str) -> Optional[EnrichmentHit]:
            return _search_public_sources(state["company"], state["city"], query)

        with ThreadPoolExecutor(
            max_workers=min(5, len(state.get("query_plan", [])))
        ) as executor:
            hits = [
                hit for hit in executor.map(retrieve, state.get("query_plan", []))
                if hit
            ]
        from .enrichment import _is_hr_title
        best = max(
            hits,
            key=lambda hit: (
                _is_hr_title(hit.title),
                bool(hit.name),
                bool(hit.email),
                hit.confidence,
            ),
            default=None,
        )
        return {"search_hit": best, "search_hits": hits}

    def verify(state: ResearchState) -> ResearchState:
        from .enrichment import (
            _calibrate_hit_confidence,
            _is_hr_title,
            _is_relevant_contact,
        )
        candidates = [
            hit for hit in (
                state.get("website_hit"),
                *state.get("search_hits", []),
            ) if hit
        ]
        # A provider may return the same URL in multiple query buckets.
        unique_candidates: dict[tuple[str, str, str], EnrichmentHit] = {}
        for hit in candidates:
            evidence_key = tuple(sorted(item.url for item in hit.evidence))
            key = (hit.name.casefold() if hit.name else "", hit.email or "", str(evidence_key))
            unique_candidates[key] = hit
        candidates = [
            _calibrate_hit_confidence(hit, state["company"], state["city"])
            for hit in unique_candidates.values()
        ]
        candidates = [hit for hit in candidates if hit]
        evidenced = [hit for hit in candidates if hit.evidence and
                     ((hit.name and _is_relevant_contact(hit.name, hit.title)) or hit.email)]
        # LangChain Documents give every receipt a consistent, inspectable
        # shape; deduplicate URLs while retaining field provenance.
        from langchain_core.documents import Document
        documents = [
            Document(page_content=item.excerpt, metadata={"url": item.url, "field": item.field})
            for hit in evidenced for item in hit.evidence
        ]
        unique: dict[tuple[str, str, str], Document] = {}
        for doc in documents:
            key = (doc.metadata.get("url", ""), doc.metadata.get("field", ""), doc.page_content)
            unique[key] = doc

        def score(hit: EnrichmentHit) -> float:
            # Explainable evidence score, independent of any language model.
            distinct_urls = len({e.url for e in hit.evidence})
            return (hit.confidence + (0.12 if hit.name else 0)
                    + (0.08 if _is_hr_title(hit.title) else 0)
                    + (0.08 if hit.title else 0) + (0.08 if hit.email else 0)
                    + min(0.16, 0.08 * distinct_urls))

        selected = max(evidenced, key=score, default=None)
        conflicts = []
        if selected and len(evidenced) > 1:
            same_name = [
                hit for hit in evidenced
                if hit.name and selected.name and hit.name.casefold() == selected.name.casefold()
            ]
            if len(same_name) > 1:
                merged_evidence = [
                    item for hit in same_name for item in hit.evidence
                ]
                selected = selected.model_copy(update={
                    "confidence": min(1.0, selected.confidence + min(0.2, 0.1 * (len(same_name) - 1))),
                    "source": f"{selected.source} + corroborated by {len(same_name) - 1} public source(s)",
                    "evidence": merged_evidence,
                })
            distinct_names = {hit.name.casefold() for hit in evidenced if hit.name}
            if len(distinct_names) > 1:
                conflicts.append("sources returned different people; selected result is uncorroborated")
                # Do not present a guessed winner when two sources disagree.
                selected = None
        signals = sorted({signal for hit in evidenced for signal in hit.signals
                          if signal and not signal.casefold().startswith(("public page mentions", "provider snippet"))})
        quality = ("person+mailbox" if selected and selected.name and selected.email
                   else "verified-person" if selected and selected.name
                   else "published-mailbox" if selected and selected.email else "no-verified-contact")
        return {"candidates": candidates, "selected": selected,
                "source_documents": [{"url": d.metadata["url"], "field": d.metadata["field"],
                                      "excerpt": d.page_content} for d in unique.values()],
                "score": score(selected) if selected else 0.0,
                "conflicts": conflicts, "quality": quality, "buying_signals": signals}

    def route_quality(state: ResearchState) -> str:
        # Route reviewed leads with evidence; otherwise end as a no-hit with
        # an explicit reason for the operator.
        return "ready" if state.get("selected") else "no_hit"

    def ready(state: ResearchState) -> ResearchState:
        return {"reason": f"{state.get('quality')} selected; evidence score={state.get('score', 0):.2f}"}

    def no_hit(state: ResearchState) -> ResearchState:
        reason = "No named relevant contact or published company mailbox passed evidence validation"
        if state.get("conflicts"):
            reason += "; " + "; ".join(state["conflicts"])
        return {"reason": reason}

    builder = StateGraph(ResearchState)
    builder.add_node("plan", plan)
    builder.add_node("crawl", crawl)
    builder.add_node("search", search)
    builder.add_node("verify", verify)
    builder.add_node("ready", ready)
    builder.add_node("no_hit", no_hit)
    builder.add_edge(START, "plan")
    builder.add_edge("plan", "crawl")
    builder.add_edge("plan", "search")
    builder.add_edge(["crawl", "search"], "verify")
    builder.add_conditional_edges("verify", route_quality, {"ready": "ready", "no_hit": "no_hit"})
    builder.add_edge("ready", END)
    builder.add_edge("no_hit", END)
    return builder.compile()


def run_research_graph(company: str, city: str) -> tuple[Optional[EnrichmentHit], list[EnrichmentHit], str]:
    """Run the LangGraph network, returning selected hit, candidates, reason."""
    try:
        graph = _build_graph()
        state = graph.invoke({"company": company, "city": city})
        return state.get("selected"), state.get("candidates", []), state.get("reason", "")
    except ImportError as exc:
        raise RuntimeError("LangGraph is not installed; use the sequential fallback or install requirements.txt") from exc
