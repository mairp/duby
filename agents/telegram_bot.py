import json
import asyncio
import base64
import os
import re
import sys
import logging
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiohttp
from dotenv import load_dotenv

from core.finance import (
    init_config, call_llm, parse_json_response, resolve_company_ticker,
    fetch_yahoo_data, get_market_data, _fmt, _sanitize_html,
    calculate_atr_stop, calculate_default_target,
)
import core
from core import database as db

load_dotenv(Path(__file__).parent.parent / ".env")
init_config()
db.init_db()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("ALERT_BOT_TOKEN", "")
AUTHORIZED_CHAT = os.environ.get("ALERT_CHAT_ID", "")
BOT_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"

COMMON_WORDS = {
    "I", "A", "AM", "AN", "AS", "AT", "BE", "BY", "DO", "GO", "HE", "IF",
    "IN", "IS", "IT", "ME", "MY", "NO", "OF", "OK", "ON", "OR", "SO", "TO",
    "UP", "US", "WE", "ALL", "AND", "ARE", "BUY", "CAN", "DAY", "DID",
    "FOR", "GET", "GOT", "HAS", "HAD", "HER", "HIM", "HIS", "HOW", "ITS",
    "LET", "LOW", "MAY", "NEW", "NOT", "NOW", "OLD", "ONE", "OUR", "OUT",
    "OWN", "PUT", "RUN", "SAY", "SET", "SHE", "THE", "TOO", "TWO", "USE",
    "WAY", "WHO", "WHY", "WIN", "YES", "YET", "YOU", "ADD", "TOP", "MAX",
    "MIN", "PER", "USD", "ETF", "CEO", "CFO", "IPO", "ATH", "EPS", "RSI",
    "PE", "MA", "ATM",
}

MAX_HISTORY = 10
conversations: dict[int, list[dict]] = defaultdict(list)

# Pending confirmations: chat_id -> {"stocks": [...], "action": "buy"|"sell"}
pending_confirmations: dict[int, dict] = {}

PORTFOLIO_EXTRACT_PROMPT = """You analyze images and text of model portfolios, watchlists, or stock tables.
These often come from Spanish-language Telegram investment channels (Big Deal Capital, etc.).

Extract every stock listed with its US ticker symbol, number of shares, and entry price.

Key vocabulary:
- COMPRA / COMPRAS / COMPRA VALORES = BUY
- VENTA / VENTAS / VENTA DE VALORES = SELL
- TITULOS / CANTIDAD = number of shares
- Precio actual / PRECIO ACTUAL / PRECIO MEDIO = price per share
- IMPORTE / VALORACIÓN ACTUAL / VAL. MERCADO = total position value (shares × price)
- PESO APROX = portfolio weight percentage

Number format: Spanish uses comma as decimal separator (153,76 = 153.76) and dot as thousands (1.214,15 = 1214.15). Convert ALL numbers to standard decimal format in your output.

Return ONLY valid JSON:
{
  "action": "buy" or "sell" or "hold",
  "stocks": [
    {"ticker": "PANW", "company": "Palo Alto Networks", "shares": 5, "price": 242.83},
    {"ticker": "FIX", "company": "Comfort Systems", "shares": 1, "price": 1992.74}
  ]
}

Rules:
- Use the US ticker symbol. If the ticker is shown as $PANW or (PANW), extract PANW.
- If only the company name is shown (e.g., "MUELLER INDUSTRIES"), infer the US ticker.
- "shares" = number of shares/TITULOS/CANTIDAD. If not shown, set to null.
- "price" = price per share (Precio actual / PRECIO ACTUAL), NOT the total IMPORTE. Convert Spanish decimals (comma) to standard (dot).
- If action cannot be determined, use "hold".
- If no stocks found, return: {"action": "hold", "stocks": []}
- Only return JSON, no extra text."""

CHAT_SYSTEM_PROMPT = """You are a concise stock analyst bot for a long-term growth investor. Business quality and compounding power matter most; technicals only influence position sizing.

PORTFOLIO BUDGET (MANDATORY):
- Total budget: $10K-$15K for ALL positions combined (NOT per stock)
- The user diversifies across 5-10+ stocks
- MAX per position: $3,000 (20% of budget) — NEVER suggest more
- Ideal position: $1,500-$3,000 (10-20% of budget)
- Always check PORTFOLIO STATE (provided with each message) to see available capital
- If available capital < $500, say WAIT — no money left to deploy
- If user already holds this ticker, say HOLD — no doubling down

When asked about a stock, answer in this exact format:

ICON <b>TICKER — Company Name</b>
Verdict: BUY / SCALE-IN / WAIT / AVOID
Current: $X.XX
Fundamentals: FCF $XB (Y% yield) | ROE X% | Margins X% | D/E X
Analysts: X Buy / X Sell / X Neutral (TradingView consensus)
Entry target: $X.XX (if WAIT or SCALE-IN — the price to scale to full position)
Stop loss: $X.XX
Position: X shares (~$X,XXX — X% of portfolio)
Why: one sentence covering fundamental quality + technical timing

Use these icons before each stock header:
- ✅ for BUY
- 🔄 for SCALE-IN
- ⏸ for WAIT
- 🔴 for AVOID

Rules:
- Lead with fundamentals. A stock with negative FCF and declining revenue is AVOID regardless of technicals.
- Strong fundamentals + weak technicals = SCALE-IN (smaller position, add on dip). Technicals affect SIZING, not the binary buy decision.
- Weak fundamentals + strong technicals = AVOID (momentum without substance).
- Quality compounders at premium valuations are NORMAL. Only penalize valuation when extreme vs growth (PEG > 3).
- RSI < 30 on a quality business is often a buy opportunity, not a warning.
- Be decisive. BUY, SCALE-IN, WAIT, or AVOID. No hedging.
- If BUY: current price is a good entry. Say why briefly.
- If SCALE-IN: buy half position now, give target to add more.
- If WAIT: give a specific entry price target and why.
- If AVOID: say why in one sentence.
- Use live market data and portfolio state when provided.
- Keep responses under 500 characters for single-stock questions.
- For broader questions, stay under 1000 characters.
- Format for Telegram HTML: <b>bold</b>, <i>italic</i>, <code>code</code>."""


def _portfolio_context() -> str:
    from core.finance import BUDGET_MIN, BUDGET_MAX
    summary = db.get_portfolio_summary()
    available = BUDGET_MAX - summary["total_invested"]
    holdings = ", ".join(
        f"{p['ticker']} ({p['shares']} shares @ ${p['entry_price']:.2f})"
        for p in summary["positions"]
    ) or "None"
    return (
        f"\n\nPORTFOLIO STATE:\n"
        f"Total budget: ${BUDGET_MIN:,}-${BUDGET_MAX:,} (for ALL positions combined)\n"
        f"Invested: ${summary['total_invested']:,.2f} in {summary['position_count']} positions\n"
        f"Available capital: ${available:,.2f}\n"
        f"Holdings: {holdings}"
    )


def detect_tickers(text: str) -> list[str]:
    found: list[str] = []

    # 1. Explicit $TICKER patterns (e.g., $TSLA, $panw)
    for m in re.finditer(r'\$([A-Za-z]{1,5})\b', text):
        t = m.group(1).upper()
        if t not in found:
            found.append(t)

    # 2. Uppercase words that look like tickers (existing behavior)
    for w in re.findall(r'\b[A-Z]{2,5}\b', text):
        if w not in COMMON_WORDS and w not in found:
            found.append(w)

    # 3. If nothing found yet, try case-insensitive: words 2-5 chars
    #    that could be tickers (validate via Yahoo Finance)
    if not found:
        candidates = re.findall(r'\b([A-Za-z]{2,5})\b', text)
        seen = set()
        for w in candidates:
            upper = w.upper()
            if upper in COMMON_WORDS or upper in seen:
                continue
            seen.add(upper)
            if len(seen) > 5:
                break
            try:
                data = fetch_yahoo_data(upper)
                if "error" not in data and data.get("current_price"):
                    found.append(upper)
                    if len(found) >= 2:
                        break
            except Exception:
                continue

    # 4. If still nothing, look for multi-word company names and resolve
    if not found:
        capitalized = re.findall(r'[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*', text)
        for name in capitalized[:3]:
            if len(name) < 4:
                continue
            ticker = resolve_company_ticker(name)
            if ticker and ticker not in found:
                found.append(ticker)
                break

    return found[:3]


def parse_spanish_decimal(s: str) -> float | None:
    """Convert Spanish number format (1.234,56) to float (1234.56)."""
    s = s.strip().rstrip("$€").strip()
    if not s:
        return None
    s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def parse_cartera_text(text: str) -> dict | None:
    """Parse structured CARTERA BIG DEAL text messages with regex."""
    action = "hold"
    text_upper = text.upper()
    if "COMPRA" in text_upper:
        action = "buy"
    elif "VENTA" in text_upper:
        action = "sell"

    stocks = []

    # Pattern: $TICKER (N TITULOS) Precio actual 153,76$. IMPORTE 922,56$
    pattern = re.compile(
        r'\$([A-Z]{1,5})\s*\((\d+)\s*TITULO'
        r'.*?Precio actual\s*([\d.,]+)\s*\$',
        re.IGNORECASE
    )
    for m in pattern.finditer(text):
        ticker = m.group(1).upper()
        shares = int(m.group(2))
        price = parse_spanish_decimal(m.group(3))
        if price:
            stocks.append({"ticker": ticker, "company": "", "shares": shares, "price": price})

    # Pattern: COMPANY_NAME (TICKER) ... CANTIDAD ... PRECIO_ACTUAL
    # e.g., "PALO ALTO NETWORKS (PANW)    5    242,83 $"
    pattern2 = re.compile(
        r'([A-Z][A-Z &]+?)\s*\(([A-Z]{1,5})\)\s+'
        r'(\d+)\s+'
        r'([\d.,]+)\s*\$',
        re.IGNORECASE
    )
    for m in pattern2.finditer(text):
        ticker = m.group(2).upper()
        if any(s["ticker"] == ticker for s in stocks):
            continue
        shares = int(m.group(3))
        price = parse_spanish_decimal(m.group(4))
        if price:
            stocks.append({"ticker": ticker, "company": m.group(1).strip(), "shares": shares, "price": price})

    # Pattern: COMPANY_NAME (N TITULOS) Precio actual PRICE — no $TICKER tag
    # e.g., "MUELLER INDUSTRIES (5 TITULOS) Precio actual 139,30$"
    pattern3 = re.compile(
        r'([A-Z][A-Z ]+?)\s*\((\d+)\s*TITULO'
        r'.*?Precio actual\s*([\d.,]+)\s*\$',
        re.IGNORECASE
    )
    for m in pattern3.finditer(text):
        company = m.group(1).strip()
        # Skip if we already got this company via $TICKER pattern
        if any(company.upper() in (s.get("company", "").upper() or s["ticker"]) for s in stocks):
            continue
        shares = int(m.group(2))
        price = parse_spanish_decimal(m.group(3))
        if price:
            stocks.append({"ticker": company, "company": company, "shares": shares, "price": price, "_needs_ticker": True})

    # Resolve company names to tickers for entries without $TICKER
    for s in stocks:
        if s.pop("_needs_ticker", False):
            resolved = resolve_company_ticker(s["company"])
            if resolved:
                s["ticker"] = resolved

    if not stocks:
        return None
    return {"action": action, "stocks": stocks}


def is_cartera_message(text: str) -> bool:
    """Check if a text message looks like a CARTERA/portfolio alert."""
    indicators = ["CARTERA", "COMPRA VALORES", "VENTA VALORES", "COMPRA DE VALORES",
                   "VENTA DE VALORES", "TITULOS", "Precio actual", "IMPORTE"]
    text_upper = text.upper()
    return sum(1 for ind in indicators if ind.upper() in text_upper) >= 2


def _strip_tags(text: str) -> str:
    """Last-resort plain text: drop tags + unescape entities (so users never see raw <b>)."""
    text = re.sub(r"</?[A-Za-z][^>]*>", "", text)
    return (text.replace("&lt;", "<").replace("&gt;", ">")
                .replace("&quot;", '"').replace("&amp;", "&"))


async def send_message(session: aiohttp.ClientSession, chat_id: int | str, text: str, parse_mode: str = "HTML"):
    # Escape stray < > & in content (keep real <b>/<i>/<code> tags) so Telegram HTML never fails to parse.
    if parse_mode == "HTML":
        text = _sanitize_html(text)
    chunks = _split_message(text)
    for chunk in chunks:
        resp = await session.post(f"{BOT_BASE}/sendMessage", json={
            "chat_id": chat_id,
            "text": chunk,
            "parse_mode": parse_mode,
        })
        data = await resp.json()
        if not data.get("ok"):
            log.warning("sendMessage failed (parse_mode=%s): %s", parse_mode, data.get("description", ""))
            # fall back to PLAIN TEXT with tags stripped — never show raw <b> markup
            await session.post(f"{BOT_BASE}/sendMessage", json={
                "chat_id": chat_id,
                "text": _strip_tags(chunk),
            })
        if len(chunks) > 1:
            await asyncio.sleep(0.3)


def _split_message(text: str, limit: int = 4000) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return chunks


async def send_typing(session: aiohttp.ClientSession, chat_id: int | str):
    await session.post(f"{BOT_BASE}/sendChatAction", json={
        "chat_id": chat_id,
        "action": "typing",
    })


async def send_photo(session: aiohttp.ClientSession, chat_id: int | str,
                     image_bytes: bytes, caption: str = ""):
    data = aiohttp.FormData()
    data.add_field("chat_id", str(chat_id))
    data.add_field("photo", image_bytes, filename="chart.png", content_type="image/png")
    if caption:
        data.add_field("caption", caption)
        data.add_field("parse_mode", "HTML")
    resp = await session.post(f"{BOT_BASE}/sendPhoto", data=data)
    result = await resp.json()
    if not result.get("ok"):
        log.warning("sendPhoto failed: %s", result.get("description", ""))


CHART_RANGE_MAP = {
    "1d": ("1d", "1m"), "5d": ("5d", "5m"),
    "1mo": ("1mo", "1d"), "3mo": ("3mo", "1d"),
    "1y": ("1y", "1wk"), "max": ("max", "1mo"),
}


def render_chart(ticker: str, period: str = "3mo",
                 entry_price: float | None = None,
                 stop_loss: float | None = None,
                 target_price: float | None = None) -> bytes | None:
    import io
    import yfinance as yf
    import mplfinance as mpf
    import matplotlib
    matplotlib.use("Agg")

    period_key, interval = CHART_RANGE_MAP.get(period, ("3mo", "1d"))
    t = yf.Ticker(ticker)
    df = t.history(period=period_key, interval=interval)
    if df.empty or len(df) < 2:
        return None

    mc = mpf.make_marketcolors(up="#22c55e", down="#ef4444", edge="inherit",
                               wick="inherit", volume="in")
    style = mpf.make_mpf_style(base_mpf_style="nightclouds", marketcolors=mc,
                                facecolor="#0f1117", edgecolor="#1f2937",
                                gridcolor="#1f2937", gridstyle="--")

    hlines_vals = []
    hline_colors = []
    if entry_price:
        hlines_vals.append(entry_price)
        hline_colors.append("#3b82f6")
    if stop_loss:
        hlines_vals.append(stop_loss)
        hline_colors.append("#ef4444")
    if target_price:
        hlines_vals.append(target_price)
        hline_colors.append("#22c55e")

    kwargs = {}
    if hlines_vals:
        kwargs["hlines"] = dict(hlines=hlines_vals, colors=hline_colors,
                                linestyle="--", linewidths=1)

    buf = io.BytesIO()
    mpf.plot(df, type="candle", style=style, volume=True,
             title=f"\n{ticker}  ({period.upper()})",
             figsize=(10, 6), tight_layout=True,
             savefig=dict(fname=buf, dpi=150, bbox_inches="tight",
                          facecolor="#0f1117"),
             **kwargs)
    buf.seek(0)
    return buf.read()


async def download_photo(session: aiohttp.ClientSession, file_id: str) -> bytes | None:
    async with session.get(f"{BOT_BASE}/getFile", params={"file_id": file_id}) as resp:
        data = await resp.json()
    if not data.get("ok"):
        return None
    file_path = data["result"]["file_path"]
    async with session.get(f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}") as resp:
        if resp.status == 200:
            return await resp.read()
    return None


def format_confirmation(extracted: dict) -> str:
    """Format extracted stocks into a confirmation message."""
    stocks = extracted["stocks"]
    action = extracted.get("action", "hold")
    action_label = {"buy": "BUY (COMPRA)", "sell": "SELL (VENTA)"}.get(action, "HOLD")
    action_icon = {"buy": "🟢", "sell": "🔴"}.get(action, "🟡")

    lines = [f"{action_icon} <b>Detected {len(stocks)} stocks — {action_label}</b>\n"]

    total_cost = 0.0
    for i, s in enumerate(stocks, 1):
        ticker = s["ticker"]
        company = s.get("company", "")
        shares = s.get("shares")
        price = s.get("price")
        label = f"{ticker}"
        if company:
            label = f"{ticker} ({company})"

        if shares and price:
            cost = shares * price
            total_cost += cost
            lines.append(f"  {i}. <b>{label}</b>: {shares} shares @ ${price:,.2f} = ${cost:,.2f}")
        elif price:
            lines.append(f"  {i}. <b>{label}</b>: price ${price:,.2f} (shares unknown)")
        else:
            lines.append(f"  {i}. <b>{label}</b>: detected (missing data)")

    if total_cost > 0:
        lines.append(f"\n  Total: <b>${total_cost:,.2f}</b>")

    if action == "buy":
        lines.append("\n<b>Add all to portfolio?</b>")
    elif action == "sell":
        lines.append("\n<b>Remove all from portfolio?</b>")
    else:
        lines.append("\n<b>Add all to portfolio?</b>")

    lines.append("Reply <b>YES</b> to confirm, <b>NO</b> to cancel.")
    return "\n".join(lines)


async def apply_confirmation(session: aiohttp.ClientSession, chat_id: int):
    """Apply pending confirmation — add or remove stocks."""
    pending = pending_confirmations.pop(chat_id, None)
    if not pending:
        await send_message(session, chat_id, "No pending action to confirm.")
        return

    action = pending.get("action", "buy")

    if action == "reset":
        count = db.delete_all_positions()
        await send_message(session, chat_id, (
            f"<b>Portfolio reset.</b>\n"
            f"Removed {count} position{'s' if count != 1 else ''}.\n"
            f"Use /add or send a CARTERA message to start fresh."
        ))
        return

    stocks = pending["stocks"]
    results = []

    for s in stocks:
        ticker = s["ticker"]
        shares = s.get("shares")
        price = s.get("price")

        if action == "sell":
            removed = db.remove_position(ticker)
            if removed:
                results.append(f"  🔴 {ticker}: removed")
            else:
                results.append(f"  ⚠️ {ticker}: no open position found")
        else:
            if shares and price:
                db.add_position(ticker, float(shares), float(price), source="telegram-import")
                results.append(f"  🟢 {ticker}: {shares} shares @ ${price:,.2f}")
            else:
                results.append(f"  ⚠️ {ticker}: skipped (missing shares or price)")

    action_label = "Removed from" if action == "sell" else "Added to"
    lines = [f"<b>{action_label} portfolio:</b>\n"] + results
    lines.append("\nView with /portfolio or on the dashboard.")
    await send_message(session, chat_id, "\n".join(lines))


async def handle_pdf(session: aiohttp.ClientSession, chat_id: int, file_id: str, caption: str):
    """Handle PDF documents — extract text, then route to brief/analyze/import."""
    await send_typing(session, chat_id)

    pdf_bytes = await download_photo(session, file_id)
    if not pdf_bytes:
        await send_message(session, chat_id, "Failed to download PDF.")
        return

    import fitz
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(pdf_bytes)
        tmp_path = tmp.name

    doc = fitz.open(tmp_path)
    text = "\n".join(page.get_text() for page in doc)
    log.info("PDF text extracted: %d chars, preview: %s", len(text), text[:200].replace('\n', ' '))

    # Render page images as fallback (max 3 pages, 100 DPI to keep payload small)
    page_images = []
    for i, page in enumerate(doc):
        if i >= 3:
            break
        pix = page.get_pixmap(dpi=100)
        page_images.append(base64.b64encode(pix.tobytes("png")).decode())
    doc.close()

    lower = caption.lower() if caption else ""

    if "sync" in lower or "/sync" in lower:
        from core.wio_parser import parse_statement, sync_holdings_to_db
        try:
            result = parse_statement(tmp_path)
        except Exception as e:
            await send_message(session, chat_id, f"Failed to parse PDF: {e}")
            return
        finally:
            os.unlink(tmp_path)
        holdings = result.get("holdings", [])
        if not holdings:
            await send_message(session, chat_id, "No holdings found in PDF. Is this a Wio Invest statement?")
            return
        sync = sync_holdings_to_db(holdings)
        acct = result.get("account", {})
        lines = [f"<b>Wio Sync Complete</b>"]
        if acct:
            lines.append(f"Account: {acct.get('account_type', '')} ({acct.get('account_number', '')})")
        lines.append(f"Holdings parsed: {len(holdings)}")
        if sync["added"]:
            lines.append(f"\n<b>Added:</b> {', '.join(sync['added'])}")
        if sync["updated"]:
            lines.append(f"<b>Updated:</b> {', '.join(sync['updated'])}")
        if sync["not_in_statement"]:
            lines.append(f"\n⚠️ <b>Not in statement:</b> {', '.join(sync['not_in_statement'])}")
        await send_message(session, chat_id, "\n".join(lines))
        return

    os.unlink(tmp_path)
    log.info("PDF rendered %d page images (sizes: %s)", len(page_images),
             [f"{len(img)//1024}KB" for img in page_images])

    # Try regex extraction from text first — faster and more reliable than vision
    pdf_tickers = []
    if len(text.strip()) >= 50:
        pdf_tickers = list(dict.fromkeys(re.findall(r'\(([A-Z]{1,5})\)', text)))
        log.info("PDF regex extracted %d tickers: %s", len(pdf_tickers), pdf_tickers)

    if "invest" in lower:
        amount_match = re.search(r'(\d[\d,]*\.?\d*)', lower)
        if not amount_match:
            await send_message(session, chat_id, "Include an amount: /invest 5000")
            return
        amount = float(amount_match.group(1).replace(",", ""))
        if pdf_tickers:
            await _run_invest(session, chat_id, amount, pdf_tickers)
        elif page_images:
            await _handle_photo_invest(session, chat_id, page_images, caption, amount)
        else:
            await send_message(session, chat_id, "Could not extract content from PDF.")

    elif any(k in lower for k in ["brief", "breve", "resumen", "?"]):
        if pdf_tickers:
            await _run_brief_for_tickers(session, chat_id, pdf_tickers)
        elif page_images:
            await _handle_photo_brief(session, chat_id, page_images, caption)
        else:
            await send_message(session, chat_id, "Could not extract content from PDF.")

    elif any(k in lower for k in ["analyze", "analysis", "analizar", "analisis"]):
        if pdf_tickers:
            await _run_full_analysis_for_tickers(session, chat_id, pdf_tickers, caption)
        elif page_images:
            await _handle_photo_full_analysis(session, chat_id, page_images, caption)
        else:
            await send_message(session, chat_id, "Could not extract content from PDF.")

    else:
        if len(text.strip()) >= 50:
            await handle_cartera_text(session, chat_id, text[:10000])
        elif page_images:
            await _handle_photo_portfolio_or_analysis(session, chat_id, page_images[0], caption)
        else:
            await send_message(session, chat_id, "Could not extract content from PDF.")


async def handle_photo(session: aiohttp.ClientSession, chat_id: int, photo_sizes: list, caption: str):
    await send_typing(session, chat_id)

    # If caption contains structured CARTERA text, try regex first
    text_extracted = None
    if caption and is_cartera_message(caption):
        text_extracted = parse_cartera_text(caption)

    if text_extracted and text_extracted["stocks"]:
        log.info("Parsed %d stocks from caption text", len(text_extracted["stocks"]))
        pending_confirmations[chat_id] = text_extracted
        await send_message(session, chat_id, format_confirmation(text_extracted))
        return

    file_id = photo_sizes[-1]["file_id"]
    image_bytes = await download_photo(session, file_id)
    if not image_bytes:
        await send_message(session, chat_id, "Failed to download image.")
        return

    b64 = base64.b64encode(image_bytes).decode()

    # Check caption for analysis mode
    lower = caption.lower() if caption else ""
    if "invest" in lower:
        amount_match = re.search(r'(\d[\d,]*\.?\d*)', lower)
        if amount_match:
            amount = float(amount_match.group(1).replace(",", ""))
            await _handle_photo_invest(session, chat_id, [b64], caption, amount)
        else:
            await send_message(session, chat_id, "Include an amount: /invest 5000")
    elif any(k in lower for k in ["brief", "breve", "resumen", "?"]):
        await _handle_photo_brief(session, chat_id, [b64], caption)
    elif any(k in lower for k in ["analyze", "analysis", "analizar", "analisis"]):
        await _handle_photo_full_analysis(session, chat_id, [b64], caption)
    else:
        await _handle_photo_portfolio_or_analysis(session, chat_id, b64, caption)


async def _extract_tickers_from_images(images: list[str], caption: str) -> list[str]:
    """Extract tickers from one or more images using the portfolio extraction prompt.
    Processes images one at a time to avoid overloading the LLM context."""
    seen = set()
    tickers = []

    for idx, b64 in enumerate(images):
        log.info("Extracting tickers from image %d/%d (%dKB)...", idx + 1, len(images), len(b64) // 1024)
        content: list[dict] = []
        if caption:
            content.append({"type": "text", "text": f"Caption: {caption}"})
        content.append({"type": "text", "text": "Extract all stocks from this image with their tickers, share counts, and prices."})
        content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}})

        resp = await call_llm([
            {"role": "system", "content": PORTFOLIO_EXTRACT_PROMPT},
            {"role": "user", "content": content},
        ])

        extracted = parse_json_response(resp)
        stocks = extracted.get("stocks", []) if extracted else []
        for s in stocks:
            t = s.get("ticker", "").upper().strip()
            if t and t not in seen:
                seen.add(t)
                tickers.append(t)

    return tickers


async def _run_brief_for_tickers(session: aiohttp.ClientSession, chat_id: int, tickers: list[str]):
    """Run concise brief analysis for a pre-extracted list of tickers."""
    log.info("Brief analysis — %d tickers: %s", len(tickers), tickers)
    await send_message(session, chat_id, f"Found {len(tickers)} stocks: {', '.join(tickers)}\nAnalyzing...")

    BATCH_SIZE = 5
    portfolio_ctx = _portfolio_context()

    for batch_start in range(0, len(tickers), BATCH_SIZE):
        batch = tickers[batch_start:batch_start + BATCH_SIZE]
        batch_prompt = f"Analyze these {len(batch)} stocks. For each one, give your concise verdict.\n\n"

        for ticker in batch:
            await send_typing(session, chat_id)
            log.info("Fetching market data for %s...", ticker)
            market_data = await get_market_data(ticker)
            y = market_data.get("yahoo", {})
            tv = market_data.get("tradingview", {})
            if "error" not in y:
                brief_data = {
                    "price": y.get("current_price"),
                    "change_pct": round(((y["current_price"] - y["previous_close"]) / y["previous_close"]) * 100, 2) if y.get("current_price") and y.get("previous_close") else None,
                    "52w_low": y.get("fifty_two_week_low"),
                    "52w_high": y.get("fifty_two_week_high"),
                    "pe": y.get("pe_ratio"),
                    "sector": y.get("sector"),
                    "rsi": y.get("rsi_14"),
                    "tv_rec": tv.get("recommendation"),
                    "tv_buy": tv.get("buy_signals"),
                    "tv_sell": tv.get("sell_signals"),
                    "tv_neutral": tv.get("neutral_signals"),
                    "fcf": y.get("free_cashflow"),
                    "roe": y.get("return_on_equity"),
                    "om": y.get("operating_margins"),
                    "de": y.get("debt_to_equity"),
                    "rg": y.get("revenue_growth"),
                    "peg": y.get("peg_ratio"),
                }
                batch_prompt += f"{ticker}: {json.dumps(brief_data)}\n"
            else:
                batch_prompt += f"{ticker}: no data\n"

        batch_prompt += "\n" + portfolio_ctx

        log.info("Calling LLM for brief batch %d-%d of %d tickers...",
                 batch_start + 1, batch_start + len(batch), len(tickers))
        resp = await call_llm([
            {"role": "system", "content": CHAT_SYSTEM_PROMPT},
            {"role": "user", "content": batch_prompt},
        ])

        if resp:
            await send_message(session, chat_id, resp)
        else:
            await send_message(session, chat_id, f"Could not analyze batch: {', '.join(batch)}")


async def _run_full_analysis_for_tickers(session: aiohttp.ClientSession, chat_id: int, tickers: list[str], caption: str):
    """Run full detailed analysis for a pre-extracted list of tickers."""
    log.info("Full analysis — %d tickers: %s", len(tickers), tickers)
    await send_message(session, chat_id, f"Found {len(tickers)} stocks: {', '.join(tickers)}\nRunning full analysis...")

    extraction = {"tickers": tickers, "recommendation": "unknown", "raw_summary": "PDF document"}
    for ticker in tickers:
        await send_typing(session, chat_id)
        log.info("Fetching market data for %s...", ticker)
        market_data = await get_market_data(ticker)
        if "error" in market_data.get("yahoo", {}):
            await send_message(session, chat_id, f"Could not fetch data for {ticker}.")
            continue
        analysis = await core.analyze_ticker(extraction, market_data, caption or "[PDF]")
        if not analysis:
            await send_message(session, chat_id, f"Could not analyze {ticker}.")
            continue
        alert_text = core.format_alert_message(analysis, market_data, "PDF analysis")
        await send_message(session, chat_id, alert_text)


async def _handle_photo_brief(session: aiohttp.ClientSession, chat_id: int, images: list[str], caption: str):
    """Concise BUY/WAIT/AVOID verdicts for all stocks in the image(s)."""
    await send_message(session, chat_id, "Extracting stocks from image...")

    tickers = await _extract_tickers_from_images(images, caption)
    if not tickers:
        await send_message(session, chat_id, "No stocks found in the image.")
        return

    log.info("Brief analysis — extracted %d unique tickers: %s", len(tickers), tickers)
    await send_message(session, chat_id, f"Found {len(tickers)} stocks: {', '.join(tickers)}\nAnalyzing...")

    BATCH_SIZE = 5
    portfolio_ctx = _portfolio_context()

    for batch_start in range(0, len(tickers), BATCH_SIZE):
        batch = tickers[batch_start:batch_start + BATCH_SIZE]
        batch_prompt = f"Analyze these {len(batch)} stocks. For each one, give your concise verdict.\n\n"

        for ticker in batch:
            await send_typing(session, chat_id)
            log.info("Fetching market data for %s...", ticker)
            market_data = await get_market_data(ticker)
            y = market_data.get("yahoo", {})
            tv = market_data.get("tradingview", {})
            if "error" not in y:
                brief_data = {
                    "price": y.get("current_price"),
                    "change_pct": round(((y["current_price"] - y["previous_close"]) / y["previous_close"]) * 100, 2) if y.get("current_price") and y.get("previous_close") else None,
                    "52w_low": y.get("fifty_two_week_low"),
                    "52w_high": y.get("fifty_two_week_high"),
                    "pe": y.get("pe_ratio"),
                    "sector": y.get("sector"),
                    "rsi": y.get("rsi_14"),
                    "tv_rec": tv.get("recommendation"),
                    "tv_buy": tv.get("buy_signals"),
                    "tv_sell": tv.get("sell_signals"),
                    "tv_neutral": tv.get("neutral_signals"),
                    "fcf": y.get("free_cashflow"),
                    "roe": y.get("return_on_equity"),
                    "om": y.get("operating_margins"),
                    "de": y.get("debt_to_equity"),
                    "rg": y.get("revenue_growth"),
                    "peg": y.get("peg_ratio"),
                }
                batch_prompt += f"{ticker}: {json.dumps(brief_data)}\n"
            else:
                batch_prompt += f"{ticker}: no data\n"

        batch_prompt += "\n" + portfolio_ctx

        log.info("Calling LLM for brief batch %d-%d of %d tickers...",
                 batch_start + 1, batch_start + len(batch), len(tickers))
        resp = await call_llm([
            {"role": "system", "content": CHAT_SYSTEM_PROMPT},
            {"role": "user", "content": batch_prompt},
        ])

        if resp:
            await send_message(session, chat_id, resp)
        else:
            await send_message(session, chat_id, f"Could not analyze batch: {', '.join(batch)}")


async def _handle_photo_full_analysis(session: aiohttp.ClientSession, chat_id: int, images: list[str], caption: str):
    """Detailed analysis per stock with verdict first."""
    await send_message(session, chat_id, "Extracting stocks from image...")

    tickers = await _extract_tickers_from_images(images, caption)
    if not tickers:
        await send_message(session, chat_id, "No stocks found in the image.")
        return

    log.info("Full analysis — extracted %d tickers: %s", len(tickers), tickers)
    await send_message(session, chat_id, f"Found {len(tickers)} stocks: {', '.join(tickers)}\nRunning full analysis...")

    extraction = {
        "tickers": tickers,
        "recommendation": "unknown",
        "raw_summary": f"Portfolio image with {len(tickers)} positions",
    }

    for ticker in tickers:
        await send_typing(session, chat_id)
        log.info("Fetching market data for %s...", ticker)
        market_data = await get_market_data(ticker)

        if "error" in market_data.get("yahoo", {}):
            await send_message(session, chat_id, f"Could not fetch data for {ticker}.")
            continue

        analysis = await core.analyze_ticker(extraction, market_data, caption or "[image]")
        if not analysis:
            await send_message(session, chat_id, f"Could not analyze {ticker}.")
            continue

        log.info("Verdict for %s: %s (%s)", ticker, analysis.get("verdict"), analysis.get("confidence"))
        alert_text = core.format_alert_message(analysis, market_data, "Image analysis")
        await send_message(session, chat_id, alert_text)


async def _run_invest(session: aiohttp.ClientSession, chat_id: int, amount: float, tickers: list[str]):
    """Run holistic portfolio optimization across all given tickers."""
    await send_typing(session, chat_id)
    await send_message(session, chat_id,
        f"\U0001f4b0 Optimizing {_fmt(amount)} across {len(tickers)} candidates: {', '.join(tickers)}\n"
        f"Fetching market data...")

    tasks = [get_market_data(t) for t in tickers]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    market_data_dict = {}
    failed = []
    for ticker, result in zip(tickers, results):
        if isinstance(result, Exception):
            failed.append(ticker)
            log.warning("Market data fetch failed for %s: %s", ticker, result)
        elif "error" in result.get("yahoo", {}):
            failed.append(ticker)
        else:
            market_data_dict[ticker] = result

    if failed:
        await send_message(session, chat_id, f"⚠️ Could not fetch data for: {', '.join(failed)}")

    if not market_data_dict:
        await send_message(session, chat_id, "No valid market data found. Check ticker symbols.")
        return

    await send_typing(session, chat_id)
    log.info("Calling optimizer LLM with %d tickers, $%.0f...", len(market_data_dict), amount)
    result = await core.optimize_allocation(amount, list(market_data_dict.keys()), market_data_dict)

    if not result:
        await send_message(session, chat_id, "Optimizer returned no result. Try again.")
        return

    allocations = result.get("allocations", [])
    if not allocations:
        screened = result.get("screened", [])
        excluded = result.get("excluded", [])
        lines = ["⚠️ <b>No actionable stocks found</b>\n"]
        for s in screened + excluded:
            icon = {"BUY": "✅", "SCALE-IN": "\U0001f504", "WAIT": "⏸", "AVOID": "\U0001f534"}.get(s.get("verdict", ""), "❓")
            lines.append(f"{icon} <b>{s.get('ticker', '???')}</b>: {s.get('reason', 'N/A')}")
        lines.append(f"\nRecommendation: Hold {_fmt(amount)} in cash and wait for better entries.")
        await send_message(session, chat_id, "\n".join(lines))
        return

    msg = core.format_invest_message(result, amount)
    log.info("Formatted invest message length: %d chars, chunks: %d", len(msg), len(_split_message(msg)))
    await send_message(session, chat_id, msg)


async def _handle_photo_invest(session: aiohttp.ClientSession, chat_id: int, images: list[str], caption: str, amount: float):
    """Extract tickers from image(s), then run optimizer."""
    await send_message(session, chat_id, f"Extracting stocks from image for {_fmt(amount)} allocation...")

    tickers = await _extract_tickers_from_images(images, caption)
    if not tickers:
        await send_message(session, chat_id, "No stocks found in the image.")
        return

    log.info("Invest via image — extracted %d tickers: %s", len(tickers), tickers)
    await _run_invest(session, chat_id, amount, tickers)


async def _handle_photo_portfolio_or_analysis(session: aiohttp.ClientSession, chat_id: int, b64: str, caption: str):
    """Try portfolio import first; if no stocks found with shares/prices, try analysis."""
    await send_message(session, chat_id, "Analyzing image...")

    content: list[dict] = []
    if caption:
        content.append({"type": "text", "text": f"Caption: {caption}"})
    content.append({"type": "text", "text": "Extract all stocks from this portfolio/watchlist image with their tickers, share counts, and prices."})
    content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})

    resp = await call_llm([
        {"role": "system", "content": PORTFOLIO_EXTRACT_PROMPT},
        {"role": "user", "content": content},
    ])

    extracted = parse_json_response(resp)
    if extracted and extracted.get("stocks"):
        log.info("LLM extracted %d stocks from image", len(extracted["stocks"]))
        pending_confirmations[chat_id] = extracted
        await send_message(session, chat_id, format_confirmation(extracted))
        return

    # No portfolio data found — try as a recommendation image instead
    log.info("No portfolio stocks found, trying as recommendation image...")
    await _handle_photo_brief(session, chat_id, [b64], caption)


async def handle_cartera_text(session: aiohttp.ClientSession, chat_id: int, text: str):
    """Handle structured CARTERA portfolio text messages."""
    await send_typing(session, chat_id)

    extracted = parse_cartera_text(text)

    if not extracted or not extracted["stocks"]:
        # Regex didn't work — fall back to LLM
        resp = await call_llm([
            {"role": "system", "content": PORTFOLIO_EXTRACT_PROMPT},
            {"role": "user", "content": text},
        ])
        extracted = parse_json_response(resp)

    if not extracted or not extracted.get("stocks"):
        await send_message(session, chat_id, "Could not extract stocks from this message.")
        return

    log.info("Extracted %d stocks from cartera text", len(extracted["stocks"]))
    pending_confirmations[chat_id] = extracted
    await send_message(session, chat_id, format_confirmation(extracted))


async def handle_command(session: aiohttp.ClientSession, chat_id: int, command: str, args: str):
    if command == "/start" or command == "/help":
        await send_message(session, chat_id, (
            "<b>Stock Analysis Bot</b>\n\n"
            "I can help you analyze stocks and manage your portfolio.\n\n"
            "<b>Commands:</b>\n"
            "/brief TICKER — Quick BUY/WAIT/AVOID verdict\n"
            "/analyze TICKER — Full analysis with technicals\n"
            "/invest AMOUNT [TICKERS] — Optimized allocation plan\n"
            "/quote TICKER — Raw price quote\n"
            "/chart TICKER [RANGE] — Candlestick chart image (1d/5d/1mo/3mo/1y)\n"
            "/add TICKER SHARES PRICE [STOP] [TARGET] — Add to portfolio\n"
            "/setstop TICKER STOP [TARGET] — Set stop/target levels\n"
            "/levels — Show stop/target distances\n"
            "/remove TICKER — Remove from portfolio\n"
            "/portfolio — View portfolio with P&amp;L\n"
            "/sync — Upload Wio PDF to sync portfolio\n"
            "/reset — Clear all portfolio positions\n"
            "/clear — Clear conversation history\n"
            "/help — Show this message\n\n"
            "<b>Image/PDF analysis:</b>\n"
            "Send a portfolio image or PDF with caption:\n"
            "• <code>/brief</code> or <code>?</code> — concise verdicts for all stocks\n"
            "• <code>/analyze</code> — detailed analysis per stock\n"
            "• <code>/invest AMOUNT</code> — optimized allocation across BUY-worthy stocks\n"
            "• No caption — portfolio import (add positions)\n\n"
            "Or just chat naturally about stocks!"
        ))
        return

    if command == "/clear":
        conversations[chat_id].clear()
        pending_confirmations.pop(chat_id, None)
        await send_message(session, chat_id, "Conversation history and pending actions cleared.")
        return

    if command == "/quote":
        if not args:
            await send_message(session, chat_id, "Usage: /quote TICKER\nExample: /quote AAPL")
            return
        ticker = args.split()[0].upper()
        await send_typing(session, chat_id)
        data = fetch_yahoo_data(ticker)
        if "error" in data:
            await send_message(session, chat_id, f"Error fetching {ticker}: {data['error']}")
            return
        price = data.get("current_price")
        prev = data.get("previous_close")
        change = ""
        if price and prev:
            pct = ((price - prev) / prev) * 100
            arrow = "▲" if pct >= 0 else "▼"
            change = f" ({arrow} {pct:+.1f}%)"
        rsi = data.get("rsi_14", "N/A")
        await send_message(session, chat_id, (
            f"<b>{ticker} — {data.get('name', ticker)}</b>\n"
            f"Price: {_fmt(price)}{change}\n"
            f"52W: {_fmt(data.get('fifty_two_week_low'))} — {_fmt(data.get('fifty_two_week_high'))}\n"
            f"P/E: {data.get('pe_ratio', 'N/A')} | Cap: {_fmt(data.get('market_cap'))}\n"
            f"Vol: {_fmt(data.get('volume'))} | RSI: {rsi}\n"
            f"Sector: {data.get('sector', 'N/A')}"
        ))
        return

    if command == "/chart":
        parts = args.split()
        if not parts:
            await send_message(session, chat_id, "Usage: /chart TICKER [RANGE]\nExample: /chart NVDA 3mo\nRanges: 1d, 5d, 1mo, 3mo, 1y")
            return
        ticker = parts[0].upper()
        period = parts[1].lower() if len(parts) > 1 else "3mo"
        if period not in CHART_RANGE_MAP:
            period = "3mo"
        await send_typing(session, chat_id)

        pos = None
        positions = db.get_portfolio()
        for p in positions:
            if p["ticker"] == ticker:
                pos = p
                break

        entry = (pos.get("unit_cost") or pos["entry_price"]) if pos else None
        stop = pos.get("stop_loss") if pos else None
        target = pos.get("target_price") if pos else None

        image = render_chart(ticker, period, entry_price=entry,
                             stop_loss=stop, target_price=target)
        if not image:
            await send_message(session, chat_id, f"No chart data available for {ticker}.")
            return

        caption_parts = [f"<b>{ticker}</b> — {period.upper()}"]
        if pos:
            yahoo = fetch_yahoo_data(ticker)
            price = yahoo.get("current_price")
            if price and entry:
                pnl_pct = (price - entry) / entry * 100
                caption_parts.append(f"Price: {_fmt(price)} | Entry: {_fmt(entry)}")
                caption_parts.append(f"P&L: {pnl_pct:+.1f}%")
            if stop:
                caption_parts.append(f"🔴 Stop: {_fmt(stop)}")
            if target:
                caption_parts.append(f"🟢 Target: {_fmt(target)}")
        await send_photo(session, chat_id, image, "\n".join(caption_parts))
        return

    if command == "/brief":
        if not args:
            await send_message(session, chat_id, "Usage: /brief TICKER\nExample: /brief AAPL")
            return
        ticker = args.split()[0].upper()
        await send_typing(session, chat_id)
        market_data = await get_market_data(ticker)
        if "error" in market_data.get("yahoo", {}):
            await send_message(session, chat_id, f"Error fetching {ticker}: {market_data['yahoo']['error']}")
            return
        context = f"Give your verdict on {ticker}.\n\nMARKET DATA:\n{json.dumps(market_data, default=str)}{_portfolio_context()}"
        resp = await call_llm([
            {"role": "system", "content": CHAT_SYSTEM_PROMPT},
            {"role": "user", "content": context},
        ])
        await send_message(session, chat_id, resp)
        return

    if command == "/analyze":
        if not args:
            await send_message(session, chat_id, "Usage: /analyze TICKER\nExample: /analyze AAPL")
            return
        ticker = args.split()[0].upper()
        await send_typing(session, chat_id)
        market_data = await get_market_data(ticker)
        y = market_data.get("yahoo", {})

        if "error" in y:
            await send_message(session, chat_id, f"Error fetching {ticker}: {y['error']}")
            return

        context = f"Provide a complete analysis of {ticker}.\n\nMARKET DATA:\n{json.dumps(market_data, indent=2, default=str)}{_portfolio_context()}"
        resp = await call_llm([
            {"role": "system", "content": CHAT_SYSTEM_PROMPT},
            {"role": "user", "content": context},
        ])
        await send_message(session, chat_id, resp)
        return

    if command == "/invest":
        parts = args.split() if args else []
        if not parts:
            await send_message(session, chat_id, (
                "Usage: /invest AMOUNT [TICKERS]\n"
                "Examples:\n"
                "  /invest 5000 AAPL MSFT GOOGL\n"
                "  /invest 5000  (uses recent alerts)\n\n"
                "Or send a portfolio image/PDF with caption:\n"
                "  /invest 5000"
            ))
            return
        try:
            amount = float(parts[0].replace(",", "").replace("$", ""))
        except ValueError:
            await send_message(session, chat_id, "Invalid amount. Usage: /invest 5000 AAPL MSFT GOOGL")
            return
        if amount < 100:
            await send_message(session, chat_id, "Minimum investment amount is $100.")
            return
        if amount > 100_000:
            await send_message(session, chat_id, "Maximum investment amount is $100,000.")
            return

        tickers = [t.upper().strip("$") for t in parts[1:] if len(t) >= 1]
        tickers = [t for t in tickers if t not in COMMON_WORDS and re.match(r'^[A-Z]{1,5}$', t)]

        if not tickers:
            from core.finance import read_analysis_history
            recent = read_analysis_history(alerts_only=True, limit=10)
            alert_tickers = list(dict.fromkeys(
                r.get("ticker", "") for r in recent if r.get("ticker")
            ))
            if alert_tickers:
                tickers = alert_tickers[:10]
                await send_message(session, chat_id,
                    f"No tickers specified. Using {len(tickers)} from recent alerts: {', '.join(tickers)}\n"
                    f"Analyzing...")
            else:
                await send_message(session, chat_id,
                    "No tickers specified and no recent alerts found.\n"
                    "Usage: /invest 5000 AAPL MSFT GOOGL\n"
                    "Or send an image/PDF with caption: /invest 5000")
                return

        await _run_invest(session, chat_id, amount, tickers)
        return

    if command == "/add":
        parts = args.split()
        if len(parts) < 3:
            await send_message(session, chat_id, "Usage: /add TICKER SHARES PRICE [STOP] [TARGET]\nExample: /add AAPL 50 195.00\nExample: /add AAPL 50 195.00 180.00 220.00")
            return
        try:
            ticker = parts[0].upper()
            shares = float(parts[1])
            price = float(parts[2])
            stop_loss = float(parts[3]) if len(parts) > 3 else None
            target_price = float(parts[4]) if len(parts) > 4 else None
        except ValueError:
            await send_message(session, chat_id, "Invalid numbers. Usage: /add TICKER SHARES PRICE [STOP] [TARGET]")
            return
        stop_method = None
        if stop_loss is None:
            yahoo = fetch_yahoo_data(ticker)
            atr = yahoo.get("atr_14")
            beta = yahoo.get("beta", 1.0)
            if atr:
                stop_loss, stop_method = calculate_atr_stop(price, atr, beta or 1.0)
                if target_price is None:
                    target_price = calculate_default_target(price, stop_loss)
        db.add_position(ticker, shares, price, source="telegram",
                        stop_loss=stop_loss, target_price=target_price, stop_method=stop_method)
        cost = shares * price
        lines = [f"<b>Added to portfolio:</b>",
                 f"{ticker}: {shares} shares @ {_fmt(price)} = {_fmt(cost)}"]
        if stop_loss:
            lines.append(f"🔴 Stop: {_fmt(stop_loss)}" + (f" ({stop_method})" if stop_method else ""))
        if target_price:
            lines.append(f"🟢 Target: {_fmt(target_price)}")
        await send_message(session, chat_id, "\n".join(lines))
        return

    if command == "/setstop":
        parts = args.split()
        if len(parts) < 2:
            await send_message(session, chat_id, "Usage: /setstop TICKER STOP [TARGET]\nExample: /setstop NVDA 175 240")
            return
        try:
            ticker = parts[0].upper()
            stop_loss = float(parts[1])
            target_price = float(parts[2]) if len(parts) > 2 else None
        except ValueError:
            await send_message(session, chat_id, "Invalid numbers. Usage: /setstop TICKER STOP [TARGET]")
            return
        db.update_position_levels(ticker, stop_loss=stop_loss, target_price=target_price)
        lines = [f"<b>Updated levels for {ticker}:</b>",
                 f"🔴 Stop: {_fmt(stop_loss)}"]
        if target_price:
            lines.append(f"🟢 Target: {_fmt(target_price)}")
        await send_message(session, chat_id, "\n".join(lines))
        return

    if command == "/levels":
        positions = db.get_positions_with_levels()
        if not positions:
            await send_message(session, chat_id, "No positions with stop/target levels set.\nUse /setstop TICKER STOP [TARGET] to set levels.")
            return
        await send_typing(session, chat_id)
        lines = ["<b>Stop/Target Levels</b>\n"]
        for pos in positions:
            ticker = pos["ticker"]
            yahoo = fetch_yahoo_data(ticker)
            current = yahoo.get("current_price")
            if not current:
                lines.append(f"<b>{ticker}</b>: price unavailable")
                continue
            parts_list = [f"<b>{ticker}</b> @ {_fmt(current)}"]
            if pos["stop_loss"]:
                dist = (current - pos["stop_loss"]) / current * 100
                icon = "⚠️" if dist < 3 else "🔴"
                parts_list.append(f"  {icon} Stop: {_fmt(pos['stop_loss'])} ({dist:.1f}% away)")
            if pos["target_price"]:
                dist = (pos["target_price"] - current) / current * 100
                icon = "✅" if dist < 3 else "🟢"
                parts_list.append(f"  {icon} Target: {_fmt(pos['target_price'])} ({dist:.1f}% away)")
            if pos["stop_method"]:
                parts_list.append(f"  Method: {pos['stop_method']}")
            lines.append("\n".join(parts_list))
        await send_message(session, chat_id, "\n\n".join(lines))
        return

    if command == "/sync":
        await send_message(session, chat_id, "Send your Wio Invest PDF statement as a document with the caption <code>/sync</code> to sync your portfolio.")
        return

    if command == "/remove":
        if not args:
            await send_message(session, chat_id, "Usage: /remove TICKER")
            return
        ticker = args.split()[0].upper()
        removed = db.remove_position(ticker)
        if removed:
            await send_message(session, chat_id, f"Removed {ticker} from portfolio.")
        else:
            await send_message(session, chat_id, f"No open position found for {ticker}.")
        return

    if command == "/reset":
        positions = db.get_portfolio()
        if not positions:
            await send_message(session, chat_id, "Portfolio is already empty.")
            return
        pending_confirmations[chat_id] = {
            "action": "reset",
            "stocks": [],
            "count": len(positions),
        }
        tickers = ", ".join(p["ticker"] for p in positions)
        await send_message(session, chat_id, (
            f"<b>Reset portfolio?</b>\n\n"
            f"This will remove all <b>{len(positions)}</b> open positions:\n"
            f"{tickers}\n\n"
            f"Reply <b>YES</b> to confirm, <b>NO</b> to cancel."
        ))
        return

    if command == "/portfolio":
        positions = db.get_portfolio()
        if not positions:
            await send_message(session, chat_id, "Portfolio is empty. Use /add TICKER SHARES PRICE to add positions.")
            return
        await send_typing(session, chat_id)
        lines = ["<b>Portfolio</b>\n"]
        total_cost = 0.0
        total_value = 0.0
        for pos in positions:
            ticker = pos["ticker"]
            yahoo = fetch_yahoo_data(ticker)
            current = yahoo.get("current_price")
            cost = pos["shares"] * pos["entry_price"]
            value = pos["shares"] * current if current else cost
            pnl = value - cost
            pnl_pct = (pnl / cost * 100) if cost > 0 else 0
            total_cost += cost
            total_value += value
            icon = "🟢" if pnl >= 0 else "🔴"
            entry = pos.get("unit_cost") or pos["entry_price"]
            line = (f"{icon} <b>{ticker}</b>: {pos['shares']:.2f} sh @ {_fmt(entry)}\n"
                    f"   Now: {_fmt(current)} | P&L: {_fmt(pnl)} ({pnl_pct:+.1f}%)")
            if pos.get("stop_loss") and current:
                stop_dist = (current - pos["stop_loss"]) / current * 100
                line += f"\n   Stop: {_fmt(pos['stop_loss'])} ({stop_dist:.1f}% away)"
            if pos.get("target_price") and current:
                tgt_dist = (pos["target_price"] - current) / current * 100
                line += f" | Target: {_fmt(pos['target_price'])} ({tgt_dist:.1f}%)"
            lines.append(line)
        total_pnl = total_value - total_cost
        total_pnl_pct = (total_pnl / total_cost * 100) if total_cost > 0 else 0
        icon = "🟢" if total_pnl >= 0 else "🔴"
        lines.append(f"\n{icon} <b>Total:</b> {_fmt(total_value)} | P&L: {_fmt(total_pnl)} ({total_pnl_pct:+.1f}%)")
        await send_message(session, chat_id, "\n".join(lines))
        return

    await send_message(session, chat_id, f"Unknown command: {command}\nType /help for available commands.")


async def handle_chat(session: aiohttp.ClientSession, chat_id: int, text: str):
    # Check for YES/NO confirmation
    text_lower = text.strip().lower()
    if chat_id in pending_confirmations and text_lower in ("yes", "si", "sí", "y", "ok", "confirm"):
        await apply_confirmation(session, chat_id)
        return
    if chat_id in pending_confirmations and text_lower in ("no", "n", "cancel", "cancelar"):
        pending_confirmations.pop(chat_id, None)
        await send_message(session, chat_id, "Cancelled. No changes made.")
        return

    # Check if this is a structured CARTERA message
    if is_cartera_message(text):
        asyncio.create_task(handle_cartera_text(session, chat_id, text))
        return

    await send_typing(session, chat_id)

    history = conversations[chat_id]
    tickers = detect_tickers(text)
    log.info("Detected tickers: %s", tickers)

    market_context = ""
    if tickers:
        data_parts = []
        for t in tickers[:2]:
            log.info("Fetching market data for %s...", t)
            md = await get_market_data(t)
            if "error" not in md.get("yahoo", {}):
                data_parts.append(f"{t}: {json.dumps(md, default=str)}")
        if data_parts:
            market_context = "\n\nLIVE MARKET DATA:\n" + "\n".join(data_parts)

    user_content = text
    if market_context:
        user_content += market_context
    user_content += _portfolio_context()

    history.append({"role": "user", "content": user_content})
    if len(history) > MAX_HISTORY:
        history[:] = history[-MAX_HISTORY:]

    messages = [{"role": "system", "content": CHAT_SYSTEM_PROMPT}] + history
    log.info("Calling LLM...")
    resp = await call_llm(messages)
    log.info("LLM response: %d chars", len(resp) if resp else 0)

    if not resp:
        resp = "Sorry, I couldn't generate a response. Please try again."

    history.append({"role": "assistant", "content": resp})
    if len(history) > MAX_HISTORY:
        history[:] = history[-MAX_HISTORY:]

    await send_message(session, chat_id, resp)
    log.info("Response sent to %s", chat_id)


PIDFILE = Path(__file__).resolve().parent.parent / "data" / "telegram_bot.pid"


def acquire_pidlock() -> bool:
    """Ensure only one bot instance runs at a time."""
    if PIDFILE.exists():
        old_pid = PIDFILE.read_text().strip()
        if old_pid.isdigit() and Path(f"/proc/{old_pid}").exists():
            log.error("Another bot instance is running (PID %s). Exiting.", old_pid)
            return False
        log.info("Stale PID file found (PID %s gone), removing.", old_pid)
    PIDFILE.write_text(str(os.getpid()))
    return True


def release_pidlock():
    try:
        if PIDFILE.exists() and PIDFILE.read_text().strip() == str(os.getpid()):
            PIDFILE.unlink()
    except Exception:
        pass


async def main():
    if not BOT_TOKEN:
        log.error("ALERT_BOT_TOKEN not set")
        return
    if not AUTHORIZED_CHAT:
        log.error("ALERT_CHAT_ID not set")
        return

    if not acquire_pidlock():
        return

    log.info("=== Interactive Chat Bot Started ===")
    log.info("Authorized chat: %s", AUTHORIZED_CHAT)

    offset = 0
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get(
                    f"{BOT_BASE}/getUpdates",
                    params={"offset": offset, "timeout": 30},
                    timeout=aiohttp.ClientTimeout(total=60),
                ) as resp:
                    if resp.status == 409:
                        await asyncio.sleep(1)
                        continue
                    if resp.status != 200:
                        log.error("getUpdates failed: %s", resp.status)
                        await asyncio.sleep(5)
                        continue
                    data = await resp.json()

                if not data.get("ok"):
                    log.error("getUpdates not ok: %s", data)
                    await asyncio.sleep(5)
                    continue

                for update in data.get("result", []):
                    offset = update["update_id"] + 1
                    message = update.get("message")
                    if not message:
                        continue

                    sender = message.get("from", {})
                    if sender.get("is_bot"):
                        continue

                    chat_id = message["chat"]["id"]
                    if str(chat_id) != str(AUTHORIZED_CHAT):
                        log.info("Ignoring message from unauthorized chat: %s", chat_id)
                        continue

                    photo = message.get("photo")
                    if photo:
                        caption = message.get("caption", "").strip()
                        log.info("Photo from %s (caption: %s)", chat_id, caption[:100] if caption else "none")
                        asyncio.create_task(handle_photo(session, chat_id, photo, caption))
                        continue

                    document = message.get("document")
                    if document and (document.get("mime_type", "") == "application/pdf"):
                        caption = message.get("caption", "").strip()
                        log.info("PDF from %s (caption: %s)", chat_id, caption[:100] if caption else "none")
                        asyncio.create_task(handle_pdf(session, chat_id, document["file_id"], caption))
                        continue

                    text = message.get("text", "").strip()
                    if not text:
                        continue

                    log.info("Message from %s: %s", chat_id, text[:100])

                    if text.startswith("/"):
                        parts = text.split(maxsplit=1)
                        command = parts[0].lower().split("@")[0]
                        args = parts[1] if len(parts) > 1 else ""
                        asyncio.create_task(handle_command(session, chat_id, command, args))
                    else:
                        asyncio.create_task(handle_chat(session, chat_id, text))

            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("Polling error: %s", e, exc_info=True)
                await asyncio.sleep(5)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        release_pidlock()
