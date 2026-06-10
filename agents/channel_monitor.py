import json
import asyncio
import base64
import os
import sys
import logging
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
import aiohttp
import fitz
from telethon import TelegramClient, events

import core
from core.finance import init_config, call_llm, parse_json_response, EXTRACTION_PROMPT

load_dotenv(Path(__file__).parent.parent / ".env")
init_config()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
CHANNELS = []
for _c in os.environ.get("TELEGRAM_CHANNELS", "").split(","):
    _c = _c.strip()
    if _c:
        try:
            CHANNELS.append(int(_c))
        except ValueError:
            CHANNELS.append(_c)

ALERT_BOT_TOKEN = os.environ.get("ALERT_BOT_TOKEN", "")
ALERT_CHAT_ID = os.environ.get("ALERT_CHAT_ID", "")

OUTPUT_FILE = os.environ.get("OUTPUT_FILE", "extracted.jsonl")
DATA_DIR = Path(__file__).parent.parent / "data"
MEDIA_DIR = DATA_DIR / os.environ.get("MEDIA_DIR", "media")
MEDIA_DIR.mkdir(parents=True, exist_ok=True)

client = TelegramClient(
    str(Path(__file__).parent.parent / "telegram_session"),
    API_ID, API_HASH,
)

# ---------------------------------------------------------------------------
# PDF / Image helpers
# ---------------------------------------------------------------------------

def extract_pdf_text(pdf_path: str) -> str:
    doc = fitz.open(pdf_path)
    parts = [page.get_text() for page in doc]
    doc.close()
    return "\n".join(parts)


def extract_pdf_images(pdf_path: str, max_pages: int = 5) -> list[str]:
    doc = fitz.open(pdf_path)
    images = []
    for i, page in enumerate(doc):
        if i >= max_pages:
            break
        pix = page.get_pixmap(dpi=150)
        images.append(base64.b64encode(pix.tobytes("png")).decode())
    doc.close()
    return images


def image_to_base64(file_path: str) -> str:
    with open(file_path, "rb") as f:
        return base64.b64encode(f.read()).decode()

# ---------------------------------------------------------------------------
# Stage 1 — extract tickers from channel message
# ---------------------------------------------------------------------------

async def extract_from_text(text: str) -> dict | None:
    resp = await call_llm([
        {"role": "system", "content": EXTRACTION_PROMPT},
        {"role": "user", "content": text},
    ])
    return parse_json_response(resp)


async def extract_from_image(image_path: str, caption: str = "") -> dict | None:
    b64 = image_to_base64(image_path)
    ext = Path(image_path).suffix.lower().lstrip(".")
    mime_map = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                "gif": "image/gif", "webp": "image/webp"}
    media_type = mime_map.get(ext, "image/png")

    content: list[dict] = []
    if caption:
        content.append({"type": "text", "text": f"Caption: {caption}"})
    content.append({"type": "text", "text": "Extract stock ticker symbols and trading recommendations from this image."})
    content.append({"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{b64}"}})

    resp = await call_llm([
        {"role": "system", "content": EXTRACTION_PROMPT},
        {"role": "user", "content": content},
    ])
    return parse_json_response(resp)


async def extract_from_pdf(pdf_path: str, caption: str = "") -> dict | None:
    text = extract_pdf_text(pdf_path)
    if len(text.strip()) > 50:
        prompt = "PDF document"
        if caption:
            prompt += f" (caption: {caption})"
        prompt += f":\n\n{text[:10000]}"
        return await extract_from_text(prompt)

    images = extract_pdf_images(pdf_path)
    if not images:
        return None

    content: list[dict] = []
    if caption:
        content.append({"type": "text", "text": f"Caption: {caption}"})
    content.append({"type": "text", "text": f"PDF with {len(images)} pages. Extract stock tickers and recommendations."})
    for b64 in images:
        content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}})

    resp = await call_llm([
        {"role": "system", "content": EXTRACTION_PROMPT},
        {"role": "user", "content": content},
    ])
    return parse_json_response(resp)

# ---------------------------------------------------------------------------
# Alert sending
# ---------------------------------------------------------------------------

async def send_alert(text: str, message_id: int):
    if not ALERT_BOT_TOKEN or not ALERT_CHAT_ID:
        log.warning("Alert skipped — bot token or chat ID not set")
        return

    url = f"https://api.telegram.org/bot{ALERT_BOT_TOKEN}/sendMessage"
    chunks = [text[i:i + 4000] for i in range(0, len(text), 4000)]
    async with aiohttp.ClientSession() as session:
        for chunk in chunks:
            async with session.post(url, json={
                "chat_id": ALERT_CHAT_ID,
                "text": chunk,
                "parse_mode": "HTML",
            }) as resp:
                if resp.status == 200:
                    log.info("Alert sent for message %d", message_id)
                else:
                    body = await resp.text()
                    log.error("Alert send failed: %s", body)

# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

async def save_result(record: dict):
    output_path = DATA_DIR / OUTPUT_FILE
    with open(output_path, "a") as f:
        f.write(json.dumps(record, default=str) + "\n")

# ---------------------------------------------------------------------------
# Message handler
# ---------------------------------------------------------------------------

@client.on(events.NewMessage(chats=CHANNELS))
async def handler(event):
    msg = event.message
    channel = ""
    if event.chat:
        channel = getattr(event.chat, "title", "") or getattr(event.chat, "username", "") or str(event.chat_id)

    caption = msg.text or ""
    media_type = "text"
    extraction = None

    try:
        if msg.document:
            mime = msg.document.mime_type or ""
            if mime == "application/pdf":
                media_type = "pdf"
                path = await msg.download_media(file=str(MEDIA_DIR))
                log.info("PDF from %s: %s", channel, path)
                extraction = await extract_from_pdf(path, caption)
            elif mime.startswith("image/"):
                media_type = "image"
                path = await msg.download_media(file=str(MEDIA_DIR))
                log.info("Image from %s: %s", channel, path)
                extraction = await extract_from_image(path, caption)
            else:
                log.info("Skipping unsupported document: %s", mime)
                return

        elif msg.photo:
            media_type = "image"
            path = await msg.download_media(file=str(MEDIA_DIR))
            log.info("Photo from %s: %s", channel, path)
            extraction = await extract_from_image(path, caption)

        elif msg.text and len(msg.text.strip()) >= 5:
            media_type = "text"
            log.info("Text from %s: %s", channel, msg.text[:100])
            extraction = await extract_from_text(msg.text)

        else:
            return

        if not extraction:
            log.info("Could not parse extraction response")
            return

        tickers = extraction.get("tickers", [])
        log.info("Extracted tickers: %s (rec: %s)", tickers, extraction.get("recommendation"))

        if not tickers or extraction.get("recommendation") == "none":
            await save_result({
                "timestamp": datetime.utcnow().isoformat(),
                "channel": channel,
                "message_id": msg.id,
                "media_type": media_type,
                "extraction": extraction,
                "action": "ignored_no_tickers",
            })
            log.info("No tickers found, logged and skipped")
            return

        for ticker in tickers[:3]:
            ticker = ticker.upper().strip()
            log.info("Fetching market data for %s ...", ticker)
            market_data = await core.get_market_data(ticker)
            log.info("Market data for %s: price=%s", ticker,
                     market_data.get("yahoo", {}).get("current_price"))

            analysis = await core.analyze_ticker(extraction, market_data, caption or "[media]")
            if not analysis:
                log.warning("Could not parse analyst response for %s", ticker)
                continue

            log.info("Verdict for %s: %s (%s confidence, action=%s)",
                     ticker, analysis.get("verdict"),
                     analysis.get("confidence"), analysis.get("action"))

            await save_result({
                "timestamp": datetime.utcnow().isoformat(),
                "channel": channel,
                "message_id": msg.id,
                "media_type": media_type,
                "ticker": ticker,
                "extraction": extraction,
                "market_data": market_data,
                "analysis": analysis,
            })

            action = analysis.get("action", "log")
            if action == "alert":
                alert_text = core.format_alert_message(analysis, market_data, channel)
                await send_alert(alert_text, msg.id)
            else:
                log.info("%s for %s (action=%s)", "Logged" if action == "log" else "Ignored", ticker, action)

    except Exception as e:
        log.error("Error processing message %d: %s", msg.id, e, exc_info=True)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    from core.finance import BUDGET_MIN, BUDGET_MAX, LLM_MODEL
    await client.start()
    me = await client.get_me()
    log.info("=== Share Analysis Agent Started ===")
    log.info("User: %s (id=%d)", me.username or me.first_name, me.id)
    log.info("Channels: %s", CHANNELS)
    log.info("Budget: $%s-$%s (US market)", f"{BUDGET_MIN:,}", f"{BUDGET_MAX:,}")
    log.info("Model: %s", LLM_MODEL)
    log.info("Output: %s", OUTPUT_FILE)
    if ALERT_BOT_TOKEN and ALERT_CHAT_ID:
        log.info("Alerts: ON (chat %s)", ALERT_CHAT_ID)
    else:
        log.info("Alerts: OFF")
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
