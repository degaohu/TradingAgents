"""Unit tests for web/decision.py: structured decision-header parsing."""

from __future__ import annotations

import pytest

from web.decision import build_decision_summary


@pytest.mark.unit
class TestBuildDecisionSummary:
    def test_full_structured_output_parses_all_fields(self):
        final_state = {
            "trader_investment_plan": (
                "**Action**: Buy\n\n"
                "**Reasoning**: Strong momentum and improving fundamentals.\n\n"
                "**Entry Price**: 1.52\n\n"
                "**Stop Loss**: 1.31\n\n"
                "**Position Sizing**: 5% of portfolio\n\n"
                "FINAL TRANSACTION PROPOSAL: **BUY**"
            ),
            "final_trade_decision": (
                "**Rating**: Overweight\n\n"
                "**Executive Summary**: Enter gradually, tight stop below support.\n\n"
                "**Investment Thesis**: Detailed reasoning here.\n\n"
                "**Price Target**: 2.10\n\n"
                "**Time Horizon**: 3-6 months"
            ),
        }
        summary = build_decision_summary(final_state, "Overweight")
        assert summary["rating"] == "Overweight"
        assert summary["action"] == "BUY"
        assert summary["entry_price"] == 1.52
        assert summary["stop_loss"] == 1.31
        assert summary["position_sizing"] == "5% of portfolio"
        assert summary["price_target"] == 2.10
        assert summary["time_horizon"] == "3-6 months"
        assert "Enter gradually" in summary["executive_summary"]
        assert "Strong momentum" in summary["reasoning"]

    @pytest.mark.parametrize(
        ("rating", "expected_action"),
        [
            ("Buy", "BUY"),
            ("Overweight", "BUY"),
            ("Hold", "HOLD"),
            ("Underweight", "SELL"),
            ("Sell", "SELL"),
        ],
    )
    def test_action_mapping_covers_all_five_tiers(self, rating, expected_action):
        assert build_decision_summary({}, rating)["action"] == expected_action

    def test_unknown_rating_defaults_to_hold_action(self):
        assert build_decision_summary({}, "Unrecognized")["action"] == "HOLD"

    def test_missing_fields_degrade_to_none_not_exception(self):
        summary = build_decision_summary({}, "Hold")
        assert summary["entry_price"] is None
        assert summary["stop_loss"] is None
        assert summary["position_sizing"] is None
        assert summary["executive_summary"] is None
        assert summary["price_target"] is None
        assert summary["time_horizon"] is None

    def test_free_text_fallback_has_no_parseable_fields(self):
        """When a provider lacks structured-output support, invoke_structured_or_freetext
        falls back to arbitrary prose without the **Field**: template — fields
        must degrade to None rather than mis-parsing free text."""
        final_state = {
            "trader_investment_plan": "I think we should buy some shares here, looks bullish.",
            "final_trade_decision": "Overall I'd rate this a buy given the setup.",
        }
        summary = build_decision_summary(final_state, "Buy")
        assert summary["entry_price"] is None
        assert summary["position_sizing"] is None
        assert summary["executive_summary"] is None

    def test_non_numeric_price_field_degrades_to_none(self):
        final_state = {"trader_investment_plan": "**Action**: Hold\n\n**Entry Price**: TBD"}
        assert build_decision_summary(final_state, "Hold")["entry_price"] is None


@pytest.mark.unit
class TestConsistencyCheck:
    """The consistency block flags when the rating-derived action
    disagrees with what the trader wrote in Position Sizing —
    surfaced by the UI as a warning banner."""

    def test_hold_rating_with_exit_narrative_flags_conflict(self):
        final_state = {
            "trader_investment_plan": (
                "**Action**: Hold\n\n"
                "**Position Sizing**: 完全退出或减仓至接近零敞口\n\n"
            ),
            "final_trade_decision": "",
        }
        summary = build_decision_summary(final_state, "Hold")
        assert summary["consistency"] is not None
        assert summary["consistency"]["conflict"] is True
        assert summary["consistency"]["rating_says"] == "HOLD"
        assert summary["consistency"]["position_says"] == "SELL"

    def test_buy_rating_with_buy_narrative_no_conflict(self):
        final_state = {
            "trader_investment_plan": (
                "**Position Sizing**: Add to position on any pullback\n\n"
            ),
            "final_trade_decision": "",
        }
        assert build_decision_summary(final_state, "Buy")["consistency"] is None

    def test_missing_position_sizing_no_conflict(self):
        final_state = {"trader_investment_plan": "", "final_trade_decision": ""}
        assert build_decision_summary(final_state, "Hold")["consistency"] is None

    def test_english_exit_language_detected(self):
        final_state = {
            "trader_investment_plan": (
                "**Position Sizing**: Reduce to zero exposure before earnings.\n\n"
            ),
            "final_trade_decision": "",
        }
        summary = build_decision_summary(final_state, "Hold")
        assert summary["consistency"]["conflict"] is True
        assert summary["consistency"]["position_says"] == "SELL"
