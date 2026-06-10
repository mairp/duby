import os
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

from dotenv import load_dotenv

log = logging.getLogger(__name__)

_client = None
_write_api = None
_query_api = None
_bucket = ""
_org = ""
_available = False


def init_influx():
    global _client, _write_api, _query_api, _bucket, _org, _available

    if _client is not None:
        return _available

    load_dotenv(Path(__file__).parent.parent / ".env")

    url = os.environ.get("INFLUXDB_URL", "http://localhost:8086")
    token = os.environ.get("INFLUXDB_TOKEN", "shares-telemetry-token")
    _org = os.environ.get("INFLUXDB_ORG", "shares")
    _bucket = os.environ.get("INFLUXDB_BUCKET", "telemetry")

    try:
        from influxdb_client import InfluxDBClient
        from influxdb_client.client.write_api import SYNCHRONOUS

        _client = InfluxDBClient(url=url, token=token, org=_org)
        _write_api = _client.write_api(write_options=SYNCHRONOUS)
        _query_api = _client.query_api()
        _available = True
        log.info("InfluxDB connected at %s", url)
    except ImportError:
        log.warning("influxdb-client not installed — telemetry disabled")
    except Exception as e:
        log.warning("InfluxDB connection failed: %s — telemetry disabled", e)

    return _available


def write_price_snapshot(ticker: str, price: float, volume: int | None = None, rsi: float | None = None):
    if not init_influx():
        return
    try:
        from influxdb_client import Point
        p = Point("price").tag("ticker", ticker.upper()).field("price", float(price))
        if volume is not None:
            p = p.field("volume", int(volume))
        if rsi is not None:
            p = p.field("rsi", float(rsi))
        _write_api.write(bucket=_bucket, org=_org, record=p)
    except Exception as e:
        log.warning("InfluxDB write failed (price %s): %s", ticker, e)


def write_portfolio_snapshot(total_value: float, total_cost: float):
    if not init_influx():
        return
    try:
        from influxdb_client import Point
        pnl = total_value - total_cost
        pnl_pct = (pnl / total_cost * 100) if total_cost > 0 else 0.0
        p = (Point("portfolio")
             .field("total_value", float(total_value))
             .field("total_cost", float(total_cost))
             .field("pnl", float(pnl))
             .field("pnl_pct", float(pnl_pct)))
        _write_api.write(bucket=_bucket, org=_org, record=p)
    except Exception as e:
        log.warning("InfluxDB write failed (portfolio): %s", e)


def query_price_history(ticker: str, start: str | None = None, stop: str | None = None) -> list[dict]:
    if not init_influx():
        return []

    if start and stop:
        range_clause = f'  |> range(start: {start}, stop: {stop})'
    elif start:
        range_clause = f'  |> range(start: {start})'
    else:
        range_clause = '  |> range(start: -90d)'

    query = f'''
from(bucket: "{_bucket}")
{range_clause}
  |> filter(fn: (r) => r._measurement == "price")
  |> filter(fn: (r) => r.ticker == "{ticker.upper()}")
  |> filter(fn: (r) => r._field == "price")
  |> sort(columns: ["_time"])
'''
    try:
        tables = _query_api.query(query, org=_org)
        results = []
        for table in tables:
            for record in table.records:
                results.append({
                    "timestamp": record.get_time().isoformat(),
                    "price": record.get_value(),
                    "ticker": ticker.upper(),
                })
        return results
    except Exception as e:
        log.warning("InfluxDB query failed (price %s): %s", ticker, e)
        return []


def query_portfolio_history(days: int = 30) -> list[dict]:
    if not init_influx():
        return []

    query = f'''
from(bucket: "{_bucket}")
  |> range(start: -{days}d)
  |> filter(fn: (r) => r._measurement == "portfolio")
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> sort(columns: ["_time"])
'''
    try:
        tables = _query_api.query(query, org=_org)
        results = []
        for table in tables:
            for record in table.records:
                results.append({
                    "timestamp": record.values.get("_time", "").isoformat() if hasattr(record.values.get("_time", ""), "isoformat") else str(record.values.get("_time", "")),
                    "total_value": record.values.get("total_value"),
                    "total_cost": record.values.get("total_cost"),
                    "pnl": record.values.get("pnl"),
                    "pnl_pct": record.values.get("pnl_pct"),
                })
        return results
    except Exception as e:
        log.warning("InfluxDB query failed (portfolio): %s", e)
        return []
