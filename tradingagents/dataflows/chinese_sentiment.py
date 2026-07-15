"""Chinese-market sentiment fetchers for A-share tickers.

The default sentiment sources (StockTwits, Reddit, Google Trends) cover
essentially zero Chinese equity chatter — an A-share ticker like
``600519.SS`` receives silence there. This module supplies the parallel
set of sources retail Chinese investors actually use:

    1. Baidu Index          — search-attention proxy via chinaz mirror
                              (real Baidu Index requires a BDUSS cookie)
    2. Baidu News           — media-coverage count + sample headlines
    3. Weibo Hot Search     — whether the stock is trending on Weibo's
                              real-time hot-topics board
    4. Xueqiu (雪球)        — quality retail-analyst discussion timeline
    5. Eastmoney Guba (股吧) — highest-volume retail forum, per-stock board

Every fetcher follows the same contract as ``stocktwits.py`` /
``google_trends.py``:

  - stdlib ``urllib`` only, ``default_ssl_context()`` for TLS, shared UA
  - short ``timeout`` (default 10.0s), single-shot request (no retry loops)
  - returns a ``str`` — first line is a headline summary, blank line, then
    detail lines ready for prompt injection
  - all HTTP / parse errors are caught and surfaced as
    ``"<xxx unavailable: ExceptionType>"``; the caller never sees an exception
  - non-A-share tickers short-circuit to
    ``"<xxx unavailable: not an A-share ticker>"`` — no network call is made
"""

from __future__ import annotations

import http.client
import http.cookiejar
import json
import logging
import re
from html import unescape
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import HTTPCookieProcessor, HTTPSHandler, Request, build_opener, urlopen

from .symbol_utils import a_share_code
from .utils import default_ssl_context

logger = logging.getLogger(__name__)

_UA = "tradingagents/0.2 (+https://github.com/TauricResearch/TradingAgents)"
# Slightly more browser-ish UA for Weibo/Baidu, whose edge WAFs reject the
# short library UA on some paths. Kept as a separate constant so we're explicit
# about *why* we're spoofing and don't drift on the two.
_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

# Seed table of A-share code → 中文简称. Chinese platforms accept the 6-digit
# code as a query term almost universally, so this is a hint layer that
# improves matching where available (Weibo hot-search text is by name; Baidu
# News blends both). Extend as needed — the empty case degrades to the code.
_CN_NAME_HINTS = {
    "600519": "贵州茅台",
    "601398": "工商银行",
    "601988": "中国银行",
    "600036": "招商银行",
    "600000": "浦发银行",
    "601318": "中国平安",
    "000001": "平安银行",
    "000002": "万科A",
    "000858": "五粮液",
    "300750": "宁德时代",
    "300059": "东方财富",
    "601899": "紫金矿业",
}


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _fetch_text(url: str, timeout: float, *, browser_ua: bool = False, headers: dict | None = None) -> str:
    """Single-shot GET returning decoded text. Raises on transport error."""
    hdrs = {"User-Agent": _BROWSER_UA if browser_ua else _UA}
    if headers:
        hdrs.update(headers)
    req = Request(url, headers=hdrs)
    with urlopen(req, timeout=timeout, context=default_ssl_context()) as resp:
        raw = resp.read()
        # Chinese sites variously ship gb2312/gbk/utf-8; content-type is often
        # wrong. Try utf-8 first, fall back to gbk, then replace.
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            try:
                return raw.decode("gbk")
            except UnicodeDecodeError:
                return raw.decode("utf-8", "replace")


def _build_cookie_opener():
    """Opener carrying a cookie jar — needed for xueqiu (issues xq_a_token)."""
    return build_opener(
        HTTPCookieProcessor(http.cookiejar.CookieJar()),
        HTTPSHandler(context=default_ssl_context()),
    )


def _keyword_for(code: str) -> str:
    """Preferred search keyword — Chinese name if we have one, else the code."""
    return _CN_NAME_HINTS.get(code, code)


def _short(text: str, limit: int = 140) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[:limit] + "…"


def _strip_html(html: str) -> str:
    """Ultra-simple HTML → plaintext: drop tags, unescape entities."""
    return _short(unescape(re.sub(r"<[^>]+>", " ", html)))


_ASHARE_UNAVAILABLE = "<{source} unavailable: not an A-share ticker>"
_ERR_TEMPLATE = "<{source} unavailable: {name}>"


def _require_a_share(ticker: str, source: str) -> tuple[str, str] | str:
    parts = a_share_code(ticker)
    if parts is None:
        return _ASHARE_UNAVAILABLE.format(source=source)
    return parts


# ---------------------------------------------------------------------------
# 1. Baidu Index (via chinaz mirror — no BDUSS required)
# ---------------------------------------------------------------------------

# chinaz keyword-detail page includes a Baidu-Index snapshot for the queried
# term. The page is HTML; we grep out the "百度指数：<n>" line. If chinaz
# changes their markup, we degrade to a placeholder — that's exactly the
# contract.
_CHINAZ_URL = "https://data.chinaz.com/keyword/allindex/{keyword}"
_BAIDU_INDEX_RE = re.compile(r"百度指数[:：]\s*(\d[\d,]*)")


def fetch_baidu_index(ticker: str, timeout: float = 10.0) -> str:
    """Approximate Baidu search attention for an A-share ticker via chinaz.

    Real Baidu Index (index.baidu.com) requires an authenticated BDUSS
    cookie and a rolling Cipher-Text header derived from JS — not viable in
    a keyless fetcher. chinaz publishes a public snapshot of the same
    number, which is what retail SEO tooling in China commonly cites.
    """
    parts = _require_a_share(ticker, "baidu index")
    if isinstance(parts, str):
        return parts
    code, _exch = parts

    keyword = _keyword_for(code)
    url = _CHINAZ_URL.format(keyword=quote(keyword, safe=""))
    try:
        html = _fetch_text(url, timeout, browser_ua=True)
    except (OSError, http.client.HTTPException, HTTPError) as exc:
        logger.warning("Baidu Index (chinaz) fetch failed for %s: %s", keyword, exc)
        return _ERR_TEMPLATE.format(source="baidu index", name=type(exc).__name__)

    match = _BAIDU_INDEX_RE.search(html)
    if not match:
        # chinaz sometimes serves the number in a JSON blob instead — look for
        # a bare "index": <n> pair as fallback.
        alt = re.search(r'"index"\s*:\s*(\d+)', html)
        if not alt:
            return f'<no Baidu Index data for query "{keyword}">'
        raw_value = alt.group(1)
    else:
        raw_value = match.group(1).replace(",", "")

    return (
        f'Query: "{keyword}" (A-share {code}) — Baidu Index via chinaz mirror\n'
        f"Current index: {raw_value}\n"
        f"(Values are search-attention only, not directional. Real-Baidu-Index "
        f"authenticated series would give a 7/30-day trend; the mirror gives a snapshot.)"
    )


# ---------------------------------------------------------------------------
# 2. Baidu News (媒体报道指数 proxy)
# ---------------------------------------------------------------------------

_BAIDU_NEWS_URL = "https://www.baidu.com/s?tn=news&word={keyword}&rn=20"
# Baidu news card layout uses <h3 class="news-title..."> containers around
# each headline; the total-results line reads "百度为您找到相关资讯约 N 篇".
_BAIDU_RESULT_COUNT_RE = re.compile(r"找到相关(?:资讯|新闻)约?\s*([\d,]+)\s*(?:篇|条)")
_BAIDU_HEADLINE_RE = re.compile(
    r'<h3[^>]*class="[^"]*news-title[^"]*"[^>]*>(.*?)</h3>', re.DOTALL
)


def fetch_baidu_news_coverage(ticker: str, timeout: float = 10.0) -> str:
    """Baidu News coverage volume + sample headlines for an A-share ticker."""
    parts = _require_a_share(ticker, "baidu news")
    if isinstance(parts, str):
        return parts
    code, _exch = parts

    keyword = _keyword_for(code)
    url = _BAIDU_NEWS_URL.format(keyword=quote(keyword, safe=""))
    try:
        html = _fetch_text(url, timeout, browser_ua=True)
    except (OSError, http.client.HTTPException, HTTPError) as exc:
        logger.warning("Baidu News fetch failed for %s: %s", keyword, exc)
        return _ERR_TEMPLATE.format(source="baidu news", name=type(exc).__name__)

    count_match = _BAIDU_RESULT_COUNT_RE.search(html)
    total = count_match.group(1).replace(",", "") if count_match else "?"

    headlines: list[str] = []
    for m in _BAIDU_HEADLINE_RE.finditer(html):
        cleaned = _strip_html(m.group(1))
        if cleaned:
            headlines.append(cleaned)
        if len(headlines) >= 10:
            break

    if not headlines and total == "?":
        return f'<no Baidu News data for query "{keyword}">'

    lines = [
        f'Query: "{keyword}" (A-share {code}) — Baidu News search '
        f"(media-coverage volume proxy)",
        f"Total results reported: {total}",
        "",
    ]
    lines += [f"- {h}" for h in headlines] or ["(no headlines parsed from the results page)"]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 3. Weibo Hot Search (榜单)
# ---------------------------------------------------------------------------

_WEIBO_HOT_URL = "https://s.weibo.com/top/summary?cate=realtimehot"
# Each hot-search row renders as <td class="td-02"><a href="/weibo?q=...">title</a>
_WEIBO_ROW_RE = re.compile(
    r'<td class="td-02">\s*<a href="[^"]*"[^>]*>([^<]+)</a>', re.DOTALL
)


def fetch_weibo_hot_search(ticker: str, timeout: float = 10.0) -> str:
    """Check whether an A-share ticker (code or name) is trending on Weibo's
    real-time hot-search board, and return the top-of-board context.
    """
    parts = _require_a_share(ticker, "weibo hot search")
    if isinstance(parts, str):
        return parts
    code, _exch = parts

    name = _CN_NAME_HINTS.get(code)
    needles = [code] + ([name] if name else [])

    try:
        html = _fetch_text(_WEIBO_HOT_URL, timeout, browser_ua=True)
    except (OSError, http.client.HTTPException, HTTPError) as exc:
        logger.warning("Weibo hot-search fetch failed for %s: %s", code, exc)
        return _ERR_TEMPLATE.format(source="weibo hot search", name=type(exc).__name__)

    entries = [_strip_html(m.group(1)) for m in _WEIBO_ROW_RE.finditer(html)]
    entries = [e for e in entries if e]
    if not entries:
        return "<no Weibo hot-search entries parsed>"

    hits = [
        (rank + 1, title)
        for rank, title in enumerate(entries)
        for needle in needles
        if needle and needle in title
    ]

    header = f'A-share {code}' + (f' / "{name}"' if name else "")
    if hits:
        rank, title = hits[0]
        summary = (
            f"Weibo hot search — {header} IS TRENDING at rank #{rank}: {title!r}"
        )
    else:
        summary = (
            f"Weibo hot search — {header} is NOT on the top-{len(entries)} "
            f"real-time board right now (weak retail-buzz signal)."
        )
    top5 = "\n".join(f"  #{i+1}. {t}" for i, t in enumerate(entries[:5]))
    return f"{summary}\n\nTop 5 on the board right now (context):\n{top5}"


# ---------------------------------------------------------------------------
# 4. Xueqiu (雪球) discussion timeline
# ---------------------------------------------------------------------------

_XUEQIU_HOME = "https://xueqiu.com"
_XUEQIU_TIMELINE = (
    "https://stock.xueqiu.com/v5/stock/comment/list.json?symbol={symbol}&count=20"
)


def _xueqiu_symbol(code: str, exch: str) -> str:
    # Xueqiu uses SH600519 / SZ000001 / BJ430047
    return f"{exch}{code}"


def fetch_xueqiu_posts(ticker: str, timeout: float = 10.0) -> str:
    """Recent Xueqiu (雪球) discussion for an A-share ticker.

    Xueqiu requires an anonymous ``xq_a_token`` cookie — obtained by visiting
    the homepage once, the same handshake pattern Google Trends uses.
    """
    parts = _require_a_share(ticker, "xueqiu")
    if isinstance(parts, str):
        return parts
    code, exch = parts

    symbol = _xueqiu_symbol(code, exch)
    opener = _build_cookie_opener()
    try:
        with opener.open(
            Request(_XUEQIU_HOME, headers={"User-Agent": _BROWSER_UA}), timeout=timeout
        ):
            pass
    except (OSError, http.client.HTTPException) as exc:
        logger.debug("Xueqiu cookie bootstrap failed: %s", exc)

    url = _XUEQIU_TIMELINE.format(symbol=symbol)
    req = Request(url, headers={"User-Agent": _BROWSER_UA, "Accept": "application/json"})
    try:
        with opener.open(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except (OSError, http.client.HTTPException, json.JSONDecodeError, HTTPError) as exc:
        logger.warning("Xueqiu fetch failed for %s: %s", symbol, exc)
        return _ERR_TEMPLATE.format(source="xueqiu", name=type(exc).__name__)

    items = ((data.get("data") or {}).get("items")) or [] if isinstance(data, dict) else []
    if not items:
        return f"<no Xueqiu posts found for {symbol}>"

    lines = []
    for post in items[:20]:
        user = ((post.get("user") or {}).get("screen_name")) or "?"
        text = _strip_html(post.get("text") or post.get("description") or "")
        reply = post.get("reply_count", 0)
        retweet = post.get("retweet_count", 0)
        like = post.get("like_count", 0)
        lines.append(
            f"[@{user} · reply {reply} · retweet {retweet} · like {like}] {text}"
        )

    header = (
        f"Xueqiu (雪球) — {symbol}, {len(items)} most-recent discussion posts "
        f"(quality-weighted retail: engagement counts follow the [] tags)"
    )
    return header + "\n\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# 5. Eastmoney Guba (股吧)
# ---------------------------------------------------------------------------

_GUBA_URL = "https://guba.eastmoney.com/list,{code}.html"
# Guba encodes each row's read/reply counts in <span class="l1 a1">read</span>
# <span class="l2 a2">reply</span> ... and the title in <span class="l3 a3"><a title="...">.
_GUBA_ROW_RE = re.compile(
    r'<div class="articleh normal_post[^"]*">.*?'
    r'<span class="l1 a1">([\d.]+[万亿]?)</span>.*?'
    r'<span class="l2 a2">([\d.]+[万亿]?)</span>.*?'
    r'<span class="l3 a3">.*?title="([^"]+)".*?</span>',
    re.DOTALL,
)


def fetch_eastmoney_guba(ticker: str, timeout: float = 10.0) -> str:
    """Recent Eastmoney Guba (东方财富股吧) posts for an A-share ticker.

    Guba is the highest-volume retail forum in China — noisier than Xueqiu,
    but volume itself is a signal (post count + read/reply counts).
    """
    parts = _require_a_share(ticker, "eastmoney guba")
    if isinstance(parts, str):
        return parts
    code, _exch = parts

    url = _GUBA_URL.format(code=code)
    try:
        html = _fetch_text(url, timeout, browser_ua=True)
    except (OSError, http.client.HTTPException, HTTPError) as exc:
        logger.warning("Eastmoney Guba fetch failed for %s: %s", code, exc)
        return _ERR_TEMPLATE.format(source="eastmoney guba", name=type(exc).__name__)

    rows = _GUBA_ROW_RE.findall(html)
    if not rows:
        return f"<no Eastmoney Guba posts parsed for {code}>"

    lines = []
    for read, reply, title in rows[:20]:
        lines.append(f"[read {read} · reply {reply}] {_short(title, 120)}")

    header = (
        f"Eastmoney Guba (东方财富股吧) — code {code}, "
        f"{len(rows)} most-recent posts on the per-stock board "
        f"(retail-heavy; read/reply counts follow the [] tags)"
    )
    return header + "\n\n" + "\n".join(lines)
