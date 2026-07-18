"""Google Trends fetcher: keyword mapping, handshake parsing, and the
graceful-degradation contract shared by all social fetchers (a placeholder
string on any failure, never an exception).
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

import pytest

from tradingagents.dataflows import google_trends


def _resp(body: bytes):
    resp = MagicMock()
    resp.__enter__ = lambda self_inner: self_inner
    resp.__exit__ = lambda self_inner, *a: False
    resp.read.return_value = body
    return resp


def _fake_opener(responses):
    """Opener whose .open() yields ``responses`` in order (exceptions raise)."""
    opener = MagicMock()

    def _open(req, timeout=None):
        item = responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return _resp(item)

    opener.open.side_effect = _open
    return opener


def _explore_body(token: str = "TOK") -> bytes:
    widgets = {
        "widgets": [
            {"id": "TIMESERIES", "token": token, "request": {"time": "today 3-m"}},
            {"id": "RELATED_QUERIES", "token": "other"},
        ]
    }
    return b")]}'\n" + json.dumps(widgets).encode()


def _multiline_body(points) -> bytes:
    payload = {"default": {"timelineData": points}}
    return b")]}',\n" + json.dumps(payload).encode()


def _point(epoch: int, value: int, partial: bool = False) -> dict:
    p = {"time": str(epoch), "value": [value]}
    if partial:
        p["isPartial"] = True
    return p


_DAY = 86400
_T0 = 1780000000  # arbitrary fixed epoch; tests never touch the real clock


@pytest.mark.unit
class TestTrendsKeyword:
    @pytest.mark.parametrize(
        ("ticker", "expected"),
        [
            ("TLRY", "TLRY stock"),
            ("nvda", "NVDA stock"),
            ("BRK-B", "BRK-B stock"),
            ("BTC-USD", "BTC price"),
            ("eth-usdt", "ETH price"),
            ("XYZ-USD", "XYZ-USD stock"),  # unknown base: not treated as crypto
        ],
    )
    def test_keyword_mapping(self, ticker, expected):
        assert google_trends._trends_keyword(ticker) == expected


@pytest.mark.unit
class TestTrendsHappyPath:
    def test_full_handshake_produces_block(self):
        points = [_point(_T0 + i * _DAY, v) for i, v in enumerate([10, 20, 100, 40, 50])]
        points.append(_point(_T0 + 5 * _DAY, 1, partial=True))
        opener = _fake_opener([b"", _explore_body(), _multiline_body(points)])

        with patch.object(google_trends, "_build_opener", return_value=opener):
            out = google_trends.fetch_google_trends_interest("TLRY")

        assert 'Query: "TLRY stock"' in out
        assert "window peak: 100" in out
        # The partial trailing bucket must not surface as the latest reading.
        assert "Latest: 50" in out
        assert "Latest: 1 " not in out

    def test_cookie_bootstrap_failure_is_nonfatal(self):
        points = [_point(_T0 + i * _DAY, 30) for i in range(3)]
        opener = _fake_opener(
            [TimeoutError("homepage slow"), _explore_body(), _multiline_body(points)]
        )
        with patch.object(google_trends, "_build_opener", return_value=opener):
            out = google_trends.fetch_google_trends_interest("NVDA")
        assert 'Query: "NVDA stock"' in out

    def test_empty_timeline_returns_placeholder(self):
        opener = _fake_opener([b"", _explore_body(), _multiline_body([])])
        with patch.object(google_trends, "_build_opener", return_value=opener):
            out = google_trends.fetch_google_trends_interest("NVDA")
        assert out.startswith("<no Google Trends data")


@pytest.mark.unit
class TestTrendsResilience:
    @pytest.mark.parametrize(
        "exc",
        [
            TimeoutError("slow"),
            HTTPError("url", 503, "down", {}, None),
            ConnectionResetError("reset"),
        ],
    )
    def test_transport_errors_return_placeholder(self, exc):
        # Cookie bootstrap swallows its own errors, so the fatal error must
        # come from the explore call (second .open()).
        opener = _fake_opener([b"", exc])
        with patch.object(google_trends, "_build_opener", return_value=opener):
            out = google_trends.fetch_google_trends_interest("NVDA")
        assert out.startswith("<google trends unavailable")

    def test_rate_limit_names_the_cause(self):
        opener = _fake_opener([b"", HTTPError("url", 429, "quota", {}, None)])
        with patch.object(google_trends, "_build_opener", return_value=opener):
            out = google_trends.fetch_google_trends_interest("NVDA")
        assert "rate-limited" in out

    def test_missing_timeseries_widget_returns_placeholder(self):
        no_widget = b")]}'\n" + json.dumps({"widgets": [{"id": "RELATED_QUERIES"}]}).encode()
        opener = _fake_opener([b"", no_widget])
        with patch.object(google_trends, "_build_opener", return_value=opener):
            out = google_trends.fetch_google_trends_interest("NVDA")
        assert out.startswith("<google trends unavailable")

    def test_non_json_response_returns_placeholder(self):
        opener = _fake_opener([b"", b"<html>captcha wall</html>"])
        with patch.object(google_trends, "_build_opener", return_value=opener):
            out = google_trends.fetch_google_trends_interest("NVDA")
        assert out.startswith("<google trends unavailable")
