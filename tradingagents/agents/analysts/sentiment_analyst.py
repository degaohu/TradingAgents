"""Sentiment analyst — multi-source sentiment analysis for a target ticker.

Previously named ``social_media_analyst``. Renamed and redesigned because
the old version had a prompt that demanded social-media analysis but the
only tool available was Yahoo Finance news — which led LLMs to fabricate
Reddit/X/StockTwits content under prompt pressure (verified live).

The redesigned agent pre-fetches four complementary data sources before
the LLM is invoked and injects them into the prompt as structured blocks:

  1. News headlines     — Yahoo Finance (institutional framing)
  2. StockTwits messages — retail-trader posts indexed by cashtag, with
                           user-labeled Bullish/Bearish sentiment tags
  3. Reddit posts        — r/wallstreetbets, r/stocks, r/investing
  4. Google Trends       — relative search interest (attention volume,
                           not direction), past 90 days

The agent does not use tool-calling; the data is in the prompt from
turn 0. Output uses the structured-output pattern (json_schema for
OpenAI/xAI, response_schema for Gemini, tool-use for Anthropic), falling
back to free-text generation for providers that lack native support, so
the sentiment header (band + score + confidence) is deterministic across
runs and providers instead of free-form per-model prose.

See: https://github.com/TauricResearch/TradingAgents/issues/557
See: https://github.com/TauricResearch/TradingAgents/issues/796
"""

from datetime import datetime, timedelta

from langchain_core.messages import AIMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from tradingagents.agents.schemas import SentimentReport, render_sentiment_report
from tradingagents.agents.utils.agent_utils import (
    get_instrument_context_from_state,
    get_language_instruction,
    get_news,
)
from tradingagents.agents.utils.structured import (
    bind_structured,
    invoke_structured_or_freetext,
)
from tradingagents.dataflows.chinese_sentiment import (
    fetch_baidu_index,
    fetch_baidu_news_coverage,
    fetch_eastmoney_guba,
    fetch_weibo_hot_search,
    fetch_xueqiu_posts,
)
from tradingagents.dataflows.google_trends import fetch_google_trends_interest
from tradingagents.dataflows.reddit import fetch_reddit_posts
from tradingagents.dataflows.stocktwits import fetch_stocktwits_messages
from tradingagents.dataflows.symbol_utils import a_share_code


def _seven_days_back(trade_date: str) -> str:
    return (datetime.strptime(trade_date, "%Y-%m-%d") - timedelta(days=7)).strftime("%Y-%m-%d")


def create_sentiment_analyst(llm):
    """Create a sentiment analyst node for the trading graph.

    Pre-fetches news + StockTwits + Reddit data, injects them into the
    prompt as structured blocks, and produces a deterministic sentiment
    report via structured output (with a free-text fallback for providers
    that do not support it).
    """
    structured_llm = bind_structured(llm, SentimentReport, "Sentiment Analyst")

    def sentiment_analyst_node(state):
        ticker = state["company_of_interest"]
        end_date = state["trade_date"]
        start_date = _seven_days_back(end_date)
        instrument_context = get_instrument_context_from_state(state)

        # Pre-fetch all sources. Each fetcher degrades gracefully and
        # returns a string (no exceptions surface from here), so the LLM
        # always sees something — either real data or a clear placeholder.
        # For A-share tickers (.SS/.SZ/.BJ) we route to the Chinese-market
        # sources; StockTwits/Reddit/Google Trends have essentially no
        # A-share coverage and would return nothing useful.
        news_block = get_news.func(ticker, start_date, end_date)
        if a_share_code(ticker) is not None:
            chinese_blocks = {
                "baidu_index": fetch_baidu_index(ticker),
                "baidu_news": fetch_baidu_news_coverage(ticker),
                "weibo_hot": fetch_weibo_hot_search(ticker),
                "xueqiu": fetch_xueqiu_posts(ticker),
                "guba": fetch_eastmoney_guba(ticker),
            }
            system_message = _build_chinese_system_message(
                ticker=ticker,
                start_date=start_date,
                end_date=end_date,
                news_block=news_block,
                blocks=chinese_blocks,
            )
        else:
            stocktwits_block = fetch_stocktwits_messages(ticker, limit=30)
            reddit_block = fetch_reddit_posts(ticker)
            trends_block = fetch_google_trends_interest(ticker)
            system_message = _build_system_message(
                ticker=ticker,
                start_date=start_date,
                end_date=end_date,
                news_block=news_block,
                stocktwits_block=stocktwits_block,
                reddit_block=reddit_block,
                trends_block=trends_block,
            )

        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "You are a helpful AI assistant, collaborating with other assistants."
                    " If you or any other assistant has the FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL** or deliverable,"
                    " prefix your response with FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL** so the team knows to stop."
                    " Today's date is {current_date}; treat it as 'now' for all analysis and tool-call date ranges. {instrument_context}"
                    "\n{system_message}",
                ),
                MessagesPlaceholder(variable_name="messages"),
            ]
        )

        prompt = prompt.partial(system_message=system_message)
        prompt = prompt.partial(current_date=end_date)
        prompt = prompt.partial(instrument_context=instrument_context)

        # Format the template into a concrete message list so the structured
        # and free-text paths receive the same input. No bind_tools — the
        # data is already in the prompt.
        formatted_messages = prompt.format_messages(messages=state["sentiment_messages"])

        report_text = invoke_structured_or_freetext(
            structured_llm,
            llm,
            formatted_messages,
            render_sentiment_report,
            "Sentiment Analyst",
        )

        return {
            "sentiment_messages": [AIMessage(content=report_text)],
            "sentiment_report": report_text,
        }

    return sentiment_analyst_node


def _build_system_message(
    *,
    ticker: str,
    start_date: str,
    end_date: str,
    news_block: str,
    stocktwits_block: str,
    reddit_block: str,
    trends_block: str,
) -> str:
    """Assemble the sentiment-analyst system message with structured data blocks."""
    return f"""You are a financial market sentiment analyst. Your task is to produce a comprehensive sentiment report for {ticker} covering the period from {start_date} to {end_date}, drawing on four complementary data sources that have already been collected for you.

## Data sources (pre-fetched, in this prompt)

### News headlines — Yahoo Finance, past 7 days
Institutional framing. Fact-driven, slower-moving signal.

<start_of_news>
{news_block}
<end_of_news>

### StockTwits messages — retail-trader social platform indexed by cashtag
Fast-moving signal. Each message carries a user-labeled sentiment tag (Bullish / Bearish / no-label) plus the message body.

<start_of_stocktwits>
{stocktwits_block}
<end_of_stocktwits>

### Reddit posts — r/wallstreetbets, r/stocks, r/investing (past 7 days)
Community discussion. Engagement signal via upvote score and comment count. Subreddit character matters (r/wallstreetbets is often contrarian/exuberant; r/stocks more measured; r/investing longer-term).

<start_of_reddit>
{reddit_block}
<end_of_reddit>

### Google Trends — relative search interest, past 90 days
Attention-volume signal from the broader public (not just traders). Values are scaled 0-100 relative to the peak within the window for this query — they measure how many people are searching, not whether they are bullish or bearish.

<start_of_google_trends>
{trends_block}
<end_of_google_trends>

## How to analyze this data (best practices)

1. **Read the StockTwits Bullish/Bearish ratio as a leading retail-sentiment signal.** A 70/30 bullish/bearish split is moderately bullish; ≥90/10 may indicate over-extension and contrarian risk; 50/50 is uncertainty. Sample size matters — base rates on the actual message count, not percentages alone.

2. **Look for cross-source divergences.** If news framing is bearish but StockTwits is overwhelmingly bullish, that mismatch is itself a signal — it can mean retail is leaning into a thesis the news flow hasn't caught up to (or vice versa, that retail is chasing while institutions are cautious).

3. **Weight Reddit posts by engagement.** A 400-upvote / 200-comment thread reflects community attention; a 3-upvote post is noise. Read the body excerpts for context — the title alone often misleads.

4. **Distinguish opinion from event.** A news headline ("Nvidia announces $500M Corning deal") is an event; a StockTwits post ("buying NVDA, this is going to moon") is opinion. Both are inputs but should be weighted differently in your conclusions.

5. **Identify recurring narrative themes.** What topic keeps coming up across sources? That's the dominant narrative driving current sentiment.

6. **Read Google Trends as attention, not direction.** Search interest spiking alongside bullish social chatter confirms a crowded, momentum-driven narrative; spiking alongside bearish news usually means fear or capitulation is drawing eyeballs. Flat interest while social sentiment runs hot suggests the story has not reached the broader public yet. Never map a high interest value to bullishness by itself, and remember values are relative to the 90-day peak, not absolute volumes (weekend dips in equity queries are normal).

7. **Be honest about data limits.** If StockTwits returned only a handful of messages, or one or more sources returned an "<unavailable>" placeholder, the sentiment read is less robust — flag this explicitly in the `confidence` field and the narrative. If the sources are silent on a given subreddit, say so.

8. **Identify catalysts and risks** that emerge across sources — news of upcoming earnings, product launches, competitive threats, macro headlines, etc.

9. **Past sentiment is not predictive.** Frame your conclusions as signal for the trader to weigh alongside fundamentals and technicals, not as a price call.

## Output fields

Fill the following fields:

- **overall_band**: Exactly one of Bullish / Mildly Bullish / Neutral / Mixed / Mildly Bearish / Bearish. Use Mixed when sources point in clearly different directions; Neutral only when all sources are genuinely silent.
- **overall_score**: A number from 0 (maximally bearish) to 10 (maximally bullish); 5 is neutral. Keep it consistent with overall_band.
- **confidence**: low / medium / high, based on data quality and sample size.
- **narrative**: Full source-by-source breakdown, divergences, dominant narrative themes, catalysts and risks, and a markdown summary table of key sentiment signals (direction, source, supporting evidence).

{get_language_instruction()}"""


# ---------------------------------------------------------------------------
# Chinese-market sentiment prompt (A-share tickers: .SS / .SZ / .BJ)
# ---------------------------------------------------------------------------
def _build_chinese_system_message(
    *,
    ticker: str,
    start_date: str,
    end_date: str,
    news_block: str,
    blocks: dict,
) -> str:
    """A-share variant — swaps StockTwits/Reddit/Google Trends for the five
    Chinese-market sources fetched by ``dataflows.chinese_sentiment``.
    """
    return f"""You are a financial market sentiment analyst covering the Chinese A-share market. Produce a comprehensive sentiment report for {ticker} covering {start_date} to {end_date}, drawing on the news headlines plus five China-specific data sources that have already been collected for you.

## Data sources (pre-fetched, in this prompt)

### News headlines — Yahoo Finance, past 7 days
Institutional / English-language framing. May be sparse for A-shares; treat silence here as a data limit rather than a bearish signal.

<start_of_news>
{news_block}
<end_of_news>

### Baidu Index — search-attention snapshot (via chinaz mirror)
Baidu is the dominant Chinese search engine. Higher index = more retail attention. Attention only, not direction.

<start_of_baidu_index>
{blocks["baidu_index"]}
<end_of_baidu_index>

### Baidu News — media-coverage volume + sample headlines
Proxy for how heavily Chinese media are covering this name right now. High volume + negative-tone headlines is a risk signal; high volume with neutral/positive framing usually reflects an active narrative (earnings, policy, product).

<start_of_baidu_news>
{blocks["baidu_news"]}
<end_of_baidu_news>

### Weibo Hot Search — real-time trending board
Being on Weibo's real-time hot-search board is a very strong retail-buzz signal, but directionally ambiguous — a stock trends on Weibo for scandals as often as for good news. Cross-check against news framing.

<start_of_weibo_hot>
{blocks["weibo_hot"]}
<end_of_weibo_hot>

### Xueqiu (雪球) — quality retail-analyst discussion
Xueqiu is China's investor-focused social platform; posts are generally more analytical than pure retail chatter. Engagement counts (reply/retweet/like) after the [@user] tag indicate community traction.

<start_of_xueqiu>
{blocks["xueqiu"]}
<end_of_xueqiu>

### Eastmoney Guba (股吧) — highest-volume retail forum, per-stock board
Guba is the noisiest, most retail-heavy source. Post-volume and read/reply counts matter more than any single title. Treat individual claims skeptically; look for repeated themes across many posts.

<start_of_guba>
{blocks["guba"]}
<end_of_guba>

## How to analyze this data (best practices for A-share sentiment)

1. **Xueqiu is higher-signal than Guba.** Xueqiu discussions are semi-professional; weight them more heavily. Guba's value is in aggregate volume and recurring themes, not any single post.

2. **Baidu Index is attention, not direction.** A rising index only tells you retail is looking at this name — cross-reference with news framing and Guba tone to decide whether that attention is bullish or fear-driven.

3. **Weibo hot-search trending is a rare and powerful signal.** Most stocks never appear. When one does, it usually reflects either a strong catalyst or a scandal — never assume direction; read the news block and the trending title text together.

4. **Read Baidu News as coverage volume.** A day with 5x the usual coverage volume for this name almost always means something material happened (earnings, regulatory, M&A). If the news_block from Yahoo doesn't reflect this, note the divergence — English-language news often lags China-market events by hours or days.

5. **Guba post themes matter more than individual posts.** Retail forum posts are frequently promotional or emotional. What matters is: are 50% of posts talking about the same topic (e.g. an upcoming earnings, a rumored acquisition, a regulatory concern)? That's the dominant retail narrative.

6. **Be honest about data limits.** Any source showing an ``<... unavailable: ...>`` placeholder should be called out explicitly in the confidence field and narrative. English news often has very thin A-share coverage — say so if that's what you see.

7. **Identify catalysts and risks** that emerge across sources — earnings, policy shifts (证监会/PBOC), sector rotations, macro news specific to China.

8. **Past sentiment is not predictive.** Frame conclusions as signal for the trader to weigh alongside fundamentals and technicals.

## Output fields

Fill the following fields:

- **overall_band**: Exactly one of Bullish / Mildly Bullish / Neutral / Mixed / Mildly Bearish / Bearish. Use Mixed when sources point in clearly different directions; Neutral only when all sources are genuinely silent.
- **overall_score**: A number from 0 (maximally bearish) to 10 (maximally bullish); 5 is neutral. Keep it consistent with overall_band.
- **confidence**: low / medium / high, based on data quality and sample size.
- **narrative**: Full source-by-source breakdown, divergences, dominant narrative themes, catalysts and risks, and a markdown summary table of key sentiment signals (direction, source, supporting evidence).

{get_language_instruction()}"""


# ---------------------------------------------------------------------------
# Backwards-compatibility shim
# ---------------------------------------------------------------------------
def create_social_media_analyst(llm):
    """Deprecated alias for :func:`create_sentiment_analyst`.

    Kept so existing code that imports ``create_social_media_analyst``
    continues to work.

    .. deprecated::
        Import :func:`create_sentiment_analyst` directly instead.
    """
    import warnings
    warnings.warn(
        "create_social_media_analyst is deprecated and will be removed in a "
        "future version. Use create_sentiment_analyst instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    return create_sentiment_analyst(llm)
