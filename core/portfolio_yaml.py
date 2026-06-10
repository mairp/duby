"""Portfolio YAML persistence — save/load portfolio state to a YAML file."""

import logging
from pathlib import Path

import yaml

from core import database as db
from core.finance import fetch_yahoo_data, calculate_atr_stop, calculate_default_target

log = logging.getLogger(__name__)

DEFAULT_PATH = Path(__file__).parent.parent / "data" / "portfolio.yaml"


def save_portfolio(path: Path | None = None) -> Path:
    path = path or DEFAULT_PATH
    positions = db.get_portfolio()
    entries = []
    for pos in positions:
        entry = {"ticker": pos["ticker"], "shares": pos["shares"]}
        if pos.get("unit_cost"):
            entry["unit_cost"] = pos["unit_cost"]
        entry["entry_price"] = pos["entry_price"]
        if pos.get("stop_loss"):
            entry["stop_loss"] = pos["stop_loss"]
        if pos.get("target_price"):
            entry["target_price"] = pos["target_price"]
        if pos.get("stop_method"):
            entry["stop_method"] = pos["stop_method"]
        if pos.get("source"):
            entry["source"] = pos["source"]
        entries.append(entry)

    data = {"positions": entries}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False, allow_unicode=True))
    log.info("Portfolio saved to %s (%d positions)", path, len(entries))
    return path


def load_portfolio(path: Path | None = None, mode: str = "merge", calc_stops: bool = True) -> dict:
    path = path or DEFAULT_PATH
    if not path.exists():
        return {"loaded": 0, "skipped": 0, "error": f"File not found: {path}"}

    raw = yaml.safe_load(path.read_text())
    if not raw or "positions" not in raw:
        return {"loaded": 0, "skipped": 0, "error": "Invalid YAML: missing 'positions' key"}

    positions = raw["positions"]
    mode = raw.get("mode", mode)

    if mode == "replace":
        db.clear_all_positions()

    loaded = 0
    skipped = 0
    details = []

    for p in positions:
        ticker = p.get("ticker", "").upper()
        shares = p.get("shares")
        if not ticker or not shares:
            skipped += 1
            continue

        unit_cost = p.get("unit_cost")
        entry_price = p.get("entry_price") or unit_cost
        if not entry_price:
            skipped += 1
            continue

        stop_loss = p.get("stop_loss")
        target_price = p.get("target_price")
        stop_method = p.get("stop_method")
        source = p.get("source", "yaml")

        if calc_stops and stop_loss is None:
            try:
                yahoo = fetch_yahoo_data(ticker)
                atr = yahoo.get("atr_14")
                beta = yahoo.get("beta") or 1.0
                if atr:
                    stop_loss, stop_method = calculate_atr_stop(entry_price, atr, beta)
                    if target_price is None:
                        target_price = calculate_default_target(entry_price, stop_loss)
            except Exception as e:
                log.warning("Failed to calculate stops for %s: %s", ticker, e)

        db.upsert_position(
            ticker=ticker,
            shares=shares,
            entry_price=entry_price,
            source=source,
            unit_cost=unit_cost,
            stop_loss=stop_loss,
            target_price=target_price,
            stop_method=stop_method,
        )
        loaded += 1
        details.append(ticker)

    log.info("Portfolio loaded from %s: %d loaded, %d skipped (mode=%s)", path, loaded, skipped, mode)
    return {"loaded": loaded, "skipped": skipped, "mode": mode, "tickers": details}
