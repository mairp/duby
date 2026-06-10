"""Portfolio CRUD + Wio sync + YAML import/export endpoints."""

import asyncio
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile, File, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

import json
import logging

from core import database as db
from core.finance import (
    fetch_yahoo_data, fetch_tradingview_data, calculate_atr_stop,
    calculate_default_target, call_llm, parse_json_response,
)
from core.portfolio_yaml import save_portfolio, load_portfolio, DEFAULT_PATH

log = logging.getLogger(__name__)

router = APIRouter(tags=["portfolio"])
_executor = ThreadPoolExecutor(max_workers=3)


class AddPositionRequest(BaseModel):
    ticker: str
    shares: float
    entry_price: float
    stop_loss: float | None = None
    target_price: float | None = None


class SetLevelsRequest(BaseModel):
    stop_loss: float | None = None
    target_price: float | None = None


@router.get("/portfolio")
async def get_portfolio():
    positions = db.get_portfolio()
    loop = asyncio.get_running_loop()
    results = []

    for pos in positions:
        ticker = pos["ticker"]
        try:
            data = await loop.run_in_executor(_executor, fetch_yahoo_data, ticker)
            price = data.get("current_price")
        except Exception:
            price = None

        entry = pos.get("unit_cost") or pos["entry_price"]
        cost = entry * pos["shares"]
        value = pos["shares"] * price if price else cost
        pnl = value - cost
        pnl_pct = (pnl / cost * 100) if cost else 0

        stop_dist = None
        target_dist = None
        if price and pos.get("stop_loss"):
            stop_dist = round((price - pos["stop_loss"]) / price * 100, 2)
        if price and pos.get("target_price"):
            target_dist = round((pos["target_price"] - price) / price * 100, 2)

        results.append({
            **pos,
            "current_price": price,
            "value": round(value, 2),
            "cost": round(cost, 2),
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 2),
            "stop_distance_pct": stop_dist,
            "target_distance_pct": target_dist,
        })

    total_value = sum(r["value"] for r in results)
    total_cost = sum(r["cost"] for r in results)
    total_pnl = total_value - total_cost
    total_pnl_pct = (total_pnl / total_cost * 100) if total_cost else 0

    return {
        "positions": results,
        "summary": {
            "total_value": round(total_value, 2),
            "total_cost": round(total_cost, 2),
            "pnl": round(total_pnl, 2),
            "pnl_pct": round(total_pnl_pct, 2),
            "position_count": len(results),
        },
    }


@router.post("/portfolio")
async def add_position(req: AddPositionRequest):
    loop = asyncio.get_running_loop()
    stop = req.stop_loss
    target = req.target_price
    stop_method = None

    if stop is None:
        try:
            data = await loop.run_in_executor(_executor, fetch_yahoo_data, req.ticker.upper())
            atr = data.get("atr_14")
            beta = data.get("beta") or 1.0
            if atr:
                stop, stop_method = calculate_atr_stop(req.entry_price, atr, beta)
        except Exception:
            pass

    if target is None and stop is not None:
        target = calculate_default_target(req.entry_price, stop)

    pos_id = db.add_position(
        ticker=req.ticker,
        shares=req.shares,
        entry_price=req.entry_price,
        source="api",
        stop_loss=stop,
        target_price=target,
        stop_method=stop_method,
    )
    return {"id": pos_id, "stop_loss": stop, "target_price": target, "stop_method": stop_method}


@router.delete("/portfolio/{ticker}")
async def remove_position(ticker: str):
    removed = db.remove_position(ticker)
    if not removed:
        raise HTTPException(404, f"No open position for {ticker.upper()}")
    return {"removed": ticker.upper()}


@router.put("/portfolio/{ticker}/levels")
async def set_levels(ticker: str, req: SetLevelsRequest):
    updated = db.update_position_levels(
        ticker=ticker,
        stop_loss=req.stop_loss,
        target_price=req.target_price,
    )
    if not updated:
        raise HTTPException(404, f"No open position for {ticker.upper()}")
    return {"ticker": ticker.upper(), "stop_loss": req.stop_loss, "target_price": req.target_price}


@router.post("/portfolio/sync")
async def sync_wio_pdf(file: UploadFile = File(...)):
    from core.wio_parser import parse_statement, sync_holdings_to_db

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        content = await file.read()
        tmp.write(content)
        tmp_path = tmp.name

    try:
        result = parse_statement(tmp_path)
        if not result["holdings"]:
            return {"error": "No holdings found in PDF", "activity_count": len(result["activity"])}

        sync_result = sync_holdings_to_db(result["holdings"])
        return {
            "account": result["account"],
            "holdings_parsed": len(result["holdings"]),
            "activity_parsed": len(result["activity"]),
            **sync_result,
        }
    finally:
        Path(tmp_path).unlink(missing_ok=True)


@router.post("/portfolio/import")
async def import_portfolio(request: Request):
    import yaml
    content_type = request.headers.get("content-type", "")
    body = await request.body()
    text = body.decode()

    if "yaml" in content_type or "text/plain" in content_type or text.lstrip().startswith("positions:") or text.lstrip().startswith("mode:"):
        data = yaml.safe_load(text)
    else:
        import json
        data = json.loads(text)

    if not data or "positions" not in data:
        raise HTTPException(400, "Request body must contain a 'positions' list")

    tmp = Path(tempfile.mktemp(suffix=".yaml"))
    tmp.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))

    try:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            _executor,
            lambda: load_portfolio(tmp, mode=data.get("mode", "merge")),
        )
    finally:
        tmp.unlink(missing_ok=True)

    save_portfolio()
    return result


@router.get("/portfolio/export")
async def export_portfolio():
    path = save_portfolio()
    return FileResponse(path, media_type="application/x-yaml", filename="portfolio.yaml")


@router.post("/portfolio/save")
async def save_portfolio_endpoint():
    path = save_portfolio()
    return {"saved": str(path)}


RECONCILE_PROMPT = """You are a risk management expert. For each position below, recommend optimal stop_loss and target_price based on the market data provided.

Rules:
- Stop loss should respect recent support levels and ATR volatility — not arbitrary round numbers
- The ATR-based stop is provided as a starting point; adjust if support/resistance suggests a better level
- Target should reflect realistic upside from resistance levels, analyst targets, and risk/reward (minimum 1.5:1)
- If 52-week low is near the ATR stop, tighten to just below support
- If the stock is in a strong uptrend (price > MA50 > MA200, RSI 50-70), use tighter stops
- For high-beta stocks (beta > 1.5), use wider stops to avoid whipsaws
- For each position explain your reasoning in one sentence

Respond ONLY with a JSON array. Each element:
{"ticker": "X", "stop_loss": 123.45, "target_price": 234.56, "stop_method": "brief description", "reasoning": "one sentence"}
"""


@router.post("/portfolio/reconcile")
async def reconcile_levels():
    positions = db.get_portfolio()
    if not positions:
        raise HTTPException(400, "Portfolio is empty")

    loop = asyncio.get_running_loop()
    market_data = {}

    for pos in positions:
        ticker = pos["ticker"]
        try:
            yahoo, tv = await asyncio.gather(
                loop.run_in_executor(_executor, fetch_yahoo_data, ticker),
                loop.run_in_executor(_executor, fetch_tradingview_data, ticker),
            )
            market_data[ticker] = {"yahoo": yahoo, "tradingview": tv}
        except Exception as e:
            log.warning("Failed to fetch data for %s: %s", ticker, e)
            market_data[ticker] = {"error": str(e)}

    position_summaries = []
    for pos in positions:
        ticker = pos["ticker"]
        md = market_data.get(ticker, {})
        yahoo = md.get("yahoo", {})
        tv = md.get("tradingview", {})

        entry = pos.get("unit_cost") or pos["entry_price"]
        atr = yahoo.get("atr_14")
        beta = yahoo.get("beta") or 1.0
        atr_stop, atr_method = calculate_atr_stop(entry, atr, beta) if atr else (None, None)

        position_summaries.append({
            "ticker": ticker,
            "shares": pos["shares"],
            "entry_price": entry,
            "current_price": yahoo.get("current_price"),
            "atr_14": atr,
            "beta": beta,
            "rsi_14": yahoo.get("rsi_14"),
            "ma_20": yahoo.get("ma_20"),
            "ma_50": yahoo.get("ma_50"),
            "52w_low": yahoo.get("fifty_two_week_low"),
            "52w_high": yahoo.get("fifty_two_week_high"),
            "sector": yahoo.get("sector"),
            "analyst_target": yahoo.get("target_mean_price"),
            "atr_stop_suggestion": atr_stop,
            "atr_method": atr_method,
            "tradingview_recommendation": tv.get("recommendation"),
            "current_stop": pos.get("stop_loss"),
            "current_target": pos.get("target_price"),
        })

    messages = [
        {"role": "system", "content": RECONCILE_PROMPT},
        {"role": "user", "content": json.dumps(position_summaries, indent=2)},
    ]

    raw = await call_llm(messages, max_tokens=16000)
    parsed = parse_json_response(raw)

    if not parsed:
        return {"error": "LLM did not return valid JSON", "raw": raw[:2000]}

    recommendations = parsed if isinstance(parsed, list) else parsed.get("positions", parsed.get("recommendations", []))
    if not isinstance(recommendations, list):
        return {"error": "Unexpected response format", "raw": raw[:2000]}

    updated = []
    for rec in recommendations:
        ticker = rec.get("ticker", "").upper()
        stop = rec.get("stop_loss")
        target = rec.get("target_price")
        method = rec.get("stop_method")

        if not ticker or (stop is None and target is None):
            continue

        db.update_position_levels(ticker, stop_loss=stop, target_price=target, stop_method=method)
        updated.append(rec)

    save_portfolio()
    return {"reconciled": len(updated), "positions": updated}
