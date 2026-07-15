"""Unit tests for web/polish.py: prompt assembly and the single-flight cache
on Job.get_or_create_polished_report (jobs.py)."""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from web.jobs import Job
from web.polish import _build_polish_prompt, _collect_sections, generate_polished_report

_SAMPLE_DATA = {
    "ticker": "AAPL",
    "company_of_interest": "AAPL",
    "trade_date": "2026-06-01",
    "market_report": "Market is bullish.",
    "sentiment_report": "Sentiment is mixed.",
    "news_report": "",
    "fundamentals_report": "Solid fundamentals.",
    "trader_investment_plan": "**Action**: Hold",
    "final_trade_decision": "**Rating**: Hold",
    "investment_debate_state": {
        "bull_history": "Bull: strong case",
        "bear_history": "Bear: risky",
        "judge_decision": "**Recommendation**: Hold",
    },
    "risk_debate_state": {
        "aggressive_history": "Aggressive: go big",
        "conservative_history": "Conservative: be careful",
        "neutral_history": "",
        "judge_decision": "**Rating**: Hold",
    },
}


@pytest.mark.unit
class TestCollectSections:
    def test_includes_non_empty_sections_only(self):
        sections = dict(_collect_sections(_SAMPLE_DATA))
        assert sections["Technical & Market Analysis"] == "Market is bullish."
        assert sections["Bull Case"] == "Bull: strong case"
        # news_report was empty — still present as a key, just falsy content
        assert not sections["News & Macro Analysis"]

    def test_risk_transcript_joins_present_histories_only(self):
        sections = dict(_collect_sections(_SAMPLE_DATA))
        transcript = sections["Risk Team Debate"]
        assert "Aggressive: go big" in transcript
        assert "Conservative: be careful" in transcript


@pytest.mark.unit
class TestBuildPolishPrompt:
    def test_prompt_includes_ticker_and_date(self):
        prompt = _build_polish_prompt(_SAMPLE_DATA, "English")
        assert "AAPL" in prompt
        assert "2026-06-01" in prompt

    def test_prompt_omits_empty_sections(self):
        prompt = _build_polish_prompt(_SAMPLE_DATA, "English")
        assert "## News & Macro Analysis" not in prompt
        assert "## Technical & Market Analysis" in prompt

    def test_english_adds_no_language_directive(self):
        prompt = _build_polish_prompt(_SAMPLE_DATA, "English")
        assert "Write the entire report in" not in prompt

    def test_non_english_adds_language_directive_with_proper_noun_carveout(self):
        prompt = _build_polish_prompt(_SAMPLE_DATA, "Chinese")
        assert "Write the entire report in Chinese" in prompt
        assert "MACD" in prompt  # proper-noun carve-out example terms present


@pytest.mark.unit
class TestGeneratePolishedReport:
    def test_uses_job_config_provider_and_model(self):
        job = Job(id="1", ticker="AAPL", trade_date="2026-06-01")
        job.config = {"llm_provider": "deepseek", "deep_think_llm": "deepseek-chat"}
        job.result = _SAMPLE_DATA

        fake_llm = MagicMock()
        fake_llm.invoke.return_value = MagicMock(content="# Polished Report\n...")
        fake_client = MagicMock()
        fake_client.get_llm.return_value = fake_llm

        with patch("web.polish.create_llm_client", return_value=fake_client) as create_client:
            result = generate_polished_report(job)

        create_client.assert_called_once_with(
            provider="deepseek", model="deepseek-chat", base_url=None,
        )
        assert result == "# Polished Report\n..."


@pytest.mark.unit
class TestPolishSingleFlight:
    def test_second_call_reuses_cached_result_without_recomputing(self):
        job = Job(id="1", ticker="AAPL", trade_date="2026-06-01")
        calls = {"n": 0}

        def compute():
            calls["n"] += 1
            return "polished text"

        first = job.get_or_create_polished_report(compute)
        second = job.get_or_create_polished_report(compute)
        assert first == second == "polished text"
        assert calls["n"] == 1

    def test_concurrent_calls_single_flight_to_one_computation(self):
        job = Job(id="1", ticker="AAPL", trade_date="2026-06-01")
        calls = {"n": 0}
        release = threading.Event()

        def slow_compute():
            calls["n"] += 1
            release.wait(timeout=2.0)
            return "polished text"

        results = []

        def worker():
            results.append(job.get_or_create_polished_report(slow_compute))

        threads = [threading.Thread(target=worker) for _ in range(3)]
        for t in threads:
            t.start()
        time.sleep(0.05)  # let all three threads reach the lock
        release.set()
        for t in threads:
            t.join(timeout=2.0)

        assert calls["n"] == 1
        assert results == ["polished text"] * 3
