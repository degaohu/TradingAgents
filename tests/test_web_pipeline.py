"""Unit tests for web/pipeline.py: stage-transition detection over graph.stream() chunks.

TradingAgentsGraph streams with stream_mode="values" (propagation.py), so
each chunk is the *complete* cumulative state so far — these tests build
chunks the same way (fields, once set, stay set) rather than emitting deltas.
"""

from __future__ import annotations

import pytest

from web.pipeline import (
    PORTFOLIO_MANAGER,
    RESEARCH_DEBATE,
    RESEARCH_MANAGER,
    RISK_DEBATE,
    TRADER,
    PipelineTracker,
    build_stage_specs,
)

_ANALYSTS = ("market", "social", "news", "fundamentals")


@pytest.mark.unit
class TestBuildStageSpecs:
    def test_includes_all_selected_analysts_and_fixed_stages(self):
        specs = build_stage_specs(_ANALYSTS)
        ids = [s["id"] for s in specs]
        assert ids == [
            "market", "social", "news", "fundamentals",
            RESEARCH_DEBATE, RESEARCH_MANAGER, TRADER, RISK_DEBATE, PORTFOLIO_MANAGER,
        ]

    def test_respects_analyst_subset_and_order(self):
        specs = build_stage_specs(("news", "market"))
        ids = [s["id"] for s in specs if s["group"] == "analysts"]
        assert ids == ["news", "market"]


class _EventLog:
    def __init__(self):
        self.events = []

    def __call__(self, stage_id, status, elapsed_s, reports):
        self.events.append((stage_id, status, elapsed_s, dict(reports)))


@pytest.mark.unit
class TestPipelineTrackerAnalysts:
    def test_all_analysts_start_running_on_construction(self):
        # All selected analysts run as parallel graph branches (setup.py
        # fans out from START to every one of them at once).
        log = _EventLog()
        PipelineTracker(_ANALYSTS, on_event=log)
        assert log.events == [
            ("market", "running", None, {}),
            ("social", "running", None, {}),
            ("news", "running", None, {}),
            ("fundamentals", "running", None, {}),
        ]

    def test_report_completion_marks_done(self):
        log = _EventLog()
        tracker = PipelineTracker(_ANALYSTS, on_event=log)
        log.events.clear()
        tracker.update({"market_report": "## Market\nBullish setup."})
        market_event = log.events[0]
        stage_id, status, elapsed_s, reports = market_event
        assert (stage_id, status) == ("market", "done")
        assert elapsed_s is not None and elapsed_s >= 0
        assert reports == {"report-market": "## Market\nBullish setup."}
        # Other analysts were already running from construction — no new
        # "running" transition to emit for them here.
        assert len(log.events) == 1

    def test_sentiment_report_maps_to_sentiment_dom_id(self):
        log = _EventLog()
        tracker = PipelineTracker(_ANALYSTS, on_event=log)
        log.events.clear()
        tracker.update({
            "market_report": "m", "sentiment_report": "s",
        })
        reports_by_stage = {e[0]: e[3] for e in log.events if e[1] == "done"}
        assert reports_by_stage["market"] == {"report-market": "m"}
        assert reports_by_stage["social"] == {"report-sentiment": "s"}

    def test_repeated_chunk_is_idempotent(self):
        log = _EventLog()
        tracker = PipelineTracker(_ANALYSTS, on_event=log)
        chunk = {"market_report": "m"}
        tracker.update(chunk)
        n_events_after_first = len(log.events)
        tracker.update(dict(chunk))  # same cumulative state repeated, as stream_mode="values" does
        assert len(log.events) == n_events_after_first


@pytest.mark.unit
class TestPipelineTrackerDownstream:
    def _all_analysts_done_chunk(self):
        return {
            "market_report": "m", "sentiment_report": "s",
            "news_report": "n", "fundamentals_report": "f",
        }

    def test_debate_progress_then_judge_completes_research_stages(self):
        log = _EventLog()
        tracker = PipelineTracker(_ANALYSTS, on_event=log)
        tracker.update(self._all_analysts_done_chunk())
        log.events.clear()

        tracker.update({
            **self._all_analysts_done_chunk(),
            "investment_debate_state": {"bull_history": "Bull: buy the dip", "bear_history": ""},
        })
        assert (RESEARCH_DEBATE, "running", None, {"debate-bull": "Bull: buy the dip"}) in log.events

        log.events.clear()
        tracker.update({
            **self._all_analysts_done_chunk(),
            "investment_debate_state": {
                "bull_history": "Bull: buy the dip", "bear_history": "Bear: too risky",
                "judge_decision": "**Recommendation**: Buy",
            },
        })
        stage_statuses = {(e[0], e[1]) for e in log.events}
        assert (RESEARCH_DEBATE, "done") in stage_statuses
        assert (RESEARCH_MANAGER, "done") in stage_statuses
        assert (TRADER, "running") in stage_statuses
        rm_reports = next(e[3] for e in log.events if e[0] == RESEARCH_MANAGER)
        assert rm_reports == {"debate-judge-content": "**Recommendation**: Buy"}

    def test_full_run_reaches_portfolio_manager_done(self):
        log = _EventLog()
        tracker = PipelineTracker(_ANALYSTS, on_event=log)
        tracker.update(self._all_analysts_done_chunk())
        tracker.update({
            **self._all_analysts_done_chunk(),
            "investment_debate_state": {"bull_history": "b", "bear_history": "b2", "judge_decision": "j"},
        })
        tracker.update({
            **self._all_analysts_done_chunk(),
            "investment_debate_state": {"bull_history": "b", "bear_history": "b2", "judge_decision": "j"},
            "trader_investment_plan": "**Action**: Buy",
        })
        log.events.clear()
        tracker.update({
            **self._all_analysts_done_chunk(),
            "investment_debate_state": {"bull_history": "b", "bear_history": "b2", "judge_decision": "j"},
            "trader_investment_plan": "**Action**: Buy",
            "risk_debate_state": {
                "aggressive_history": "a", "conservative_history": "c", "neutral_history": "n",
                "judge_decision": "**Rating**: Overweight",
            },
        })
        stage_statuses = {(e[0], e[1]) for e in log.events}
        assert (RISK_DEBATE, "done") in stage_statuses
        assert (PORTFOLIO_MANAGER, "done") in stage_statuses
        pm_reports = next(e[3] for e in log.events if e[0] == PORTFOLIO_MANAGER)
        assert pm_reports["risk-judge-content"] == "**Rating**: Overweight"
        assert pm_reports["final-decision-content"] == "**Rating**: Overweight"
