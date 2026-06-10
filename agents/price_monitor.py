"""Price monitor agent — polls prices, checks stop/target levels, sends alerts, writes telemetry."""

import asyncio
import os
import sys
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
import aiohttp

load_dotenv(Path(__file__).parent.parent / ".env")

from core.finance import init_config, fetch_yahoo_data
from core import database as db
from core import timeseries as ts
from core.portfolio_yaml import save_portfolio, load_portfolio, DEFAULT_PATH

init_config()
db.init_db()
ts.init_influx()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("ALERT_BOT_TOKEN", "")
CHAT_ID = os.environ.get("ALERT_CHAT_ID", "")
POLL_MARKET = 60
POLL_OFF = 300
ALERT_COOLDOWN_MIN = 15
ALERT_DEDUP_HOURS = 24
PROXIMITY_PCT = 0.02

PID_FILE = Path(__file__).parent.parent / "data" / "price_monitor.pid"


def _is_market_hours() -> bool:
    now = datetime.now(timezone.utc)
    et_offset = -4
    et_hour = (now.hour + et_offset) % 24
    return now.weekday() < 5 and 9 <= et_hour < 16


async def send_telegram(text: str):
    if not BOT_TOKEN or not CHAT_ID:
        log.warning("Alert skipped — bot token or chat ID not set")
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    async with aiohttp.ClientSession() as session:
        for chunk in [text[i:i + 4000] for i in range(0, len(text), 4000)]:
            try:
                async with session.post(url, json={
                    "chat_id": CHAT_ID,
                    "text": chunk,
                    "parse_mode": "HTML",
                }) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        log.error("Telegram send failed: %s", body)
            except Exception as e:
                log.error("Telegram send error: %s", e)


def _format_alert(alert_type: str, ticker: str, price: float, level: float,
                   entry: float, shares: float) -> str:
    cost = entry * shares
    value = price * shares
    pnl = value - cost
    pnl_pct = (pnl / cost * 100) if cost else 0

    if alert_type == "stop_hit":
        icon = "\U0001f534"
        title = "STOP HIT"
        action = "Consider selling to protect capital"
    elif alert_type == "target_reached":
        icon = "\U0001f7e2"
        title = "TARGET REACHED"
        action = "Consider taking profits or scaling out"
    elif alert_type == "approaching_stop":
        icon = "⚠️"
        dist = abs(price - level) / price * 100
        title = f"APPROACHING STOP ({dist:.1f}% away)"
        action = "Monitor closely"
    elif alert_type == "approaching_target":
        icon = "⚠️"
        dist = abs(price - level) / price * 100
        title = f"APPROACHING TARGET ({dist:.1f}% away)"
        action = "Prepare exit strategy"
    else:
        icon = "❓"
        title = alert_type.upper()
        action = ""

    pnl_sign = "+" if pnl >= 0 else ""
    return (
        f"{icon} <b>{title} — {ticker}</b>\n"
        f"Price: ${price:,.2f} (level: ${level:,.2f})\n"
        f"Entry: ${entry:,.2f} | P&L: {pnl_sign}${pnl:,.2f} ({pnl_sign}{pnl_pct:.1f}%)\n"
        f"Action: {action}"
    )


async def check_position(executor: ThreadPoolExecutor, pos: dict) -> list[dict]:
    alerts = []
    ticker = pos["ticker"]
    stop = pos.get("stop_loss")
    target = pos.get("target_price")

    if not stop and not target:
        return alerts

    loop = asyncio.get_running_loop()
    try:
        data = await loop.run_in_executor(executor, fetch_yahoo_data, ticker)
    except Exception as e:
        log.warning("Fetch failed for %s: %s", ticker, e)
        return alerts

    price = data.get("current_price")
    if not price:
        return alerts

    if stop and price <= stop:
        if not db.was_alert_sent(ticker, "stop_hit", ALERT_DEDUP_HOURS):
            alerts.append({"type": "stop_hit", "ticker": ticker, "price": price,
                           "level": stop, "pos": pos})
    elif stop and price > stop:
        dist = (price - stop) / price
        if dist <= PROXIMITY_PCT and not db.was_alert_sent(ticker, "approaching_stop", ALERT_DEDUP_HOURS):
            alerts.append({"type": "approaching_stop", "ticker": ticker, "price": price,
                           "level": stop, "pos": pos})

    if target and price >= target:
        if not db.was_alert_sent(ticker, "target_reached", ALERT_DEDUP_HOURS):
            alerts.append({"type": "target_reached", "ticker": ticker, "price": price,
                           "level": target, "pos": pos})
    elif target and price < target:
        dist = (target - price) / price
        if dist <= PROXIMITY_PCT and not db.was_alert_sent(ticker, "approaching_target", ALERT_DEDUP_HOURS):
            alerts.append({"type": "approaching_target", "ticker": ticker, "price": price,
                           "level": target, "pos": pos})

    return alerts


async def collect_telemetry(executor: ThreadPoolExecutor):
    positions = db.get_portfolio()
    if not positions:
        return

    tickers = list({p["ticker"] for p in positions})
    loop = asyncio.get_running_loop()
    price_cache = {}

    for ticker in tickers:
        try:
            data = await loop.run_in_executor(executor, fetch_yahoo_data, ticker)
            price = data.get("current_price")
            if price:
                price_cache[ticker] = price
                ts.write_price_snapshot(ticker, price,
                                       volume=data.get("volume"),
                                       rsi=data.get("rsi_14"))
        except Exception:
            pass

    total_value = 0.0
    total_cost = 0.0
    for pos in positions:
        cost = pos["shares"] * pos["entry_price"]
        total_cost += cost
        p = price_cache.get(pos["ticker"])
        total_value += pos["shares"] * p if p else cost

    if total_cost > 0:
        ts.write_portfolio_snapshot(total_value, total_cost)


async def run():
    executor = ThreadPoolExecutor(max_workers=3)
    log.info("Price monitor started (market=%ds, off=%ds)", POLL_MARKET, POLL_OFF)

    if DEFAULT_PATH.exists() and not db.get_portfolio():
        log.info("DB empty, loading portfolio from %s", DEFAULT_PATH)
        result = load_portfolio(calc_stops=True)
        log.info("Loaded: %s", result)

    telemetry_counter = 0
    save_counter = 0

    while True:
        market = _is_market_hours()
        interval = POLL_MARKET if market else POLL_OFF

        positions = db.get_positions_with_levels()
        if positions:
            tasks = [check_position(executor, pos) for pos in positions]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for result in results:
                if isinstance(result, list):
                    for alert in result:
                        pos = alert["pos"]
                        entry = pos.get("unit_cost") or pos["entry_price"]
                        msg = _format_alert(alert["type"], alert["ticker"],
                                            alert["price"], alert["level"],
                                            entry, pos["shares"])
                        await send_telegram(msg)
                        db.record_alert(alert["ticker"], alert["type"],
                                        alert["price"], alert["level"],
                                        pos.get("id"))
                        log.info("Alert sent: %s %s @ %.2f (level %.2f)",
                                 alert["type"], alert["ticker"], alert["price"], alert["level"])
                elif isinstance(result, Exception):
                    log.error("Check error: %s", result)

        telemetry_counter += interval
        if market and telemetry_counter >= 300:
            telemetry_counter = 0
            await collect_telemetry(executor)
            log.info("Telemetry collected")

        save_counter += interval
        if save_counter >= 300:
            save_counter = 0
            try:
                save_portfolio()
            except Exception as e:
                log.warning("Auto-save failed: %s", e)

        await asyncio.sleep(interval)


def main():
    PID_FILE.parent.mkdir(exist_ok=True)
    if PID_FILE.exists():
        old_pid = PID_FILE.read_text().strip()
        try:
            os.kill(int(old_pid), 0)
            log.error("Price monitor already running (PID %s)", old_pid)
            sys.exit(1)
        except (ProcessLookupError, ValueError):
            pass

    PID_FILE.write_text(str(os.getpid()))
    try:
        asyncio.run(run())
    finally:
        PID_FILE.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
