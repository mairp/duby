"""Investment optimizer endpoint."""

import asyncio
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter
from pydantic import BaseModel

from core.finance import get_market_data, optimize_allocation

router = APIRouter(tags=["invest"])
_executor = ThreadPoolExecutor(max_workers=3)


class InvestRequest(BaseModel):
    amount: float
    tickers: list[str]


@router.post("/invest")
async def run_optimizer(req: InvestRequest):
    tickers = [t.upper() for t in req.tickers]

    tasks = [get_market_data(t) for t in tickers]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    market_data = {}
    failed = []
    for ticker, result in zip(tickers, results):
        if isinstance(result, Exception) or "error" in result.get("yahoo", {}):
            failed.append(ticker)
        else:
            market_data[ticker] = result

    if not market_data:
        return {"error": "No valid market data", "failed": failed}

    result = await optimize_allocation(req.amount, list(market_data.keys()), market_data)
    return {"result": result, "failed": failed}
