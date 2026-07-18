"""Structured decision-header fields for the dashboard, built server-side.

Two data sources, combined instead of guessed at with ad-hoc regexes in JS:

- ``rating`` comes from ``TradingAgentsGraph.process_signal()`` (already
  computed by every ``propagate()`` call — see ``signal_processing.py``),
  which runs the deterministic, tested ``parse_rating`` heuristic
  (``agents/utils/rating.py``). It tolerates both the structured-output
  markdown template *and* a provider's free-text fallback, so it's the
  authoritative source for the 5-tier rating and the 3-tier buy/hold/sell
  color signal derived from it.
- Price levels / sizing / horizon are parsed from the *fixed* template
  ``render_trader_proposal`` / ``render_pm_decision`` (schemas.py) emit when
  structured output succeeds — that template is framework code, not
  LLM-controlled prose, so anchored regexes against it are reliable. On the
  free-text fallback path (provider lacks native structured-output support)
  these fields degrade to None rather than guessing at whatever labels the
  model happened to use.
"""

from __future__ import annotations

import re

_ACTION_BY_RATING = {
    "Buy": "BUY",
    "Overweight": "BUY",
    "Hold": "HOLD",
    "Underweight": "SELL",
    "Sell": "SELL",
}


# Keyword → action heuristics for reading the trader's free-text
# "Position Sizing" field. Ordered longest-first so multi-token phrases
# match before single tokens (e.g. "reduce to zero" before "reduce").
# All matching is case-insensitive on lower-cased text; Chinese phrases
# are compared directly (no case folding needed for CJK).
_POSITION_KEYWORDS: tuple[tuple[str, str], ...] = (
    # Explicit exit language dominates whatever comes after it.
    ("完全退出", "SELL"),
    ("全部退出", "SELL"),
    ("清仓", "SELL"),
    ("减仓至零", "SELL"),
    ("减仓至接近零", "SELL"),
    ("close position", "SELL"),
    ("exit position", "SELL"),
    ("reduce to zero", "SELL"),
    ("liquidate", "SELL"),
    ("trim to zero", "SELL"),
    # Directional sell without "exit" phrasing.
    ("卖出", "SELL"),
    ("减仓", "SELL"),
    ("do not add", "HOLD"),   # nuance: keep but don't add — not SELL, not BUY
    ("持有不动", "HOLD"),
    ("hold current", "HOLD"),
    ("maintain", "HOLD"),
    # Buy signals.
    ("加仓", "BUY"),
    ("建仓", "BUY"),
    ("开多", "BUY"),
    ("buy", "BUY"),
    ("add to position", "BUY"),
    ("build", "BUY"),
    ("initiate long", "BUY"),
)


def _infer_action_from_position(position_sizing: str | None) -> str | None:
    """Guess the action implied by the trader's Position Sizing narrative.

    Returns ``"BUY" | "SELL" | "HOLD"`` if a keyword matches, else ``None``.
    Used only as a cross-check against the rating-derived action so the UI
    can flag inconsistencies — never rewrites the primary signal.
    """
    if not position_sizing:
        return None
    haystack = position_sizing.lower()
    for kw, verdict in _POSITION_KEYWORDS:
        if kw.lower() in haystack:
            return verdict
    return None


def _field(text: str | None, label: str) -> str | None:
    if not text:
        return None
    m = re.search(rf"\*\*{re.escape(label)}\*\*:\s*(.*?)(?:\n\n|\Z)", text, re.DOTALL)
    if not m:
        return None
    value = m.group(1).strip()
    return value or None


def _float_field(text: str | None, label: str) -> float | None:
    raw = _field(text, label)
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


_RATING_SCORE = {
    "Buy": 2,
    "Overweight": 1,
    "Hold": 0,
    "Underweight": -1,
    "Sell": -2,
}


def _compute_confidence(
    final_state: dict,
    rating_action: str,
    position_action: str | None,
    has_entry: bool,
    has_stop: bool,
    has_target: bool,
) -> dict:
    """0-100 confidence score derived from the debate and plan completeness.

    Bloomberg/TradingView analyst rating pages surface a single scalar
    like "80% strong buy" — we synthesize an equivalent from artifacts
    we already have on hand:

    - **Debate depth**: longer bull+bear histories = more evidence weighed.
      We cap the contribution; a novella isn't 10× more valuable than a
      well-argued paragraph.
    - **Debate balance**: if one side monopolized the conversation the
      manager didn't really adjudicate; a mixed transcript scores higher.
    - **Signal consistency**: rating action matches position-sizing action
      is worth a big chunk — mismatches destroy trust.
    - **Plan completeness**: entry+stop+target all specified means the
      trader was confident enough to commit numbers.

    Returned dict includes the score and a per-component breakdown so the
    UI can render a tooltip explaining why the number came out where it did.
    """
    investment = final_state.get("investment_debate_state") or {}
    bull = investment.get("bull_history") or ""
    bear = investment.get("bear_history") or ""
    debate_count = int(investment.get("count") or 0)

    bull_len, bear_len = len(bull), len(bear)
    total = bull_len + bear_len

    # Depth: 0..25. Full points once combined transcript exceeds ~4k chars.
    depth = min(25, round((total / 4000) * 25))

    # Balance: 0..15. Peaks when both sides contributed roughly equally.
    if total == 0:
        balance = 0
    else:
        share = min(bull_len, bear_len) / total
        balance = round(share * 30)  # 0.5 share × 30 = 15
        balance = min(15, balance)

    # Round bonus: >= 2 rounds means the manager saw rebuttals, not just
    # opening statements. Up to 10 points.
    rounds = min(10, debate_count * 5)

    # Consistency: 30 for match, 0 for mismatch, 15 when we can't tell
    # (position text absent — no signal either way).
    if position_action is None:
        consistency = 15
    else:
        consistency = 30 if position_action == rating_action else 0

    # Plan completeness: 5 points each for entry / stop / target = 15 max.
    plan = (5 if has_entry else 0) + (5 if has_stop else 0) + (5 if has_target else 0)

    # Rating conviction: hold sits at neutral, edges get a bump. 0..5.
    conviction = {2: 5, -2: 5, 1: 3, -1: 3, 0: 1}.get(_RATING_SCORE.get(rating_action, 0), 1)

    total_score = depth + balance + rounds + consistency + plan + conviction
    # Never claim 0 or 100 — those imply certainty we don't have.
    total_score = max(5, min(95, total_score))

    return {
        "score": total_score,
        "components": {
            "debate_depth": depth,
            "debate_balance": balance,
            "debate_rounds": rounds,
            "signal_consistency": consistency,
            "plan_completeness": plan,
            "rating_conviction": conviction,
        },
        "band": _confidence_band(total_score),
    }


def _confidence_band(score: int) -> str:
    if score >= 75:
        return "high"
    if score >= 50:
        return "medium"
    return "low"


def build_decision_summary(final_state: dict, rating: str) -> dict:
    """Assemble the decision-header payload.

    ``rating`` is the already-computed 5-tier value (``TradingAgentsGraph.propagate()``'s
    second return value / ``SignalProcessor.process_signal()``).
    """
    trader_text = final_state.get("trader_investment_plan") or ""
    pm_text = final_state.get("final_trade_decision") or ""

    entry = _float_field(trader_text, "Entry Price")
    stop = _float_field(trader_text, "Stop Loss")
    target = _float_field(pm_text, "Price Target")

    # Risk / reward ratio — the trader's own headline number. If we've got
    # an entry and both a stop and target, prefer the take-profit side;
    # otherwise fall back to whatever's available.
    risk_reward = None
    if entry is not None and stop is not None and stop != entry:
        risk = abs(entry - stop)
        if target is not None and risk > 0:
            reward = abs(target - entry)
            risk_reward = round(reward / risk, 2) if risk else None

    upside_pct = None
    downside_pct = None
    if entry:
        if target is not None:
            upside_pct = round((target - entry) / entry * 100, 2)
        if stop is not None:
            downside_pct = round((stop - entry) / entry * 100, 2)

    position_sizing = _field(trader_text, "Position Sizing")
    rating_action = _ACTION_BY_RATING.get(rating, "HOLD")
    position_action = _infer_action_from_position(position_sizing)

    # Consistency check: the trader's rating and their Position Sizing
    # narrative come from two separate output paths and can disagree
    # (observed on TLRY: rating=Hold, position="fully exit"). We keep
    # the signal fields untouched and surface a `consistency` block the
    # UI can render as a warning banner.
    consistency: dict[str, str | bool] | None = None
    if position_action and position_action != rating_action:
        consistency = {
            "conflict": True,
            "rating_says": rating_action,
            "position_says": position_action,
            "hint": (
                "评级与「建议仓位」文本方向不一致；「建议仓位」通常反映 "
                "trader 真实的操作意图，请优先参考。"
            ),
        }

    confidence = _compute_confidence(
        final_state, rating_action, position_action,
        has_entry=entry is not None, has_stop=stop is not None,
        has_target=target is not None,
    )

    return {
        "rating": rating,
        "rating_score": _RATING_SCORE.get(rating, 0),
        "action": rating_action,
        "reasoning": _field(trader_text, "Reasoning"),
        "entry_price": entry,
        "stop_loss": stop,
        "position_sizing": position_sizing,
        "executive_summary": _field(pm_text, "Executive Summary"),
        "price_target": target,
        "time_horizon": _field(pm_text, "Time Horizon"),
        "risk_reward": risk_reward,
        "upside_pct": upside_pct,
        "downside_pct": downside_pct,
        "consistency": consistency,
        "confidence": confidence,
    }
