"""Per-node progress tracking for the dashboard's live pipeline view.

``TradingAgentsGraph`` runs with ``stream_mode="values"`` (see
``propagation.py``), so every chunk handed to ``on_chunk`` is the *complete*
cumulative state so far, not a delta — a field, once populated by a node,
stays present in every later chunk. That makes "is this field non-empty yet"
a reliable, idempotent completion signal; this mirrors the exact status
transitions ``cli/main.py``'s ``update_analyst_statuses`` /
``update_research_team_status`` already use to drive the Rich TUI, just
emitting SSE-friendly events instead.

Report payloads are keyed by the dashboard's own DOM element ids
(``report-market``, ``debate-bull``, ...) so the frontend can apply them with
a single generic "setMarkdown(key, text)" call per event — no separate
key-mapping layer on either side.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from tradingagents.graph.analyst_execution import build_analyst_execution_plan

RESEARCH_DEBATE = "research_debate"
RESEARCH_MANAGER = "research_manager"
TRADER = "trader"
RISK_DEBATE = "risk_debate"
PORTFOLIO_MANAGER = "portfolio_manager"

# Analyst key -> dashboard report-card element id. Keys match
# ANALYST_NODE_SPECS; "social" (the sentiment analyst's wire key, kept for
# saved-config back-compat — see analyst_execution.py) maps to the
# "sentiment" card, matching the v0.2.5 rename.
_ANALYST_DOM_ID = {
    "market": "report-market",
    "social": "report-sentiment",
    "news": "report-news",
    "fundamentals": "report-fundamentals",
}

_STAGE_LABELS = {
    RESEARCH_DEBATE: "Bull vs. Bear Debate",
    RESEARCH_MANAGER: "Research Manager",
    TRADER: "Trader",
    RISK_DEBATE: "Risk Team Debate",
    PORTFOLIO_MANAGER: "Portfolio Manager",
}
# Chinese labels for the same stages — the front-end picks by current lang.
_STAGE_LABELS_ZH = {
    RESEARCH_DEBATE: "多空研究员辩论",
    RESEARCH_MANAGER: "研究经理裁决",
    TRADER: "交易员方案",
    RISK_DEBATE: "风险团队辩论",
    PORTFOLIO_MANAGER: "投资组合经理签批",
}
_ANALYST_LABELS_ZH = {
    "market": "技术 · 市场分析师",
    "social": "情绪分析师",
    "news": "新闻 · 宏观分析师",
    "fundamentals": "基本面分析师",
}
_FIXED_STAGE_ORDER = (RESEARCH_DEBATE, RESEARCH_MANAGER, TRADER, RISK_DEBATE, PORTFOLIO_MANAGER)

OnEvent = Callable[[str, str, float | None, dict[str, str]], None]


def build_stage_specs(selected_analysts) -> list[dict]:
    """Full pipeline topology (analysts + fixed downstream stages), in run order."""
    plan = build_analyst_execution_plan(selected_analysts)
    specs = [
        {
            "id": spec.key,
            "label": spec.agent_node,
            "label_zh": _ANALYST_LABELS_ZH.get(spec.key, spec.agent_node),
            "group": "analysts",
        }
        for spec in plan.specs
    ]
    specs += [
        {"id": RESEARCH_DEBATE, "label": _STAGE_LABELS[RESEARCH_DEBATE],
         "label_zh": _STAGE_LABELS_ZH[RESEARCH_DEBATE], "group": "research"},
        {"id": RESEARCH_MANAGER, "label": _STAGE_LABELS[RESEARCH_MANAGER],
         "label_zh": _STAGE_LABELS_ZH[RESEARCH_MANAGER], "group": "research"},
        {"id": TRADER, "label": _STAGE_LABELS[TRADER],
         "label_zh": _STAGE_LABELS_ZH[TRADER], "group": "trading"},
        {"id": RISK_DEBATE, "label": _STAGE_LABELS[RISK_DEBATE],
         "label_zh": _STAGE_LABELS_ZH[RISK_DEBATE], "group": "risk"},
        {"id": PORTFOLIO_MANAGER, "label": _STAGE_LABELS[PORTFOLIO_MANAGER],
         "label_zh": _STAGE_LABELS_ZH[PORTFOLIO_MANAGER], "group": "risk"},
    ]
    return specs


class PipelineTracker:
    """Feeds ``graph.stream()`` chunks in; emits stage transitions out.

    ``on_event(stage_id, status, elapsed_s, reports)`` fires once per actual
    transition (never repeats a status a stage already reported), where
    ``status`` is one of ``running`` / ``done`` and ``reports`` maps
    dashboard element ids to newly-available markdown text (empty when a
    transition carries no new text, e.g. a debate stage's own ``running``).
    """

    def __init__(self, selected_analysts, on_event: OnEvent):
        self._plan = build_analyst_execution_plan(selected_analysts)
        self._on_event = on_event
        self._status: dict[str, str] = {spec.key: "pending" for spec in self._plan.specs}
        for stage_id in _FIXED_STAGE_ORDER:
            self._status[stage_id] = "pending"
        self._started_at: dict[str, float] = {}
        # Every selected analyst starts together as a parallel graph branch
        # (setup.py fans out from START to all of them at once).
        for spec in self._plan.specs:
            self._set(spec.key, "running")

    def _set(self, stage_id: str, status: str, reports: dict[str, str] | None = None) -> None:
        if self._status.get(stage_id) == status:
            return
        now = time.monotonic()
        if status == "running":
            self._started_at[stage_id] = now
        self._status[stage_id] = status
        elapsed = None
        if status == "done" and stage_id in self._started_at:
            elapsed = round(now - self._started_at[stage_id], 1)
        self._on_event(stage_id, status, elapsed, reports or {})

    def update(self, chunk: dict) -> None:
        # Every analyst is already "running" from __init__ (they all start
        # in parallel) — the only transition left to detect here is done.
        for spec in self._plan.specs:
            text = chunk.get(spec.report_key)
            if text:
                self._set(spec.key, "done", {_ANALYST_DOM_ID[spec.key]: text})

        debate = chunk.get("investment_debate_state") or {}
        bull, bear = debate.get("bull_history"), debate.get("bear_history")
        if bull or bear:
            reports = {}
            if bull:
                reports["debate-bull"] = bull
            if bear:
                reports["debate-bear"] = bear
            self._set(RESEARCH_DEBATE, "running", reports)
        if debate.get("judge_decision"):
            self._set(RESEARCH_DEBATE, "done")
            self._set(RESEARCH_MANAGER, "done", {"debate-judge-content": debate["judge_decision"]})
            self._set(TRADER, "running")

        if chunk.get("trader_investment_plan"):
            self._set(TRADER, "done", {"trader-plan-content": chunk["trader_investment_plan"]})
            self._set(RISK_DEBATE, "running")

        risk = chunk.get("risk_debate_state") or {}
        agg, con, neu = (
            risk.get("aggressive_history"), risk.get("conservative_history"), risk.get("neutral_history"),
        )
        if agg or con or neu:
            reports = {}
            if agg:
                reports["risk-aggressive"] = agg
            if con:
                reports["risk-conservative"] = con
            if neu:
                reports["risk-neutral"] = neu
            self._set(RISK_DEBATE, "running", reports)
        if risk.get("judge_decision"):
            self._set(RISK_DEBATE, "done")
            self._set(PORTFOLIO_MANAGER, "done", {"risk-judge-content": risk["judge_decision"]})
