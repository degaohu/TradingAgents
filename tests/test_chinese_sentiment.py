"""Chinese-market sentiment fetchers: A-share detection, transport-error
resilience, and non-A-share short-circuit contract.
"""

from __future__ import annotations

import http.client
import json
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

import pytest

from tradingagents.dataflows import chinese_sentiment
from tradingagents.dataflows.symbol_utils import a_share_code


def _raise(exc):
    class _Resp:
        def __enter__(self_inner):
            return self_inner

        def __exit__(self_inner, *a):
            return False

        def read(self_inner):
            raise exc
    return _Resp()


def _resp(body: bytes):
    resp = MagicMock()
    resp.__enter__ = lambda self_inner: self_inner
    resp.__exit__ = lambda self_inner, *a: False
    resp.read.return_value = body
    return resp


@pytest.mark.unit
class TestAShareDetection:
    @pytest.mark.parametrize(
        ("ticker", "expected"),
        [
            ("600519.SS", ("600519", "SH")),
            ("000001.SZ", ("000001", "SZ")),
            ("430047.BJ", ("430047", "BJ")),
            ("600519.ss", ("600519", "SH")),
            ("  601398.SS  ", ("601398", "SH")),
        ],
    )
    def test_recognizes_a_share(self, ticker, expected):
        assert a_share_code(ticker) == expected

    @pytest.mark.parametrize(
        "ticker",
        ["AAPL", "BTC-USD", "600519", "12345.SS", "abcdef.SS", "600519.HK", "", None],
    )
    def test_rejects_non_a_share(self, ticker):
        assert a_share_code(ticker) is None


# The five fetchers all follow the same contract: HTTP error → placeholder.
# Xueqiu uses the opener, everyone else uses urlopen directly — parametrize
# accordingly.
_URLOPEN_FETCHERS = [
    ("baidu index",       chinese_sentiment.fetch_baidu_index),
    ("baidu news",        chinese_sentiment.fetch_baidu_news_coverage),
    ("weibo hot search",  chinese_sentiment.fetch_weibo_hot_search),
    ("eastmoney guba",    chinese_sentiment.fetch_eastmoney_guba),
]


@pytest.mark.unit
class TestFetchersReturnPlaceholderOnTransportErrors:
    @pytest.mark.parametrize(("source", "fn"), _URLOPEN_FETCHERS)
    @pytest.mark.parametrize(
        "exc",
        [
            http.client.IncompleteRead(b""),
            HTTPError("url", 503, "down", {}, None),
            TimeoutError("slow"),
        ],
    )
    def test_urlopen_fetchers_degrade(self, source, fn, exc):
        with patch.object(chinese_sentiment, "urlopen", return_value=_raise(exc)):
            out = fn("600519.SS")
        assert out.startswith(f"<{source} unavailable")

    @pytest.mark.parametrize(
        "exc",
        [
            http.client.IncompleteRead(b""),
            HTTPError("url", 503, "down", {}, None),
            TimeoutError("slow"),
        ],
    )
    def test_xueqiu_degrades(self, exc):
        opener = MagicMock()

        def _open(req, timeout=None):
            # Bootstrap request succeeds (returns a dummy body); real timeline
            # call fails.
            if "stock.xueqiu.com" in req.full_url:
                raise exc
            return _resp(b"<html></html>")

        opener.open.side_effect = _open
        with patch.object(chinese_sentiment, "_build_cookie_opener", return_value=opener):
            out = chinese_sentiment.fetch_xueqiu_posts("600519.SS")
        assert out.startswith("<xueqiu unavailable")


@pytest.mark.unit
class TestNonAShareShortCircuits:
    """Non-A-share tickers must not hit the network at all."""

    @pytest.mark.parametrize(("source", "fn"), _URLOPEN_FETCHERS)
    def test_no_http_for_non_a_share(self, source, fn):
        with patch.object(chinese_sentiment, "urlopen") as mock_open:
            out = fn("AAPL")
        assert out == f"<{source} unavailable: not an A-share ticker>"
        mock_open.assert_not_called()

    def test_xueqiu_no_http_for_non_a_share(self):
        with patch.object(chinese_sentiment, "_build_cookie_opener") as mock_opener:
            out = chinese_sentiment.fetch_xueqiu_posts("AAPL")
        assert out == "<xueqiu unavailable: not an A-share ticker>"
        mock_opener.assert_not_called()


@pytest.mark.unit
class TestXueqiuHappyPath:
    def test_formats_timeline_response(self):
        payload = {
            "data": {
                "items": [
                    {
                        "user": {"screen_name": "小散甲"},
                        "text": "茅台今天量能不错",
                        "reply_count": 3,
                        "retweet_count": 1,
                        "like_count": 10,
                    },
                    {
                        "user": {"screen_name": "机构分析师"},
                        "description": "<p>基本面稳健</p>",
                        "reply_count": 8,
                        "retweet_count": 2,
                        "like_count": 40,
                    },
                ]
            }
        }
        opener = MagicMock()

        def _open(req, timeout=None):
            if "stock.xueqiu.com" in req.full_url:
                return _resp(json.dumps(payload).encode("utf-8"))
            return _resp(b"<html></html>")

        opener.open.side_effect = _open
        with patch.object(chinese_sentiment, "_build_cookie_opener", return_value=opener):
            out = chinese_sentiment.fetch_xueqiu_posts("600519.SS")

        assert "Xueqiu" in out and "SH600519" in out
        assert "@小散甲" in out and "@机构分析师" in out
        assert "reply 3" in out and "like 40" in out
        # HTML stripped from the description
        assert "<p>" not in out
