"""AI polish pass: synthesizes a completed run's independently-written agent
reports into one cohesive, professionally edited report for PDF export.

Runs as a single extra LLM call against the same provider/model the analysis
itself used (``job.config``) — no new provider wiring, no structured-output
schema; this is a plain long-form rewrite task.
"""

from __future__ import annotations

from tradingagents.llm_clients import create_llm_client


def _collect_sections(data: dict) -> list[tuple[str, str]]:
    debate = data.get("investment_debate_state") or {}
    risk = data.get("risk_debate_state") or {}
    risk_transcript = "\n\n".join(
        filter(None, [
            risk.get("aggressive_history"),
            risk.get("conservative_history"),
            risk.get("neutral_history"),
        ])
    )
    return [
        ("Technical & Market Analysis", data.get("market_report")),
        ("Sentiment Analysis", data.get("sentiment_report")),
        ("News & Macro Analysis", data.get("news_report")),
        ("Fundamentals Analysis", data.get("fundamentals_report")),
        ("Bull Case", debate.get("bull_history")),
        ("Bear Case", debate.get("bear_history")),
        ("Research Manager Decision", debate.get("judge_decision")),
        ("Trader's Plan", data.get("trader_investment_plan")),
        ("Risk Team Debate", risk_transcript),
        ("Portfolio Manager's Final Decision", data.get("final_trade_decision")),
    ]


def _build_polish_prompt(data: dict, output_language: str) -> str:
    body = "\n\n".join(f"## {title}\n{text}" for title, text in _collect_sections(data) if text)
    ticker = data.get("company_of_interest") or data.get("ticker") or "the instrument"
    trade_date = data.get("trade_date", "")

    language_line = ""
    if output_language.strip().lower() != "english":
        language_line = (
            f"\n- Write the entire report in {output_language}, keeping tickers, "
            "company names, and technical/financial terms (e.g. MACD, RSI, EPS, P/E) "
            "in their original form rather than translating or transliterating them."
        )

    return f"""You are a professional financial editor. Below are the independently-written \
sections produced by a multi-agent trading analysis system for {ticker} on {trade_date}. \
Synthesize them into ONE cohesive, professionally written report.

Requirements:
- Organize with clear Markdown headers: Executive Summary, Technical Analysis, Sentiment \
Analysis, News & Macro Environment, Fundamentals, Bull vs. Bear Debate Summary, Risk \
Assessment, Final Recommendation.
- Remove redundancy across sections — do not repeat the same fact multiple times just \
because multiple agents mentioned it.
- Smooth over awkward phrasing and keep a single, consistent, professional tone throughout, \
as if one senior analyst had written the whole thing.
- Preserve every factual claim, number, price level, and figure exactly as given. Do not \
invent, drop, or alter any data point.
- Open with a 3-5 sentence Executive Summary stating the final recommendation and its core \
rationale.
- End with a clearly labeled Final Recommendation section (action, entry/stop/target prices \
and position sizing if available, time horizon).{language_line}

--- RAW SECTIONS ---

{body}
"""


def generate_polished_report(job) -> str:
    """Run the AI polish pass for a completed job. Blocking (single LLM call)."""
    config = job.config or {}
    client = create_llm_client(
        provider=config.get("llm_provider", "openai"),
        model=config.get("deep_think_llm", "gpt-5.5"),
        base_url=config.get("backend_url"),
    )
    llm = client.get_llm()
    prompt = _build_polish_prompt(job.result or {}, config.get("output_language", "English"))
    response = llm.invoke(prompt)
    return response.content
