import json
import asyncio
import os
import logging
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import aiohttp
from dotenv import load_dotenv

log = logging.getLogger(__name__)

_configured = False
LLM_API_URL = ""
LLM_API_KEY = ""
LLM_MODEL = ""
BUDGET_MIN = 10000
BUDGET_MAX = 15000
OUTPUT_FILE = "extracted.jsonl"

_executor = ThreadPoolExecutor(max_workers=3)


def init_config(env_path: str | Path | None = None):
    global _configured, LLM_API_URL, LLM_API_KEY, LLM_MODEL
    global BUDGET_MIN, BUDGET_MAX, OUTPUT_FILE
    if _configured:
        return
    if env_path is None:
        env_path = Path(__file__).parent.parent / ".env"
    load_dotenv(env_path)
    LLM_API_URL = os.environ.get("LLM_API_URL", os.environ.get("COMPASS_URL", "http://localhost:4000/v1/chat/completions"))
    LLM_API_KEY = os.environ.get("LLM_API_KEY", os.environ.get("COMPASS_API_KEY", ""))
    LLM_MODEL = os.environ.get("LLM_MODEL", os.environ.get("COMPASS_MODEL", "gpt-5"))
    BUDGET_MIN = int(os.environ.get("BUDGET_MIN", "10000"))
    BUDGET_MAX = int(os.environ.get("BUDGET_MAX", "15000"))
    OUTPUT_FILE = os.environ.get("OUTPUT_FILE", "extracted.jsonl")
    _configured = True


EXTRACTION_PROMPT = """You analyze messages from Telegram investment/trading channels about the US stock market.
Extract any stock ticker symbols and recommendation details.

Return ONLY valid JSON:
{
  "tickers": ["AAPL"],
  "recommendation": "buy|sell|hold|unknown",
  "entry_price": null,
  "target_prices": [],
  "stop_loss": null,
  "timeframe": "short-term|medium-term|long-term|unknown",
  "raw_summary": "one line summary of the recommendation"
}

If the message has no stock/share recommendations, return:
{"tickers": [], "recommendation": "none", "raw_summary": "not a stock recommendation"}

Only return JSON, no extra text."""


def _get_analyst_prompt():
    init_config()
    max_per_position = round(BUDGET_MAX * 0.20)
    return f"""You are a senior US stock analyst advising a long-term growth investor. You evaluate business quality and compounding power FIRST, technicals only influence position sizing and entry timing.

PORTFOLIO BUDGET RULES (MANDATORY — violating these is an error):
- TOTAL budget for ALL investments combined: ${BUDGET_MIN:,}-${BUDGET_MAX:,}
- This is NOT per stock. The client diversifies across 5-10+ positions.
- MAX per single position: ${max_per_position:,} (20% of total budget)
- IDEAL position size: ${round(BUDGET_MAX * 0.10):,}-${max_per_position:,} (10-20% of total budget)
- Position sizing formula: min(available_capital * 0.20, ${max_per_position:,}) / current_price = max shares
- If available capital < $500, verdict MUST be WAIT or SKIP regardless of the opportunity
- If client already holds this ticker, verdict should be HOLD or SKIP (no doubling down)

You receive:
1. Original channel recommendation
2. Extracted recommendation details
3. Live market data (Yahoo Finance + technical indicators + fundamentals)
4. Current portfolio state (existing positions and capital deployed)

FUNDAMENTAL CHECK (evaluate FIRST — these carry the most weight):
- Is free cash flow (FCF) positive? Negative FCF on a mature company is a red flag.
- Is ROE > 15%? (quality threshold) Is it > 25%? (exceptional)
- Are operating margins stable or expanding? Compressing margins = deteriorating business.
- Is debt manageable? D/E > 200 for non-financials is concerning.
- Is revenue growing? Declining revenue + high valuation = AVOID regardless of technicals.
- Quality compounders trading at premium multiples are NORMAL. Only penalize valuation when it's extreme relative to growth rate (PEG > 3).

CRITICAL — TECHNICALS AFFECT SIZING, NOT THE VERDICT:
- A stock with strong fundamentals but weak technicals is still BUY-worthy at reduced size (SCALE-IN), not AVOID or WAIT. Technicals determine HOW MUCH to buy, not WHETHER to buy.
- A stock with weak fundamentals but strong technicals is SPECULATIVE — size very conservatively.
- RSI < 30 on a quality business is often an opportunity (temporary washout), not a reject signal.
- Negative MACD alone should never override strong fundamentals.

STOP-LOSS (ATR-based):
- Use ATR(14) if available: stop = entry - 2 × ATR(14)
- High-beta stocks (beta > 1.5): entry - 2.5 × ATR(14)
- Low-vol quality (beta < 0.8): entry - 1.5 × ATR(14)
- Never wider than -15% from entry
- If ATR unavailable, use -8% default

Evaluate the investment opportunity:
- Is the recommendation sound given fundamentals? Technicals only adjust position size.
- Calculate position sizing from REMAINING available capital
- Size volatile stocks (high beta, high ATR) SMALLER, not larger
- For SCALE-IN: use 40-60% of normal position size; include a scale_in_target price
- Assess risk/reward ratio using ATR-based stop
- Consider sector diversification with existing holdings

Return ONLY valid JSON:
{{
  "ticker": "AAPL",
  "company_name": "Apple Inc.",
  "verdict": "BUY|SCALE-IN|SELL|HOLD|SKIP",
  "confidence": "high|medium|low",
  "fundamental_quality": "strong|moderate|weak|insufficient_data",
  "current_price": 150.00,
  "channel_entry": 148.00,
  "suggested_entry": 148.00,
  "suggested_target": 165.00,
  "suggested_stop_loss": 142.00,
  "stop_method": "2x ATR(3.45) from entry",
  "scale_in_target": null,
  "position_size_shares": 10,
  "estimated_cost": 1500.00,
  "portfolio_allocation_pct": 10.0,
  "remaining_budget_after": 8500.00,
  "risk_reward_ratio": "1:2.1",
  "summary": "2-3 sentence analysis leading with fundamental quality, then technical timing",
  "technical_outlook": "1-2 sentences on RSI, MACD, trend, TradingView consensus",
  "fundamental_outlook": "1-2 sentences on FCF, ROE, margins, revenue growth, debt",
  "risks": ["risk1", "risk2"],
  "action": "alert"
}}

action rules:
- "alert" for BUY or SCALE-IN or SELL with medium/high confidence
- "log" for HOLD or low confidence
- "ignore" for SKIP or not relevant

Only return JSON."""


def _get_optimizer_prompt():
    init_config()
    max_per_position = round(BUDGET_MAX * 0.20)
    return f"""You are a portfolio optimizer for a US stock investor. You make decisions like an institutional portfolio manager — fundamentals first, technicals second.

DATA KEY LEGEND:
P=Price, MC=MarketCap, S=Sector, B=Beta, PE=TrailingPE, FPE=ForwardPE, PEG=PEG ratio, PB=PriceToBook,
FCF=FreeCashFlow, RG=RevenueGrowth, EG=EarningsGrowth, ROE=ReturnOnEquity, OM=OperatingMargins,
DE=DebtToEquity, RSI=RSI(14), ATR=ATR(14), MACD_H=MACD histogram, TV=TradingView consensus,
AN=Analyst consensus, TP=Analyst target price, SF=Short% of float. Null means data unavailable.

PORTFOLIO BUDGET RULES (MANDATORY):
- TOTAL budget for ALL investments: ${BUDGET_MIN:,}-${BUDGET_MAX:,}
- MAX per single position: ${max_per_position:,} (20% of total budget)
- These limits apply to the TOTAL portfolio, not just this allocation

MULTI-FACTOR SCORING MODEL (score each candidate 0-100):

1. FUNDAMENTAL QUALITY (40 points):
   - FCF yield (FCF/MarketCap): >5% exceptional (10pts), >3% strong (7pts), >0 adequate (4pts), negative (0pts)
   - ROE: >25% exceptional (10pts), >15% quality (7pts), >8% adequate (4pts), <8% weak (0pts)
   - Revenue growth + Earnings growth: both positive and >10% (10pts), both positive (6pts), mixed (3pts), both negative (0pts)
   - Operating margins: >20% strong (5pts), >10% adequate (3pts), <10% weak (1pt)
   - Balance sheet: D/E <50 strong (5pts), D/E <100 adequate (3pts), D/E >200 for non-financials (0pts)
   - If fundamental data is null/unavailable, score that criterion 0 (unknown, not penalized)

2. VALUATION (25 points):
   - PEG ratio: <1 undervalued (8pts), <1.5 fair (5pts), <2.5 growth premium (3pts), >2.5 expensive (0pts)
   - Forward P/E vs sector norms: below sector avg (7pts), at avg (4pts), significantly above (1pt)
   - Price-to-book: <3 reasonable (5pts), 3-8 growth premium (3pts), >8 speculative (1pt)
   - Upside to analyst target (TP vs P): >15% upside (5pts), 5-15% (3pts), <5% or no target (1pt)

3. TECHNICAL MOMENTUM (20 points):
   - RSI zone: 40-60 ideal entry (7pts), 30-40 or 60-70 acceptable (5pts), <30 oversold-caution (3pts), >70 overbought-risk (1pt)
   - MACD histogram: positive and rising (5pts), positive but fading (3pts), negative (1pt)
   - TradingView + Analyst consensus: STRONG_BUY (4pts), BUY (3pts), NEUTRAL (1pt), SELL (0pts)
   - Price momentum: above 52w midpoint (4pts), below midpoint but above low (2pts), near 52w low (1pt)

4. RISK DEDUCTIONS (up to -15 points):
   - Beta >1.8 on speculative stock (no positive FCF): -5pts
   - Short float >10%: -3pts
   - Negative FCF + high P/E (>30): -5pts
   - D/E >200 for non-financials: -3pts
   - Declining revenue + declining earnings: -5pts

SCREENING RULES:
- Score >= 60: BUY candidate
- Score 40-59: WAIT (not bad, but not compelling enough)
- Score < 40: AVOID
- Classify each stock with a quality_tag: COMPOUNDER (high ROE + margins + growth), VALUE (low PE/PB + FCF), GROWTH (high revenue growth + momentum), CYCLICAL (sector-driven), SPECULATIVE (high beta, no FCF, momentum-dependent), TURNAROUND (improving from weak base)

ALLOCATION RULES:
- Only allocate to BUY-verdict stocks (score >= 60)
- No single stock > 30% of investment amount or > ${max_per_position:,} total position
- If investor already holds a stock, reduce weight or skip
- Max 40% of investment in one sector
- Minimum allocation per stock: $200
- Higher-scoring stocks get proportionally larger allocations
- Volatile stocks (beta >1.5 or high ATR) get SMALLER positions, not larger
- Volatility cap: if ATR available, max position risk = 3% of investment amount (shares × ATR × 10 < amount × 0.03)
- Share counts must be whole numbers (round down)
- allocated_amount = shares × current_price (recalculate after rounding)
- Sum must NOT exceed investment amount; remainder goes to cash_reserve

STOP-LOSS (ATR-based, NOT arbitrary):
- Default: entry - 2 × ATR(14)
- High-beta stocks (beta > 1.5): entry - 2.5 × ATR(14)
- Low-vol quality (beta < 0.8): entry - 1.5 × ATR(14)
- Never wider than -15% from entry
- If ATR unavailable, use -8% default

CASH DEPLOYMENT RULES:
- 0 stocks score >= 60: keep 100% cash, explain why
- 1-2 stocks score >= 60: deploy 40-60%, keep rest for better entries
- 3-5 stocks score >= 60: deploy 60-80%
- 6+ stocks score >= 60: deploy 75-90%
- Always keep minimum 5% cash reserve
- State the cash deployment rationale explicitly

CORRELATION CHECK (MANDATORY):
- Count how many BUY stocks share the same macro dependency (AI spending, rate-sensitive, cyclical)
- If >50% of allocated stocks are in one sector: flag as concentrated
- If >60% have beta >1.2: flag as momentum-concentrated
- Explicitly state correlation risk in the summary

MACRO AWARENESS:
- Infer the market regime from the basket data: if most stocks have high P/E + high RSI, market is likely extended
- If basket is growth-heavy, note rate sensitivity
- If basket is cyclical-heavy, note economic cycle dependency
- Include regime observations in strategy_note

Return ONLY valid JSON:
{{
  "screened": [
    {{
      "ticker": "AAPL",
      "company": "Apple Inc.",
      "verdict": "BUY",
      "score": 78,
      "score_breakdown": {{"fundamental": 34, "valuation": 20, "technical": 16, "risk_deductions": -2}},
      "quality_tag": "COMPOUNDER",
      "sector": "Technology",
      "current_price": 195.50,
      "reason": "Strong FCF yield 5.2%, ROE 28%, growing revenue; fair PEG at 2.1 with bullish trend"
    }}
  ],
  "allocations": [
    {{
      "ticker": "AAPL",
      "company": "Apple Inc.",
      "sector": "Technology",
      "quality_tag": "COMPOUNDER",
      "current_price": 195.50,
      "shares": 8,
      "allocated_amount": 1564.00,
      "weight_pct": 31.3,
      "conviction": "high",
      "entry_target": 194.00,
      "stop_loss": 185.00,
      "stop_method": "2x ATR(3.45) from entry",
      "target_price": 215.00,
      "risk_pct": 2.8,
      "reason": "High-quality compounder with strong FCF and reasonable growth valuation"
    }}
  ],
  "excluded": [
    {{
      "ticker": "TSLA",
      "verdict": "AVOID",
      "score": 32,
      "reason": "Negative FCF, P/E >60, beta 2.1 — speculative with no fundamental support"
    }}
  ],
  "summary": {{
    "total_allocated": 4800.00,
    "cash_reserve": 200.00,
    "cash_rationale": "3 high-conviction picks (scores 72-85), deploying 80%; reserve for pullback entries",
    "num_stocks": 4,
    "sectors": ["Technology", "Healthcare", "Industrials"],
    "sector_concentration": "Technology 45% — acceptable given quality names with different end markets",
    "correlation_risk": "MODERATE — 3 of 4 picks are growth-sensitive; portfolio underperforms if rates spike",
    "portfolio_style": "Quality-Growth blend with Healthcare defensive anchor",
    "strategy_note": "Concentrated on high-ROE compounders with positive FCF. Market appears moderately extended (avg RSI 58, avg PE 28). Sized conservatively with ATR stops."
  }}
}}

Only return JSON."""


# ---------------------------------------------------------------------------
# ATR-based stop/target calculation
# ---------------------------------------------------------------------------

def calculate_atr_stop(entry_price: float, atr: float, beta: float = 1.0) -> tuple[float, str]:
    multiplier = 2.5 if beta > 1.5 else (1.5 if beta < 0.8 else 2.0)
    stop = round(entry_price - multiplier * atr, 2)
    stop = max(stop, round(entry_price * 0.85, 2))
    return stop, f"{multiplier}x ATR({atr:.2f})"


def calculate_default_target(entry_price: float, stop_loss: float) -> float:
    risk = entry_price - stop_loss
    return round(entry_price + risk * 1.5, 2)


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------

async def call_llm(messages: list[dict], max_tokens: int = 32000) -> str:
    init_config()
    async with aiohttp.ClientSession() as session:
        payload = {
            "model": LLM_MODEL,
            "messages": messages,
            "max_completion_tokens": max_tokens,
        }
        async with session.post(
            LLM_API_URL,
            headers={
                "Authorization": f"Bearer {LLM_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=aiohttp.ClientTimeout(total=300),
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                log.error("LLM API error %d: %s", resp.status, body)
                return json.dumps({"error": f"API {resp.status}", "body": body[:500]})
            result = await resp.json()
            content = result["choices"][0]["message"]["content"]
            if not content:
                log.warning("LLM returned empty content (reasoning model may need higher max_completion_tokens)")
            return content or ""


def parse_json_response(text: str) -> dict | None:
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end])
            except json.JSONDecodeError:
                return None
    return None


# ---------------------------------------------------------------------------
# Ticker resolution
# ---------------------------------------------------------------------------

US_EXCHANGES = {"NYQ", "NMS", "NGM", "NCM", "NYS", "NYSE", "NASDAQ", "BTS", "ASE"}


def resolve_company_ticker(company: str) -> str | None:
    """Resolve a company name to its US ticker symbol via Yahoo Finance search."""
    try:
        from yfinance import Search
        results = Search(company, max_results=5)
        for q in results.quotes:
            if q.get("quoteType") == "EQUITY" and q.get("exchange") in US_EXCHANGES:
                return q["symbol"]
        for q in results.quotes:
            if q.get("quoteType") == "EQUITY":
                return q["symbol"]
        if results.quotes:
            return results.quotes[0].get("symbol")
    except Exception as e:
        log.warning("Ticker search failed for '%s': %s", company, e)
    return None


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------

def fetch_yahoo_data(ticker: str) -> dict:
    try:
        import yfinance as yf
        stock = yf.Ticker(ticker)
        info = stock.info
        hist = stock.history(period="3mo")

        technicals: dict = {}
        if not hist.empty and len(hist) > 14:
            close = hist["Close"]
            delta = close.diff()
            gain = delta.where(delta > 0, 0).rolling(14).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
            rs = gain / loss
            rsi = 100 - (100 / (1 + rs))
            technicals["rsi_14"] = round(float(rsi.iloc[-1]), 1)
            if len(close) >= 20:
                technicals["ma_20"] = round(float(close.rolling(20).mean().iloc[-1]), 2)
            if len(close) >= 50:
                technicals["ma_50"] = round(float(close.rolling(50).mean().iloc[-1]), 2)
            ema12 = close.ewm(span=12).mean()
            ema26 = close.ewm(span=26).mean()
            macd = ema12 - ema26
            signal = macd.ewm(span=9).mean()
            technicals["macd"] = round(float(macd.iloc[-1]), 3)
            technicals["macd_signal"] = round(float(signal.iloc[-1]), 3)
            technicals["macd_histogram"] = round(float((macd - signal).iloc[-1]), 3)
            if len(hist) >= 15:
                import pandas as pd
                high, low = hist["High"], hist["Low"]
                prev_c = close.shift(1)
                tr = pd.concat([high - low, (high - prev_c).abs(), (low - prev_c).abs()], axis=1).max(axis=1)
                technicals["atr_14"] = round(float(tr.rolling(14).mean().iloc[-1]), 2)

        return {
            "ticker": ticker,
            "name": info.get("shortName", ticker),
            "current_price": info.get("currentPrice") or info.get("regularMarketPrice"),
            "previous_close": info.get("previousClose"),
            "open": info.get("open") or info.get("regularMarketOpen"),
            "day_high": info.get("dayHigh") or info.get("regularMarketDayHigh"),
            "day_low": info.get("dayLow") or info.get("regularMarketDayLow"),
            "fifty_two_week_high": info.get("fiftyTwoWeekHigh"),
            "fifty_two_week_low": info.get("fiftyTwoWeekLow"),
            "market_cap": info.get("marketCap"),
            "pe_ratio": info.get("trailingPE"),
            "forward_pe": info.get("forwardPE"),
            "eps": info.get("trailingEps"),
            "dividend_yield": info.get("dividendYield"),
            "volume": info.get("volume") or info.get("regularMarketVolume"),
            "avg_volume": info.get("averageVolume"),
            "sector": info.get("sector"),
            "industry": info.get("industry"),
            "beta": info.get("beta"),
            "free_cashflow": info.get("freeCashflow"),
            "revenue_growth": info.get("revenueGrowth"),
            "earnings_growth": info.get("earningsGrowth"),
            "return_on_equity": info.get("returnOnEquity"),
            "operating_margins": info.get("operatingMargins"),
            "debt_to_equity": info.get("debtToEquity"),
            "current_ratio": info.get("currentRatio"),
            "peg_ratio": info.get("pegRatio"),
            "price_to_book": info.get("priceToBook"),
            "recommendation_key": info.get("recommendationKey"),
            "target_mean_price": info.get("targetMeanPrice"),
            "short_pct_float": info.get("shortPercentOfFloat"),
            **technicals,
        }
    except Exception as e:
        log.error("Yahoo Finance error for %s: %s", ticker, e)
        return {"ticker": ticker, "error": str(e)}


def fetch_tradingview_data(ticker: str) -> dict:
    try:
        from tradingview_ta import TA_Handler, Interval
        for exchange in ("NASDAQ", "NYSE", "AMEX"):
            try:
                handler = TA_Handler(
                    symbol=ticker, screener="america",
                    exchange=exchange, interval=Interval.INTERVAL_1_DAY,
                )
                a = handler.get_analysis()
                return {
                    "exchange": exchange,
                    "recommendation": a.summary.get("RECOMMENDATION", ""),
                    "buy_signals": a.summary.get("BUY", 0),
                    "sell_signals": a.summary.get("SELL", 0),
                    "neutral_signals": a.summary.get("NEUTRAL", 0),
                    "oscillators": a.oscillators.get("RECOMMENDATION", ""),
                    "moving_averages": a.moving_averages.get("RECOMMENDATION", ""),
                }
            except Exception:
                continue
        return {"error": "ticker not found on US exchanges"}
    except ImportError:
        return {"error": "tradingview_ta not installed"}
    except Exception as e:
        log.warning("TradingView error for %s: %s", ticker, e)
        return {"error": str(e)}


async def get_market_data(ticker: str) -> dict:
    loop = asyncio.get_running_loop()
    yahoo, tv = await asyncio.gather(
        loop.run_in_executor(_executor, fetch_yahoo_data, ticker),
        loop.run_in_executor(_executor, fetch_tradingview_data, ticker),
    )
    return {"yahoo": yahoo, "tradingview": tv}


def _cap(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        if abs(v) >= 1e12:
            return f"{v / 1e12:.1f}T"
        if abs(v) >= 1e9:
            return f"{v / 1e9:.1f}B"
        if abs(v) >= 1e6:
            return f"{v / 1e6:.0f}M"
    return v


def compact_market_data(market_data: dict) -> dict:
    y = market_data.get("yahoo", {})
    tv = market_data.get("tradingview", {})
    return {
        "P": y.get("current_price"),
        "MC": _cap(y.get("market_cap")),
        "S": y.get("sector"),
        "B": y.get("beta"),
        "PE": y.get("pe_ratio"),
        "FPE": y.get("forward_pe"),
        "PEG": y.get("peg_ratio"),
        "PB": y.get("price_to_book"),
        "FCF": _cap(y.get("free_cashflow")),
        "RG": y.get("revenue_growth"),
        "EG": y.get("earnings_growth"),
        "ROE": y.get("return_on_equity"),
        "OM": y.get("operating_margins"),
        "DE": y.get("debt_to_equity"),
        "RSI": y.get("rsi_14"),
        "ATR": y.get("atr_14"),
        "MACD_H": y.get("macd_histogram"),
        "TV": tv.get("recommendation"),
        "AN": y.get("recommendation_key"),
        "TP": y.get("target_mean_price"),
        "SF": y.get("short_pct_float"),
    }


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

async def analyze_ticker(extraction: dict, market_data: dict, original_text: str) -> dict | None:
    init_config()
    from core.database import get_portfolio_summary
    portfolio = get_portfolio_summary()
    available = BUDGET_MAX - portfolio["total_invested"]
    portfolio_info = (
        f"CURRENT PORTFOLIO STATE:\n"
        f"- Total budget: ${BUDGET_MIN:,} - ${BUDGET_MAX:,}\n"
        f"- Already invested: ${portfolio['total_invested']:,.2f} across {portfolio['position_count']} positions\n"
        f"- Available capital: ${available:,.2f}\n"
        f"- Current holdings: {', '.join(portfolio['tickers']) if portfolio['tickers'] else 'None'}\n"
    )
    context = (
        f"ORIGINAL CHANNEL MESSAGE:\n{original_text[:3000]}\n\n"
        f"EXTRACTED RECOMMENDATION:\n{json.dumps(extraction, indent=2)}\n\n"
        f"LIVE MARKET DATA:\n{json.dumps(market_data, indent=2, default=str)}\n\n"
        f"{portfolio_info}"
    )
    resp = await call_llm([
        {"role": "system", "content": _get_analyst_prompt()},
        {"role": "user", "content": context},
    ])
    return parse_json_response(resp)


async def optimize_allocation(
    amount: float,
    tickers: list[str],
    market_data_dict: dict[str, dict],
) -> dict | None:
    init_config()
    from core.database import get_portfolio_summary
    portfolio = get_portfolio_summary()
    available = BUDGET_MAX - portfolio["total_invested"]
    holdings = ", ".join(
        f"{p['ticker']} ({p['shares']} sh @ ${p['entry_price']:.2f})"
        for p in portfolio["positions"]
    ) or "None"
    portfolio_info = (
        f"CURRENT PORTFOLIO STATE:\n"
        f"- Total budget: ${BUDGET_MIN:,} - ${BUDGET_MAX:,}\n"
        f"- Already invested: ${portfolio['total_invested']:,.2f} across {portfolio['position_count']} positions\n"
        f"- Available capital: ${available:,.2f}\n"
        f"- Current holdings: {holdings}\n"
    )
    compact = {t: compact_market_data(md) for t, md in market_data_dict.items()}
    context = (
        f"INVESTMENT AMOUNT: ${amount:,.2f}\n\n"
        f"CANDIDATE STOCKS ({len(tickers)}):\n"
        f"{json.dumps(compact, default=str)}\n\n"
        f"{portfolio_info}\n"
        f"Score each candidate using the multi-factor model, then optimize the allocation of ${amount:,.2f} across BUY-worthy candidates."
    )
    resp = await call_llm([
        {"role": "system", "content": _get_optimizer_prompt()},
        {"role": "user", "content": context},
    ], max_tokens=65000)
    log.info("Optimizer LLM response length: %d chars", len(resp))
    return parse_json_response(resp)


# ---------------------------------------------------------------------------
# Alert formatting
# ---------------------------------------------------------------------------

def _fmt(n) -> str:
    if n is None:
        return "N/A"
    if isinstance(n, (int, float)):
        if abs(n) >= 1_000_000_000_000:
            return f"${n / 1_000_000_000_000:.1f}T"
        if abs(n) >= 1_000_000_000:
            return f"${n / 1_000_000_000:.1f}B"
        if abs(n) >= 1_000_000:
            return f"${n / 1_000_000:.1f}M"
        return f"${n:,.2f}"
    return str(n)


def format_alert_message(analysis: dict, market_data: dict, channel: str) -> str:
    y = market_data.get("yahoo", {})
    tv = market_data.get("tradingview", {})
    ticker = analysis.get("ticker", "???")
    name = analysis.get("company_name") or y.get("name", ticker)
    verdict = analysis.get("verdict", "???")
    verdict_icon = {"BUY": "✅", "SCALE-IN": "\U0001f504", "SELL": "\U0001f534", "HOLD": "⏸", "SKIP": "⏭"}.get(verdict, "❓")

    price = y.get("current_price")
    prev = y.get("previous_close")
    change = ""
    if price and prev:
        pct = ((price - prev) / prev) * 100
        arrow = "▲" if pct >= 0 else "▼"
        change = f" ({arrow} {pct:+.1f}%)"

    rsi = y.get("rsi_14")
    rsi_lbl = ""
    if rsi is not None:
        rsi_lbl = "Overbought" if rsi > 70 else ("Oversold" if rsi < 30 else "Neutral")

    rsi_str = f"{rsi} ({rsi_lbl})" if rsi else "N/A"

    macd_h = y.get("macd_histogram")
    macd_val = y.get("macd")
    macd_lbl = ("▲ Bullish" if macd_h > 0 else "▼ Bearish") if macd_h is not None else ""
    macd_str = f"{macd_val} ({macd_lbl})" if macd_lbl else "N/A"

    tv_rec = tv.get("recommendation", "")
    tv_str = (f"{tv_rec} ({tv.get('buy_signals', '?')} Buy / {tv.get('sell_signals', '?')} Sell)"
              if tv_rec else "N/A")

    risks = "\n".join(f"• {r}" for r in analysis.get("risks", [])) or "• None identified"

    return _sanitize_html(
        f"\U0001f4ca <b>SHARE ANALYSIS — {ticker} ({name})</b>\n"
        f"\n"
        f"{verdict_icon} <b>Verdict: {verdict}</b> (Confidence: {analysis.get('confidence', 'N/A').upper()})\n"
        f"<b>Summary:</b> {analysis.get('summary', 'N/A')}\n"
        f"• Entry: {_fmt(analysis.get('suggested_entry'))} | Target: {_fmt(analysis.get('suggested_target'))} | Stop: {_fmt(analysis.get('suggested_stop_loss'))}\n"
        f"• Position: {analysis.get('position_size_shares', 'N/A')} shares (~{_fmt(analysis.get('estimated_cost'))}) — {analysis.get('portfolio_allocation_pct', 'N/A')}% of portfolio\n"
        f"• Risk/Reward: {analysis.get('risk_reward_ratio', 'N/A')}\n"
        f"\n"
        f"\U0001f4c8 <b>Market Data:</b>\n"
        f"• Price: {_fmt(price)}{change}\n"
        f"• 52W: {_fmt(y.get('fifty_two_week_low'))} — {_fmt(y.get('fifty_two_week_high'))}\n"
        f"• P/E: {y.get('pe_ratio', 'N/A')} | Fwd P/E: {y.get('forward_pe', 'N/A')}\n"
        f"• Cap: {_fmt(y.get('market_cap'))}\n"
        f"• Vol: {_fmt(y.get('volume'))} (Avg: {_fmt(y.get('avg_volume'))})\n"
        f"• Sector: {y.get('sector', 'N/A')}\n"
        f"\n"
        f"\U0001f4c9 <b>Technicals:</b>\n"
        f"• RSI(14): {rsi_str}\n"
        f"• MACD: {macd_str}\n"
        f"• 20-MA: {_fmt(y.get('ma_20'))} | 50-MA: {_fmt(y.get('ma_50'))}\n"
        f"• TradingView: {tv_str}\n"
        f"\n"
        f"\U0001f4dd <b>Source:</b> {channel}\n"
        f"\n"
        f"⚠️ <b>Risks:</b>\n"
        f"{risks}"
    )


def format_invest_message(result: dict, amount: float) -> str:
    verdict_icons = {"BUY": "✅", "SCALE-IN": "\U0001f504", "WAIT": "⏸", "AVOID": "\U0001f534"}
    tag_icons = {"COMPOUNDER": "\U0001f48e", "VALUE": "\U0001f3af", "GROWTH": "\U0001f680",
                 "CYCLICAL": "\U0001f504", "SPECULATIVE": "\U0001f3b2", "TURNAROUND": "\U0001f504"}
    lines = [f"\U0001f4b0 <b>INVESTMENT PLAN — {_fmt(amount)}</b>\n"]

    screened = result.get("screened", [])
    excluded = result.get("excluded", [])
    seen_tickers = {s.get("ticker") for s in screened}
    all_candidates = screened + [e for e in excluded if e.get("ticker") not in seen_tickers]
    buy_count = sum(1 for s in all_candidates if s.get("verdict") == "BUY")
    scale_count = sum(1 for s in all_candidates if s.get("verdict") == "SCALE-IN")
    screen_label = f"{buy_count} BUY"
    if scale_count:
        screen_label += f" + {scale_count} SCALE-IN"
    lines.append(f"\U0001f4ca <b>SCREENING</b> ({len(all_candidates)} candidates → {screen_label})\n")
    for s in all_candidates:
        v = s.get("verdict", "")
        icon = verdict_icons.get(v, "❓")
        ticker = s.get("ticker", "???")
        company = s.get("company", "")
        sector = s.get("sector", "")
        score = s.get("score")
        tag = s.get("quality_tag", "")
        label = f"{ticker}"
        if company:
            label += f" — {company}"
        if sector:
            label += f" ({sector})"
        score_str = f" [{score}/100]" if score is not None else ""
        tag_str = f" {tag_icons.get(tag, '')}{tag}" if tag else ""
        lines.append(f"{icon} <b>{label}</b>{score_str}{tag_str}")
        reason = s.get("reason", "")
        if reason:
            lines.append(f"   {reason}")

    allocations = result.get("allocations", [])
    if allocations:
        lines.append(f"\n\U0001f4c8 <b>ALLOCATION PLAN</b>\n")
        for i, a in enumerate(allocations, 1):
            ticker = a.get("ticker", "???")
            company = a.get("company", "")
            alloc = a.get("allocated_amount", 0)
            pct = a.get("weight_pct", 0)
            shares = a.get("shares", 0)
            price = a.get("current_price", 0)
            conviction = a.get("conviction", "").upper()
            entry = a.get("entry_target")
            stop = a.get("stop_loss")
            target = a.get("target_price")
            tag = a.get("quality_tag", "")
            stop_method = a.get("stop_method", "")
            risk_pct = a.get("risk_pct")

            header = f"{i}. <b>{ticker}</b>"
            if company:
                header += f" — {company}"
            if tag:
                header += f" {tag_icons.get(tag, '')}{tag}"
            lines.append(header)
            lines.append(f"   \U0001f4b5 {_fmt(alloc)} ({pct:.1f}%) → {shares} shares @ {_fmt(price)}")
            price_line = []
            if entry:
                price_line.append(f"Entry: {_fmt(entry)}")
            if stop:
                stop_str = _fmt(stop)
                if stop_method:
                    stop_str += f" ({stop_method})"
                price_line.append(f"Stop: {stop_str}")
            if target:
                price_line.append(f"Target: {_fmt(target)}")
            if price_line:
                lines.append(f"   {' | '.join(price_line)}")
            meta = []
            if conviction:
                meta.append(f"Conviction: {conviction}")
            if risk_pct is not None:
                meta.append(f"Risk: {risk_pct:.1f}%")
            if meta:
                lines.append(f"   {' | '.join(meta)}")
            scale_target = a.get("scale_in_target")
            if scale_target:
                lines.append(f"   \U0001f504 Scale to full at {_fmt(scale_target)}")
            reason = a.get("reason", "")
            if reason:
                lines.append(f"   {reason}")

    summary = result.get("summary", {})
    if summary:
        lines.append(f"\n\U0001f4cb <b>SUMMARY</b>")
        total = summary.get("total_allocated", 0)
        cash = summary.get("cash_reserve", 0)
        num = summary.get("num_stocks", 0)
        sectors = summary.get("sectors", [])
        style = summary.get("portfolio_style", "")
        note = summary.get("strategy_note", "")
        cash_rationale = summary.get("cash_rationale", "")
        correlation = summary.get("correlation_risk", "")
        sector_conc = summary.get("sector_concentration", "")

        lines.append(f"Allocated: {_fmt(total)} of {_fmt(amount)} ({num} stocks)")
        if cash > 0:
            cash_line = f"Cash reserve: {_fmt(cash)}"
            if cash_rationale:
                cash_line += f" — {cash_rationale}"
            lines.append(cash_line)
        if sectors:
            sector_line = f"Sectors: {', '.join(sectors)}"
            if sector_conc:
                sector_line += f"\n   {sector_conc}"
            lines.append(sector_line)
        if correlation:
            lines.append(f"Correlation: {correlation}")
        narrative = summary.get("narrative_breakdown", "")
        if narrative:
            lines.append(f"Narratives: {narrative}")
        if style:
            lines.append(f"Style: {style}")
        if note:
            lines.append(f"Strategy: {note}")

    return _sanitize_html("\n".join(lines))


def _sanitize_html(text: str) -> str:
    """Escape stray < > & in text while preserving intentional <b> <i> <code> <pre> <u> <s> tags.
    Idempotent: safe to apply to already-sanitized text (won't double-escape entities)."""
    import re
    # escape & only when it is NOT already the start of an HTML entity
    text = re.sub(r"&(?!(?:amp|lt|gt|quot|#\d+|#x[0-9a-fA-F]+);)", "&amp;", text)
    text = text.replace("<", "&lt;").replace(">", "&gt;")
    for tag in ("b", "i", "code", "pre", "u", "s"):
        text = text.replace(f"&lt;{tag}&gt;", f"<{tag}>")
        text = text.replace(f"&lt;/{tag}&gt;", f"</{tag}>")
    return text


# ---------------------------------------------------------------------------
# History search
# ---------------------------------------------------------------------------

def read_analysis_history(
    ticker: str | None = None,
    limit: int = 10,
    alerts_only: bool = False,
) -> list[dict]:
    init_config()
    path = Path(OUTPUT_FILE)
    if not path.is_absolute():
        path = Path(__file__).parent.parent / "data" / path
    if not path.exists():
        return []

    results = []
    for line in reversed(path.read_text().splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ticker and entry.get("ticker", "").upper() != ticker.upper():
            continue
        if alerts_only:
            action = entry.get("analysis", {}).get("action", "")
            if action != "alert":
                continue
        results.append(entry)
        if len(results) >= limit:
            break
    return results
