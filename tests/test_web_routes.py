"""Integration tests for web/routes.py: the job lifecycle over HTTP + SSE.

TradingAgentsGraph is replaced with a fake whose ``propagate()`` drives the
same ``on_chunk``/``should_cancel`` contract the real graph does, so these
tests exercise the actual job/pipeline/SSE wiring without any LLM or network
dependency.
"""

from __future__ import annotations

import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

import web.routes as routes_module
from tradingagents.graph.trading_graph import RunCancelled
from web.jobs import JobRegistry

_STREAM_DEADLINE = 5.0


def _analyst_chunk(**overrides):
    base = {"market_report": "", "sentiment_report": "", "news_report": "", "fundamentals_report": ""}
    base.update(overrides)
    return base


class _FakeGraphFactory:
    """Builds a fake TradingAgentsGraph class bound to test-controlled hooks."""

    def __init__(self, cancel_after_first_chunk: bool = False):
        self.cancel_after_first_chunk = cancel_after_first_chunk
        self.first_chunk_emitted = threading.Event()

    def build(self):
        outer = self

        class _FakeGraph:
            def __init__(self, *args, **kwargs):
                pass

            def propagate(self, ticker, trade_date, on_chunk=None, should_cancel=None):
                if on_chunk:
                    on_chunk(_analyst_chunk(market_report="## Market\nBullish."))
                outer.first_chunk_emitted.set()

                if outer.cancel_after_first_chunk:
                    deadline = time.monotonic() + _STREAM_DEADLINE
                    while time.monotonic() < deadline:
                        if should_cancel and should_cancel():
                            raise RunCancelled(ticker, trade_date)
                        time.sleep(0.01)
                    raise AssertionError("cancel was never requested by the test")

                full_chunk = _analyst_chunk(
                    market_report="## Market\nBullish.",
                    sentiment_report="## Sentiment\nMixed.",
                    news_report="## News\nQuiet.",
                    fundamentals_report="## Fundamentals\nSolid.",
                )
                full_chunk["investment_debate_state"] = {
                    "bull_history": "Bull: strong case",
                    "bear_history": "Bear: valuation risk",
                    "judge_decision": "**Recommendation**: Buy",
                }
                full_chunk["trader_investment_plan"] = (
                    "**Action**: Buy\n\n**Entry Price**: 1.50\n\n**Stop Loss**: 1.30"
                )
                full_chunk["risk_debate_state"] = {
                    "aggressive_history": "a", "conservative_history": "c", "neutral_history": "n",
                    "judge_decision": "**Rating**: Overweight\n\n**Executive Summary**: Enter now.",
                }
                if on_chunk:
                    on_chunk(full_chunk)

                final_state = dict(full_chunk)
                final_state["company_of_interest"] = ticker
                final_state["trade_date"] = trade_date
                final_state["final_trade_decision"] = full_chunk["risk_debate_state"]["judge_decision"]
                return final_state, "Overweight"

        return _FakeGraph


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(routes_module, "registry", JobRegistry())
    return TestClient(routes_module.app)


def _read_sse_events(client, job_id, terminal_types=("result", "error", "cancelled")):
    events = []
    deadline = time.monotonic() + _STREAM_DEADLINE
    with client.stream("GET", f"/api/jobs/{job_id}/events") as response:
        assert response.status_code == 200
        for line in response.iter_lines():
            if time.monotonic() > deadline:
                raise AssertionError(f"SSE stream did not terminate; got so far: {events}")
            if not line.startswith("data:"):
                continue
            payload = json.loads(line[len("data:"):].strip())
            events.append(payload)
            if payload.get("type") in terminal_types:
                break
    return events


@pytest.mark.unit
class TestAnalyzeLifecycle:
    def test_full_run_streams_topology_stage_and_result_events(self, client, monkeypatch):
        monkeypatch.setattr(routes_module, "TradingAgentsGraph", _FakeGraphFactory().build())

        resp = client.post(
            "/api/analyze", json={"ticker": "NVDA", "trade_date": "2026-01-15"},
        )
        assert resp.status_code == 202
        job_id = resp.json()["job_id"]

        events = _read_sse_events(client, job_id)
        types = [e["type"] for e in events]
        assert "topology" in types
        assert "stage" in types
        assert types[-1] == "result"

        result_data = events[-1]["data"]
        assert result_data["decision"] == "Overweight"
        assert result_data["decision_summary"]["action"] == "BUY"
        assert result_data["decision_summary"]["entry_price"] == 1.50

        stage_events = [e for e in events if e["type"] == "stage"]
        market_done = next(e for e in stage_events if e["stage_id"] == "market" and e["status"] == "done")
        assert market_done["reports"]["report-market"] == "## Market\nBullish."

        snapshot = client.get(f"/api/jobs/{job_id}").json()
        assert snapshot["status"] == "done"
        assert snapshot["result"]["decision"] == "Overweight"

    def test_second_concurrent_analyze_returns_409_with_running_job(self, client, monkeypatch):
        factory = _FakeGraphFactory(cancel_after_first_chunk=True)
        monkeypatch.setattr(routes_module, "TradingAgentsGraph", factory.build())

        first = client.post("/api/analyze", json={"ticker": "NVDA", "trade_date": "2026-01-15"})
        job_id = first.json()["job_id"]
        assert factory.first_chunk_emitted.wait(timeout=_STREAM_DEADLINE)

        second = client.post("/api/analyze", json={"ticker": "TSLA", "trade_date": "2026-01-16"})
        assert second.status_code == 409
        assert second.json()["detail"]["job_id"] == job_id

        client.post(f"/api/jobs/{job_id}/cancel")

    def test_cancel_transitions_running_job_to_cancelled(self, client, monkeypatch):
        factory = _FakeGraphFactory(cancel_after_first_chunk=True)
        monkeypatch.setattr(routes_module, "TradingAgentsGraph", factory.build())

        resp = client.post("/api/analyze", json={"ticker": "NVDA", "trade_date": "2026-01-15"})
        job_id = resp.json()["job_id"]
        assert factory.first_chunk_emitted.wait(timeout=_STREAM_DEADLINE)

        cancel_resp = client.post(f"/api/jobs/{job_id}/cancel")
        assert cancel_resp.status_code == 202

        events = _read_sse_events(client, job_id)
        assert events[-1]["type"] == "cancelled"
        assert client.get(f"/api/jobs/{job_id}").json()["status"] == "cancelled"

    def test_cancel_on_already_finished_job_reports_status_without_error(self, client, monkeypatch):
        monkeypatch.setattr(routes_module, "TradingAgentsGraph", _FakeGraphFactory().build())
        resp = client.post("/api/analyze", json={"ticker": "NVDA", "trade_date": "2026-01-15"})
        job_id = resp.json()["job_id"]
        _read_sse_events(client, job_id)

        cancel_resp = client.post(f"/api/jobs/{job_id}/cancel")
        assert cancel_resp.status_code == 200
        assert cancel_resp.json()["status"] == "done"


@pytest.mark.unit
class TestUnknownJob:
    def test_get_unknown_job_returns_404(self, client):
        assert client.get("/api/jobs/does-not-exist").status_code == 404

    def test_cancel_unknown_job_returns_404(self, client):
        assert client.post("/api/jobs/does-not-exist/cancel").status_code == 404

    def test_events_for_unknown_job_returns_404(self, client):
        assert client.get("/api/jobs/does-not-exist/events").status_code == 404


@pytest.mark.unit
class TestPolishEndpoint:
    def _finish_a_job(self, client, monkeypatch):
        monkeypatch.setattr(routes_module, "TradingAgentsGraph", _FakeGraphFactory().build())
        resp = client.post("/api/analyze", json={"ticker": "NVDA", "trade_date": "2026-01-15"})
        job_id = resp.json()["job_id"]
        _read_sse_events(client, job_id)
        return job_id

    def test_unknown_job_returns_404(self, client):
        assert client.post("/api/jobs/does-not-exist/polish").status_code == 404

    def test_still_running_job_returns_409(self, client, monkeypatch):
        factory = _FakeGraphFactory(cancel_after_first_chunk=True)
        monkeypatch.setattr(routes_module, "TradingAgentsGraph", factory.build())
        resp = client.post("/api/analyze", json={"ticker": "NVDA", "trade_date": "2026-01-15"})
        job_id = resp.json()["job_id"]
        assert factory.first_chunk_emitted.wait(timeout=_STREAM_DEADLINE)

        polish_resp = client.post(f"/api/jobs/{job_id}/polish")
        assert polish_resp.status_code == 409

        client.post(f"/api/jobs/{job_id}/cancel")

    def test_completed_job_returns_polished_markdown_and_caches_it(self, client, monkeypatch):
        job_id = self._finish_a_job(client, monkeypatch)

        fake_llm = type("FakeLLM", (), {"invoke": staticmethod(lambda prompt: type("R", (), {"content": "# Polished\n..."})())})()
        fake_client = type("FakeClient", (), {"get_llm": lambda self: fake_llm})()
        call_count = {"n": 0}

        def fake_create_llm_client(**kwargs):
            call_count["n"] += 1
            return fake_client

        monkeypatch.setattr("web.polish.create_llm_client", fake_create_llm_client)

        first = client.post(f"/api/jobs/{job_id}/polish")
        assert first.status_code == 200
        assert first.json()["polished_markdown"] == "# Polished\n..."

        second = client.post(f"/api/jobs/{job_id}/polish")
        assert second.status_code == 200
        assert second.json()["polished_markdown"] == "# Polished\n..."
        assert call_count["n"] == 1  # cached — second call didn't re-invoke the LLM

    def test_llm_failure_returns_502(self, client, monkeypatch):
        job_id = self._finish_a_job(client, monkeypatch)

        def raising_create_llm_client(**kwargs):
            raise RuntimeError("provider unavailable")

        monkeypatch.setattr("web.polish.create_llm_client", raising_create_llm_client)

        resp = client.post(f"/api/jobs/{job_id}/polish")
        assert resp.status_code == 502


def test_get_price_history_returns_success_with_prices(client, monkeypatch):
    class FakeTicker:
        def __init__(self, symbol):
            self.symbol = symbol
        def history(self, start, end):
            import pandas as pd
            dates = pd.date_range(start="2026-05-01", end="2026-06-01", freq="D")
            df = pd.DataFrame({"Close": [100.0 + float(i) for i in range(len(dates))]}, index=dates)
            return df

    monkeypatch.setattr("yfinance.Ticker", FakeTicker)
    resp = client.get("/api/price/AAPL?date=2026-06-01")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success"
    assert len(data["prices"]) > 0
    assert data["prices"][0]["close"] == 100.0

