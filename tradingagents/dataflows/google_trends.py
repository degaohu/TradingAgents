"""Google Trends search-interest fetcher (free, keyless).

Google Trends has no official API, but the JSON endpoints behind
trends.google.com are publicly reachable without a key: ``/trends/api/explore``
issues a short-lived widget token, and ``/trends/api/widgetdata/multiline``
returns the interest-over-time series for that token. This is the same
two-step handshake the pytrends library performs; doing it directly with
stdlib ``urllib`` keeps the dependency footprint at zero, matching the
Reddit and StockTwits fetchers.

Search interest is an *attention* signal, not a directional one: values are
scaled 0-100 relative to the peak within the requested window for the query,
so a spike means "many people suddenly care", not "people are bullish". The
sentiment-analyst prompt explains this framing to the LLM.

Google throttles these endpoints aggressively per-IP (HTTP 429), and accepts
cookie-less clients less reliably — so each fetch first primes a cookie jar
from the Trends homepage. One analysis run makes a single series request,
which stays comfortably under the limit in normal use. Like the sibling
fetchers, any failure degrades to a placeholder string rather than raising,
so callers never special-case missing data.
"""

from __future__ import annotations

import http.client
import http.cookiejar
import json
import logging
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, HTTPSHandler, Request, build_opener

from .symbol_utils import crypto_base
from .utils import default_ssl_context

logger = logging.getLogger(__name__)

_HOME = "https://trends.google.com/?geo=US"
_EXPLORE = "https://trends.google.com/trends/api/explore?{qs}"
_MULTILINE = "https://trends.google.com/trends/api/widgetdata/multiline?{qs}"
_UA = "tradingagents/0.2 (+https://github.com/TauricResearch/TradingAgents)"
# ~90 days of daily buckets: enough baseline to tell a genuine attention
# spike from ordinary chatter, short enough that the 0-100 rescaling isn't
# dominated by some months-old event.
_TIMEFRAME = "today 3-m"


def _trends_keyword(ticker: str) -> str:
    """Map a pipeline symbol to a Google search query with usable volume.

    Bare tickers are poor Trends queries: "F" or "ALL" match unrelated
    searches, while thinly-traded symbols alone have near-zero volume.
    "<TICKER> stock" is the conventional disambiguator and what retail
    investors actually type. Crypto reaches us as a Yahoo pair (BTC-USD);
    "<BASE> price" matches how crypto attention shows up in search
    ("btc price", "doge price").
    """
    base = crypto_base(ticker)
    if base:
        return f"{base} price"
    return f"{ticker.strip().upper()} stock"


def _build_opener():
    """Opener with a cookie jar (Google sets NID via a homepage redirect)."""
    return build_opener(
        HTTPCookieProcessor(http.cookiejar.CookieJar()),
        HTTPSHandler(context=default_ssl_context()),
    )


def _bootstrap_cookies(opener, timeout: float) -> None:
    """Prime the opener's cookie jar from the Trends homepage.

    The API endpoints answer 429 far more often for cookie-less clients; the
    homepage redirect chain sets the NID cookie the jar then replays. Failure
    is non-fatal — the API calls are still attempted bare.
    """
    try:
        with opener.open(Request(_HOME, headers={"User-Agent": _UA}), timeout=timeout):
            pass
    except (OSError, http.client.HTTPException) as exc:
        logger.debug("Google Trends cookie bootstrap failed: %s", exc)


def _get_json(opener, url: str, timeout: float) -> dict:
    req = Request(url, headers={"User-Agent": _UA, "Accept": "application/json"})
    with opener.open(req, timeout=timeout) as resp:
        text = resp.read().decode("utf-8", "replace")
    # Google prefixes these responses with an XSSI guard (")]}'," etc.);
    # the JSON body starts at the first brace.
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object in Trends response")
    return json.loads(text[start:])


def _fetch_interest_series(keyword: str, timeout: float) -> list[tuple[str, int]]:
    """Run the explore → widgetdata handshake; return [(YYYY-MM-DD, 0-100)]."""
    opener = _build_opener()
    _bootstrap_cookies(opener, timeout)

    explore_req = {
        "comparisonItem": [{"keyword": keyword, "geo": "", "time": _TIMEFRAME}],
        "category": 0,
        "property": "",
    }
    explore = _get_json(
        opener,
        _EXPLORE.format(qs=urlencode({"hl": "en-US", "tz": "0", "req": json.dumps(explore_req)})),
        timeout,
    )
    widget = next(
        (w for w in explore.get("widgets", []) if w.get("id") == "TIMESERIES"), None
    )
    if not widget or "token" not in widget:
        raise ValueError("Trends explore response carried no TIMESERIES widget")

    payload = _get_json(
        opener,
        _MULTILINE.format(
            qs=urlencode(
                {
                    "hl": "en-US",
                    "tz": "0",
                    "req": json.dumps(widget["request"]),
                    "token": widget["token"],
                }
            )
        ),
        timeout,
    )
    return _parse_timeline(payload)


def _parse_timeline(payload: dict) -> list[tuple[str, int]]:
    timeline = ((payload.get("default") or {}).get("timelineData")) or []
    series = []
    for point in timeline:
        # The trailing bucket is flagged isPartial while its day/week is still
        # accumulating; its value reads misleadingly low, so skip it.
        if not isinstance(point, dict) or point.get("isPartial"):
            continue
        try:
            when = time.strftime("%Y-%m-%d", time.gmtime(int(point["time"])))
            series.append((when, int((point.get("value") or [None])[0])))
        except (KeyError, ValueError, TypeError):
            continue
    return series


def _format_block(keyword: str, series: list[tuple[str, int]]) -> str:
    values = [v for _, v in series]
    latest_date, latest = series[-1]
    peak = max(values)
    peak_date = next(d for d, v in series if v == peak)

    last7 = values[-7:]
    prior30 = values[-37:-7]
    last7_avg = sum(last7) / len(last7)
    prior30_avg = sum(prior30) / len(prior30) if prior30 else 0.0

    if prior30_avg == 0:
        trend = (
            "RISING FROM A ZERO BASE" if last7_avg > 0 else "FLAT (no measurable interest)"
        )
        detail = f" (last-7-day avg {last7_avg:.0f})"
    else:
        ratio = last7_avg / prior30_avg
        if ratio >= 1.5:
            trend = "RISING SHARPLY"
        elif ratio >= 1.15:
            trend = "RISING"
        elif ratio <= 0.65:
            trend = "FALLING SHARPLY"
        elif ratio <= 0.85:
            trend = "FALLING"
        else:
            trend = "FLAT"
        detail = (
            f" (last-7-day avg {last7_avg:.0f} vs prior-30-day avg "
            f"{prior30_avg:.0f}, {ratio - 1:+.0%})"
        )

    recent = series[-14:]
    daily = " | ".join(f"{d}: {v}" for d, v in recent)
    return (
        f'Query: "{keyword}" — worldwide web search, past 90 days, '
        f"scale 0-100 relative to the window peak\n"
        f"Latest: {latest} ({latest_date}) · window peak: {peak} on {peak_date}\n"
        f"Attention trend: {trend}{detail}\n"
        f"Recent daily interest ({len(recent)} days):\n  {daily}"
    )


def fetch_google_trends_interest(ticker: str, timeout: float = 10.0) -> str:
    """Fetch Google Trends search interest for ``ticker`` and return it as a
    formatted plaintext block ready for prompt injection.

    Returns a placeholder string when the endpoint is unreachable, throttled,
    or the response shape is unexpected — the caller never has to special-case
    None or exceptions.
    """
    keyword = _trends_keyword(ticker)
    try:
        series = _fetch_interest_series(keyword, timeout)
    except HTTPError as exc:
        if exc.code == 429:
            logger.warning("Google Trends rate-limited (429) for %r", keyword)
            return "<google trends unavailable: rate-limited (HTTP 429)>"
        logger.warning("Google Trends fetch failed for %r: %s", keyword, exc)
        return f"<google trends unavailable: HTTP {exc.code}>"
    except (OSError, http.client.HTTPException, json.JSONDecodeError, ValueError) as exc:
        # OSError covers URLError/TimeoutError/connection resets; HTTPException
        # covers chunked-transfer errors; ValueError covers XSSI-guard and
        # widget-shape surprises.
        logger.warning("Google Trends fetch failed for %r: %s", keyword, exc)
        return f"<google trends unavailable: {type(exc).__name__}>"

    if not series:
        return f"<no Google Trends data for query {keyword!r}>"
    return _format_block(keyword, series)
