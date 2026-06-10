"""Market data + OHLC chart endpoints."""

import asyncio
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, Query
import yfinance as yf

from core.finance import fetch_yahoo_data, fetch_tradingview_data, get_market_data

router = APIRouter(tags=["market"])
_executor = ThreadPoolExecutor(max_workers=3)

RANGE_TO_INTERVAL = {
    "1d": "1m",
    "5d": "5m",
    "1mo": "1d",
    "3mo": "1d",
    "6mo": "1d",
    "1y": "1wk",
    "2y": "1wk",
    "5y": "1mo",
    "max": "1mo",
}


@router.get("/quote/{ticker}")
async def get_quote(ticker: str):
    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(_executor, fetch_yahoo_data, ticker.upper())
    return data


@router.get("/technicals/{ticker}")
async def get_technicals(ticker: str):
    data = await get_market_data(ticker.upper())
    return data


@router.get("/charts/ohlc/{ticker}")
async def get_ohlc(
    ticker: str,
    range: str = Query("3mo", pattern="^(1d|5d|1mo|3mo|6mo|1y|2y|5y|max)$"),
    interval: str | None = None,
):
    if interval is None:
        interval = RANGE_TO_INTERVAL.get(range, "1d")

    loop = asyncio.get_running_loop()

    def _fetch():
        t = yf.Ticker(ticker.upper())
        df = t.history(period=range, interval=interval)
        if df.empty:
            return []
        records = []
        for ts, row in df.iterrows():
            records.append({
                "time": int(ts.timestamp()),
                "open": round(row["Open"], 2),
                "high": round(row["High"], 2),
                "low": round(row["Low"], 2),
                "close": round(row["Close"], 2),
                "volume": int(row["Volume"]),
            })
        return records

    data = await loop.run_in_executor(_executor, _fetch)
    return {"ticker": ticker.upper(), "range": range, "interval": interval, "data": data}
