# Screen recording script (aim for 3–5 minutes)

> v2 note: the trace now also shows a security checkpoint (blacklist +
> 7-day cooldown) before enrichment starts, and the waterfall sometimes
> spends one extra "cross-check" call when a source's confidence is
> medium (0.3–0.75) rather than high. Run `python demo.py "Razorpay"
> "Bangalore"` to see a cross-check that boosts confidence, and
> `python demo.py "Zomato" "Gurgaon"` to see one that honestly reports
> "no second source available" instead of pretending to verify.

Goal: prove the workflow runs end-to-end, and that you understand *why*
it's built the way it is — not just that it runs.

## 0. Before you hit record
- `rm -rf data` so the recording starts from a clean slate.
- Have `README.md`'s architecture diagram open in one tab (renders on
  GitHub, or paste the mermaid block into https://mermaid.live).
- Have a terminal ready in `gopratle-bdr-agent/`.

## 1. (30s) The trigger — show BOTH surfaces exist
> "This agent has two triggers that feed one pipeline: a Google Sheet row,
> or a Telegram command. I'll show the Sheet one first."
```bash
python demo.py "Zomato" "Gurgaon"
```
Let it run. Narrate as it prints:
> "Trigger: Company + City. Now it's running the waterfall enrichment —
> watch it try the free website scrape first, and only fall through to
> the next stage if that doesn't return a confident match."

## 2. (45s) Point at the enrichment trace lines
> "Each line here is one stage of the waterfall — free sources first, paid
> API only if needed. This is the same cost-ordering pattern Clay.com uses
> in production GTM tooling — no single source covers everyone, so you
> chain them and stop at the first confident hit."

## 3. (45s) Point at the critique round
> "Here's the Producer's draft pitch, and here's a *separate* Critic call
> reviewing it against a rubric before it ships. If the critic flags an
> issue, the Producer revises — capped at 2 rounds so it can't loop
> forever on our LLM bill. This is the Reflexion / Self-Refine pattern —
> having the same model grade its own writing is a known bias; a separate
> critic catches more."

## 4. (30s) The approval gate
> "Nothing gets sent until a human approves — in the CLI demo I'm
> auto-approving for speed, but in the Telegram version this is a literal
> /approve command, and in the n8n version it's an inline Telegram
> button. No lead gets messaged without a person saying go."

## 5. (30s) Show the Sheet + audit log
```bash
cat data/leads_sheet.csv
cat data/run_log.csv
```
> "Every run writes back to the sheet, and every single step — including
> the ones that didn't find anything — is logged here. If a lead looks
> wrong three weeks from now, this answers 'what did the agent try and
> why' without re-running anything."

## 6. (30s) Run the Telegram trigger to prove it's the SAME pipeline
```bash
python demo.py --telegram "Find event leads for Razorpay in Bangalore"
```
> "Different trigger, same orchestrator, same waterfall, same critic. This
> isn't two scripts that happen to look similar — it's one system with
> two front doors."

## 7. (20s) Close on the architecture diagram
Show the mermaid diagram from the README.
> "That's the whole thing: waterfall enrichment, explainable scoring,
> producer-critic pitch writing, a human approval gate, and a follow-up
> loop that remembers what worked. It runs with zero API keys for this
> demo, and upgrades to live Claude + real enrichment APIs by setting
> three env vars — no architecture changes needed."

## What to attach alongside the recording
- This whole repo / GitHub link.
- `n8n/gopratle_bdr_agent.n8n.json` (mention it's a no-code mirror of the
  same architecture, for teams that want to hand this to ops rather than
  engineering).
- A one-line note: "Demo mode uses an offline fixture + mock LLM so this
  runs with zero API cost/setup; flipping to live Claude + a real search
  API is 3 env vars, documented in the README."
