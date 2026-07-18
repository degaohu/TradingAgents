"""The four analysts run as parallel graph branches that converge on a
deferred "Bull Researcher" join node (graph/setup.py). Without
``defer=True`` on that node, LangGraph runs it once per branch as each one
arrives — not once after all of them have — duplicating the debate's
opening round (confirmed against this LangGraph version with a standalone
fan-out/fan-in script before relying on the behavior in setup.py).

These tests guard both halves of that contract:
- static topology: every analyst has a direct START edge and the join node
  is actually marked defer=True (regresses instantly if either is dropped).
- dynamic execution: analysts that finish after different numbers of
  ReAct-loop steps still converge on the join exactly once, with every
  analyst's report present.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from tradingagents.agents.analysts.fundamentals_analyst import create_fundamentals_analyst
from tradingagents.agents.analysts.market_analyst import create_market_analyst
from tradingagents.agents.analysts.news_analyst import create_news_analyst
from tradingagents.agents.analysts.sentiment_analyst import create_sentiment_analyst
from tradingagents.agents.utils.agent_states import AgentState
from tradingagents.graph.analyst_execution import ANALYST_NODE_SPECS, build_analyst_execution_plan
from tradingagents.graph.conditional_logic import ConditionalLogic
from tradingagents.graph.propagation import Propagator
from tradingagents.graph.setup import GraphSetup

_ALL_ANALYSTS = ("market", "social", "news", "fundamentals")


def _build_workflow():
    """GraphSetup with placeholder LLMs — analyst factories only capture the
    LLM in a closure at setup time; nothing is invoked until the graph runs."""
    conditional_logic = ConditionalLogic(max_debate_rounds=1, max_risk_discuss_rounds=1)
    tool_nodes = {
        key: ToolNode([], messages_key=spec.messages_key)
        for key, spec in ANALYST_NODE_SPECS.items()
    }
    setup = GraphSetup(
        quick_thinking_llm=object(),
        deep_thinking_llm=object(),
        tool_nodes=tool_nodes,
        conditional_logic=conditional_logic,
    )
    return setup.setup_graph(_ALL_ANALYSTS)


@pytest.mark.unit
class TestParallelAnalystTopology:
    def test_every_analyst_has_a_direct_start_edge(self):
        workflow = _build_workflow()
        plan = build_analyst_execution_plan(_ALL_ANALYSTS)
        for spec in plan.specs:
            assert (START, spec.agent_node) in workflow.edges, (
                f"{spec.agent_node} must start directly from START (parallel fan-out), "
                "not be chained after another analyst"
            )

    def test_bull_researcher_join_is_deferred(self):
        workflow = _build_workflow()
        assert workflow.nodes["Bull Researcher"].defer is True, (
            "Bull Researcher joins four parallel analyst branches; without "
            "defer=True it runs once per branch instead of once after all finish"
        )

    def test_no_leftover_msg_clear_nodes(self):
        # The sequential-handoff "Msg Clear X" nodes are gone now that each
        # analyst has its own isolated message channel — nothing resets a
        # shared list between them anymore.
        workflow = _build_workflow()
        assert not any(name.startswith("Msg Clear") for name in workflow.nodes)

    def test_each_tool_node_routes_back_to_its_own_analyst(self):
        workflow = _build_workflow()
        for spec in build_analyst_execution_plan(_ALL_ANALYSTS).specs:
            assert (spec.tool_node, spec.agent_node) in workflow.edges


class _ScriptedToolCallingLLM:
    """bind_tools() returns this; invoke() emits `steps - 1` tool-call
    responses then one final (no tool_calls) response carrying the report."""

    def __init__(self, steps: int, report_text: str):
        self.steps = steps
        self.report_text = report_text
        self.calls = 0

    def invoke(self, _prompt_value):
        self.calls += 1
        if self.calls < self.steps:
            return AIMessage(content="", tool_calls=[{"name": "noop", "args": {}, "id": f"c{self.calls}"}])
        return AIMessage(content=self.report_text, tool_calls=[])

    __call__ = invoke


class _FakeAnalystLLM:
    """Stands in for quick_thinking_llm: bind_tools() hands back a scripted
    responder; with_structured_output() is unsupported so the sentiment
    analyst takes its free-text fallback path (exercised elsewhere in
    test_structured_agents.py — irrelevant to what this test verifies)."""

    def __init__(self, steps: int, report_text: str):
        self._scripted = _ScriptedToolCallingLLM(steps, report_text)
        self._report_text = report_text

    def bind_tools(self, _tools):
        return self._scripted

    def invoke(self, _prompt):
        return AIMessage(content=self._report_text)

    def with_structured_output(self, _schema, **_kwargs):
        raise NotImplementedError("not exercised by this test")


@pytest.mark.unit
class TestParallelAnalystExecution:
    def test_join_fires_once_despite_uneven_analyst_loop_lengths(self):
        # Steps deliberately differ so the branches finish at different
        # supersteps — this is exactly the scenario that would double-fire
        # an un-deferred join.
        steps_by_key = {"market": 1, "social": 1, "news": 3, "fundamentals": 2}
        llms = {k: _FakeAnalystLLM(steps, f"{k} report text") for k, steps in steps_by_key.items()}

        factories = {
            "market": lambda: create_market_analyst(llms["market"]),
            "social": lambda: create_sentiment_analyst(llms["social"]),
            "news": lambda: create_news_analyst(llms["news"]),
            "fundamentals": lambda: create_fundamentals_analyst(llms["fundamentals"]),
        }
        tool_nodes = {
            key: ToolNode([_noop_tool()], messages_key=ANALYST_NODE_SPECS[key].messages_key)
            for key in steps_by_key
        }

        join_calls = {"n": 0}

        def fake_join(state):
            join_calls["n"] += 1
            return {"investment_debate_state": {**state["investment_debate_state"], "count": 1}}

        workflow = StateGraph(AgentState)
        conditional_logic = ConditionalLogic(max_debate_rounds=1, max_risk_discuss_rounds=1)
        for spec in build_analyst_execution_plan(list(steps_by_key)).specs:
            workflow.add_node(spec.agent_node, factories[spec.key]())
            workflow.add_node(spec.tool_node, tool_nodes[spec.key])
        workflow.add_node("Bull Researcher", fake_join, defer=True)

        for spec in build_analyst_execution_plan(list(steps_by_key)).specs:
            workflow.add_edge(START, spec.agent_node)
            workflow.add_conditional_edges(
                spec.agent_node,
                getattr(conditional_logic, f"should_continue_{spec.key}"),
                [spec.tool_node, "Bull Researcher"],
            )
            workflow.add_edge(spec.tool_node, spec.agent_node)
        workflow.add_edge("Bull Researcher", END)

        graph = workflow.compile()
        init_state = Propagator().create_initial_state(
            "NVDA", "2026-01-15", instrument_context="NVDA is a company."
        )
        init_state["investment_debate_state"]["count"] = 0

        # The sentiment analyst pre-fetches news/StockTwits/Reddit/Trends
        # unconditionally (sentiment_analyst.py) — stub them so this test
        # stays hermetic and fast; sentiment's own report content is
        # covered by test_structured_agents.py, not here.
        sa = "tradingagents.agents.analysts.sentiment_analyst"
        with patch(f"{sa}.get_news") as mock_get_news, \
             patch(f"{sa}.fetch_stocktwits_messages", return_value="<stub>"), \
             patch(f"{sa}.fetch_reddit_posts", return_value="<stub>"), \
             patch(f"{sa}.fetch_google_trends_interest", return_value="<stub>"):
            mock_get_news.func.return_value = "<stub>"
            final_state = graph.invoke(init_state, {"recursion_limit": 100})

        assert join_calls["n"] == 1, (
            f"Bull Researcher must run exactly once after all four analysts finish, "
            f"ran {join_calls['n']} times"
        )
        assert final_state["market_report"] == "market report text"
        assert final_state["news_report"] == "news report text"
        assert final_state["fundamentals_report"] == "fundamentals report text"
        # Sentiment took the free-text fallback path (with_structured_output
        # unsupported in this fake) — still confirms its branch completed
        # and reached the join alongside the tool-calling analysts.
        assert final_state["sentiment_report"]


def _noop_tool():
    @tool
    def noop() -> str:
        """no-op tool"""
        return "ok"

    return noop
