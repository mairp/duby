"""Parse Wio Invest account statement PDFs (PyMuPDF)."""

import re
from pathlib import Path

import fitz


def parse_statement(pdf_path: str | Path) -> dict:
    doc = fitz.open(str(pdf_path))
    holdings = []
    activity = []
    account_info = {}

    for page in doc:
        text = page.get_text()
        lines = [l.strip() for l in text.split("\n") if l.strip()]

        if "ACCOUNT NUMBER" in text:
            for i, line in enumerate(lines):
                if "ACCOUNT NUMBER" in line:
                    acct_type = line.split("-")[-1].strip() if "-" in line else ""
                    if i + 1 < len(lines):
                        account_info["account_number"] = lines[i + 1]
                        account_info["account_type"] = acct_type
                if "Ending Portfolio Value" in line and i + 1 < len(lines):
                    val = lines[i + 1].replace("USD", "").replace(",", "").strip()
                    try:
                        account_info["ending_value"] = float(val)
                    except ValueError:
                        pass

        if "HOLDINGS" in text:
            holdings.extend(_parse_holdings(lines))

        if "ACTIVITY" in text:
            activity.extend(_parse_activity(lines))

    doc.close()
    return {
        "account": account_info,
        "holdings": holdings,
        "activity": activity,
    }


def _parse_holdings(lines: list[str]) -> list[dict]:
    results = []
    headers_idx = None
    for i, line in enumerate(lines):
        if line == "Instrument Name":
            headers_idx = i
            break

    if headers_idx is None:
        return results

    has_unit_cost = "Unit Cost" in lines[headers_idx:headers_idx + 12]
    num_headers = 10 if has_unit_cost else 9
    data_lines = lines[headers_idx + num_headers:]

    i = 0
    fields_per_row = 10 if has_unit_cost else 9
    while i + fields_per_row - 1 < len(data_lines):
        name = data_lines[i]
        if name.startswith("Wio Securities") or name.startswith("care@"):
            break

        symbol = data_lines[i + 1]
        if not re.match(r'^[A-Z]{1,5}$', symbol):
            i += 1
            continue

        try:
            currency = data_lines[i + 2]
            quantity = float(data_lines[i + 3])

            if has_unit_cost:
                unit_cost = float(data_lines[i + 4])
                total_cost = float(data_lines[i + 5])
                market_price = float(data_lines[i + 6])
                market_value = float(data_lines[i + 7])
                gain = float(data_lines[i + 8])
                gain_pct = float(data_lines[i + 9].replace("%", ""))
            else:
                total_cost = float(data_lines[i + 4])
                unit_cost = round(total_cost / quantity, 2) if quantity else 0
                market_price = float(data_lines[i + 5])
                market_value = float(data_lines[i + 6])
                gain = float(data_lines[i + 7])
                gain_pct = float(data_lines[i + 8].replace("%", ""))

            results.append({
                "name": name,
                "symbol": symbol,
                "currency": currency,
                "quantity": quantity,
                "unit_cost": unit_cost,
                "total_cost": total_cost,
                "market_price": market_price,
                "market_value": market_value,
                "gain": gain,
                "gain_pct": gain_pct,
            })
            i += fields_per_row
        except (ValueError, IndexError):
            i += 1

    return results


def _parse_activity(lines: list[str]) -> list[dict]:
    results = []
    headers_idx = None
    for i, line in enumerate(lines):
        if line == "Trade Date":
            headers_idx = i
            break

    if headers_idx is None:
        return results

    data_lines = lines[headers_idx + 9:]  # skip 9 header labels

    date_pattern = re.compile(r'^\d{1,2}\s+\w+,\s+\d{2}:\d{2}$')

    i = 0
    while i + 8 < len(data_lines):
        trade_date = data_lines[i]
        if not date_pattern.match(trade_date):
            if trade_date.startswith("Wio Securities") or trade_date.startswith("care@"):
                break
            i += 1
            continue

        try:
            settle_date = data_lines[i + 1]
            currency = data_lines[i + 2]
            action = data_lines[i + 3]
            if action not in ("BUY", "SELL"):
                i += 1
                continue
            instrument = data_lines[i + 4]
            symbol = data_lines[i + 5]
            quantity = float(data_lines[i + 6])
            price = float(data_lines[i + 7])
            total = float(data_lines[i + 8])

            results.append({
                "trade_date": trade_date,
                "settle_date": settle_date,
                "currency": currency,
                "action": action,
                "instrument": instrument,
                "symbol": symbol,
                "quantity": quantity,
                "price": price,
                "total_amount": total,
            })
            i += 9
        except (ValueError, IndexError):
            i += 1

    return results


def sync_holdings_to_db(holdings: list[dict]) -> dict:
    from core.database import upsert_position, get_portfolio

    existing = {p["ticker"]: p for p in get_portfolio()}
    added, updated, flagged = [], [], []

    for h in holdings:
        ticker = h["symbol"]
        was_existing = ticker in existing
        upsert_position(
            ticker=ticker,
            shares=h["quantity"],
            entry_price=h["unit_cost"],
            source="wio",
            unit_cost=h["unit_cost"],
        )
        if was_existing:
            updated.append(ticker)
        else:
            added.append(ticker)

    for ticker in existing:
        if ticker not in {h["symbol"] for h in holdings}:
            flagged.append(ticker)

    return {"added": added, "updated": updated, "not_in_statement": flagged}
