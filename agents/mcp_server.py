import sys
import json
import asyncio
import logging
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp.server.fastmcp import FastMCP

from core.finance import (
    init_config,
    fetch_yahoo_data,
    fetch_tradingview_data,
    get_market_data,
    read_analysis_history,
    calculate_atr_stop,
    calculate_default_target,
    _fmt,
)
from core import database as db

logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

init_config()
db.init_db()

mcp = FastMCP("finance", instructions="US stock market data, technical analysis, and portfolio management tools.")
_executor = ThreadPoolExecutor(max_workers=3)


@mcp.tool()
async def get_stock_quote(ticker: str) -> str:
    """Get current stock price and fundamental data from Yahoo Finance.
    Returns price, P/E ratio, market cap, 52-week range, volume, sector, and more."""
    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(_executor, fetch_yahoo_data, ticker.upper())
    return json.dumps(data, indent=2, default=str)


@mcp.tool()
async def get_technical_analysis(ticker: str) -> str:
    """Get technical indicators for a stock: RSI(14), MACD, 20/50-day MAs from Yahoo Finance,
    plus TradingView buy/sell/neutral signal counts and consensus recommendation."""
    loop = asyncio.get_running_loop()
    yahoo, tv = await asyncio.gather(
        loop.run_in_executor(_executor, fetch_yahoo_data, ticker.upper()),
        loop.run_in_executor(_executor, fetch_tradingview_data, ticker.upper()),
    )
    technicals = {
        "ticker": ticker.upper(),
        "rsi_14": yahoo.get("rsi_14"),
        "macd": yahoo.get("macd"),
        "macd_signal": yahoo.get("macd_signal"),
        "macd_histogram": yahoo.get("macd_histogram"),
        "ma_20": yahoo.get("ma_20"),
        "ma_50": yahoo.get("ma_50"),
        "current_price": yahoo.get("current_price"),
        "tradingview": tv,
    }
    return json.dumps(technicals, indent=2, default=str)


@mcp.tool()
async def get_full_analysis(ticker: str) -> str:
    """Get complete market data for a stock: Yahoo Finance fundamentals + technicals,
    and TradingView signals combined."""
    data = await get_market_data(ticker.upper())
    return json.dumps(data, indent=2, default=str)


@mcp.tool()
async def search_analysis_history(ticker: str = "", limit: int = 10) -> str:
    """Search past stock analyses from the channel monitoring log.
    Filter by ticker symbol (optional). Returns extracted recommendations, market data, and verdicts."""
    results = read_analysis_history(
        ticker=ticker.upper() if ticker else None,
        limit=limit,
    )
    if not results:
        msg = f"No analysis history found"
        if ticker:
            msg += f" for {ticker.upper()}"
        return json.dumps({"message": msg, "results": []})
    return json.dumps({"count": len(results), "results": results}, indent=2, default=str)


@mcp.tool()
async def get_recent_alerts(limit: int = 5) -> str:
    """Get the most recent BUY/SELL alerts that were sent to Telegram.
    These are high-confidence recommendations from the channel monitoring agent."""
    results = read_analysis_history(alerts_only=True, limit=limit)
    if not results:
        return json.dumps({"message": "No alerts found yet", "results": []})
    return json.dumps({"count": len(results), "results": results}, indent=2, default=str)


@mcp.tool()
async def add_to_portfolio(
    ticker: str, shares: float, entry_price: float,
    stop_loss: float | None = None, target_price: float | None = None,
) -> str:
    """Add a stock position to the tracked portfolio with optional stop-loss and target levels.
    If stop_loss is omitted, it's auto-calculated from ATR. If target is also omitted, it defaults to 1.5x risk."""
    ticker = ticker.upper()
    loop = asyncio.get_running_loop()
    stop_method = None
    if stop_loss is None:
        yahoo = await loop.run_in_executor(_executor, fetch_yahoo_data, ticker)
        atr = yahoo.get("atr_14")
        beta = yahoo.get("beta", 1.0)
        if atr:
            stop_loss, stop_method = calculate_atr_stop(entry_price, atr, beta or 1.0)
            if target_price is None:
                target_price = calculate_default_target(entry_price, stop_loss)
    position_id = db.add_position(ticker, shares, entry_price, source="claude_code",
                                  stop_loss=stop_loss, target_price=target_price, stop_method=stop_method)
    cost = shares * entry_price
    result = {
        "status": "added",
        "position_id": position_id,
        "ticker": ticker,
        "shares": shares,
        "entry_price": entry_price,
        "total_cost": round(cost, 2),
    }
    if stop_loss:
        result["stop_loss"] = stop_loss
        if stop_method:
            result["stop_method"] = stop_method
    if target_price:
        result["target_price"] = target_price
    return json.dumps(result)


@mcp.tool()
async def set_position_levels(
    ticker: str, stop_loss: float | None = None, target_price: float | None = None,
) -> str:
    """Set stop-loss and/or target price for an existing position.
    Example: set_position_levels('NVDA', stop_loss=175, target_price=240)"""
    ticker = ticker.upper()
    db.update_position_levels(ticker, stop_loss=stop_loss, target_price=target_price)
    result = {"status": "updated", "ticker": ticker}
    if stop_loss is not None:
        result["stop_loss"] = stop_loss
    if target_price is not None:
        result["target_price"] = target_price
    return json.dumps(result)


@mcp.tool()
async def remove_from_portfolio(ticker: str) -> str:
    """Remove a stock from the tracked portfolio (closes all open positions for this ticker)."""
    ticker = ticker.upper()
    removed = db.remove_position(ticker)
    if removed:
        return json.dumps({"status": "removed", "ticker": ticker})
    return json.dumps({"status": "not_found", "ticker": ticker,
                       "message": f"No open position found for {ticker}"})


@mcp.tool()
async def get_portfolio() -> str:
    """Get the current portfolio with all open positions and live P&L calculations."""
    positions = db.get_portfolio()
    if not positions:
        return json.dumps({"message": "Portfolio is empty", "positions": [], "summary": {}})

    loop = asyncio.get_running_loop()
    enriched = []
    total_cost = 0.0
    total_value = 0.0

    for pos in positions:
        ticker = pos["ticker"]
        yahoo = await loop.run_in_executor(_executor, fetch_yahoo_data, ticker)
        current_price = yahoo.get("current_price")
        cost = pos["shares"] * pos["entry_price"]
        value = pos["shares"] * current_price if current_price else cost
        pnl = value - cost
        pnl_pct = (pnl / cost * 100) if cost > 0 else 0

        total_cost += cost
        total_value += value

        entry = {
            "ticker": ticker,
            "shares": pos["shares"],
            "entry_price": pos["entry_price"],
            "unit_cost": pos.get("unit_cost"),
            "current_price": current_price,
            "cost": round(cost, 2),
            "value": round(value, 2),
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 2),
            "entry_date": pos["entry_date"],
            "source": pos["source"],
            "stop_loss": pos.get("stop_loss"),
            "target_price": pos.get("target_price"),
            "stop_method": pos.get("stop_method"),
        }
        if pos.get("stop_loss") and current_price:
            entry["stop_distance_pct"] = round((current_price - pos["stop_loss"]) / current_price * 100, 1)
        if pos.get("target_price") and current_price:
            entry["target_distance_pct"] = round((pos["target_price"] - current_price) / current_price * 100, 1)
        enriched.append(entry)

    total_pnl = total_value - total_cost
    total_pnl_pct = (total_pnl / total_cost * 100) if total_cost > 0 else 0

    return json.dumps({
        "positions": enriched,
        "summary": {
            "total_cost": round(total_cost, 2),
            "total_value": round(total_value, 2),
            "total_pnl": round(total_pnl, 2),
            "total_pnl_pct": round(total_pnl_pct, 2),
            "position_count": len(enriched),
        },
    }, indent=2, default=str)


if __name__ == "__main__":
    mcp.run()
