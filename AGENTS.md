# Agents

This project runs five independent agents that share a common data layer (`core/finance.py`, `core/database.py`, `core/timeseries.py`, `core/portfolio_yaml.py`).

## 1. Channel Monitor (`agents/channel_monitor.py`)

**Purpose:** Passive surveillance of Telegram investment channels.

**How it works:**
- Connects to Telegram via Telethon (user API, not bot API)
- Listens for new messages in configured channels (`TELEGRAM_CHANNELS`)
- Processes text, images (via vision LLM), and PDFs (via PyMuPDF + LLM)
- 3-stage pipeline: Extract tickers -> Fetch market data (fundamentals + technicals + ATR) -> LLM analysis
- Sends rich HTML alerts to the user via the Telegram bot when a BUY/SCALE-IN/SELL is detected with medium/high confidence
- Alerts are informational only — the user decides whether to add positions manually
- Logs all results to `extracted.jsonl`

**Trigger:** Real-time — fires on every new message in monitored channels.

**Run:** `./deploy.sh start` (or individually: see below)

**Concurrency:** Single async event loop. Market data fetches run in a ThreadPoolExecutor(3). One message is fully processed before the next starts (per channel).

---

## 2. MCP Finance Server (`agents/mcp_server.py`)

**Purpose:** Expose finance tools to Claude Code so the LLM can query live market data and manage the portfolio during conversation.

**How it works:**
- FastMCP server running on stdio transport
- Launched automatically by Claude Code on session start (configured in `.mcp.json`)
- Exposes 9 tools: stock quotes, technical analysis, full analysis, history search, alerts, portfolio CRUD, stop/target levels
- Blocking Yahoo/TradingView calls run in ThreadPoolExecutor
- Reads/writes `portfolio.db` and reads `extracted.jsonl`

**Trigger:** On-demand — Claude Code calls tools when relevant to the user's question.

**Run:** Automatic (Claude Code spawns it via `.mcp.json`). Manual test: `uv run python -m agents.mcp_server`

**Tools:**

| Tool | Input | Output |
|------|-------|--------|
| `get_stock_quote` | ticker | Price, P/E, cap, 52W, volume, sector, beta |
| `get_technical_analysis` | ticker | RSI, MACD, MAs, TradingView signals |
| `get_full_analysis` | ticker | Combined Yahoo + TradingView JSON |
| `search_analysis_history` | ticker?, limit? | Past analyses from extracted.jsonl |
| `get_recent_alerts` | limit? | Recent BUY/SELL alerts |
| `add_to_portfolio` | ticker, shares, price, stop_loss?, target_price? | Creates position with optional levels (auto-calculates stop from ATR if omitted) |
| `set_position_levels` | ticker, stop_loss?, target_price? | Sets stop/target for existing position |
| `remove_from_portfolio` | ticker | Removes open position |
| `get_portfolio` | — | All positions with live P&L + stop/target distances |

---

## 3. Telegram Chat Bot (`agents/telegram_bot.py`)

**Purpose:** Interactive finance assistant accessible via Telegram bot (configured with `ALERT_BOT_TOKEN`).

**How it works:**
- Long-polls Telegram Bot API via aiohttp (`getUpdates`)
- Only responds to the authorized user (`ALERT_CHAT_ID`)
- Skips bot self-messages (prevents processing its own channel alerts)
- PID lock file (`data/telegram_bot.pid`) prevents multiple instances
- Supports structured commands (`/quote`, `/analyze`, `/chart`, `/add`, `/setstop`, `/levels`, `/sync`, `/portfolio`, `/reset`, etc.)
- Server-side candlestick chart rendering via mplfinance (`/chart` command) with dark theme, volume bars, and entry/stop/target price lines
- Free-form chat: multi-stage ticker detection (explicit $TICKER, uppercase words, Yahoo validation, company name resolution via `yfinance.Search`)
- Portfolio import: accepts CARTERA text messages, portfolio images, and Wio Invest PDF statements (`/sync`)
  - Regex parsing for structured text, vision LLM fallback for images
  - Wio PDF parser for broker statement sync (`core/wio_parser.py`)
  - Human-in-the-loop: always asks YES/NO confirmation before adding/removing positions
- Concise responses: BUY/SCALE-IN/WAIT/AVOID format for stock questions, under 500 chars
- HTML sanitization: escapes stray `<` `>` from LLM text while preserving intentional `<b>` tags
- Smart message chunking: splits long messages on newline boundaries (not mid-HTML-tag) with error recovery (plain-text fallback)
- Maintains per-user conversation history (last 10 messages, in-memory)
- LLM calls routed through LiteLLM proxy (provider-agnostic)

**Trigger:** User messages to the Telegram bot.

**Run:** `./deploy.sh start`

**Commands:**

| Command | Description |
|---------|-------------|
| `/brief TICKER` | Quick BUY/SCALE-IN/WAIT/AVOID verdict with portfolio-aware sizing |
| `/analyze TICKER` | Full detailed analysis (fundamentals first, then technicals) |
| `/invest AMOUNT [TICKERS]` | Multi-factor portfolio optimization with narrative-aware allocation |
| `/quote TICKER` | Raw price + key metrics |
| `/chart TICKER [RANGE]` | Candlestick chart image (1d/5d/1mo/3mo/1y) with entry/stop/target lines |
| `/add TICKER SHARES PRICE [STOP] [TARGET]` | Add position (auto-calculates stop from ATR if omitted) |
| `/setstop TICKER STOP [TARGET]` | Set/update stop-loss and target levels |
| `/levels` | Show all positions with stop/target distances |
| `/sync` | Upload Wio Invest PDF to sync portfolio |
| `/remove TICKER` | Remove position |
| `/portfolio` | Show portfolio with live P&L + stop/target info |
| `/reset` | Clear all positions (with YES/NO confirmation) |
| `/clear` | Reset conversation history |
| `/help` | List commands |
| _(free text)_ | LLM chat with auto ticker detection |
| _(image + caption)_ | `/brief`, `/analyze`, `/invest AMOUNT`, or portfolio import |
| _(PDF + /sync)_ | Wio Invest PDF statement sync |
| _(PDF + caption)_ | Same as image — extracts tickers via regex or vision LLM |
| _(CARTERA text)_ | Portfolio import via regex + LLM fallback |

---

## 4. Price Monitor (`agents/price_monitor.py`)

**Purpose:** Continuous price surveillance with stop/target alerts and portfolio persistence.

**How it works:**
- Polls prices for all positions with stop/target levels set
- 60-second intervals during US market hours (9:30-16:00 ET), 5-minute intervals off-hours
- Alert types: `stop_hit`, `target_reached`, `approaching_stop` (2%), `approaching_target` (2%)
- Dedup via `alerts_log` table (24-hour window, 15-minute cooldown per ticker)
- Sends alerts via Telegram bot API
- Writes price + portfolio snapshots to InfluxDB every 5 minutes during market hours
- Auto-saves portfolio to `data/portfolio.yaml` every 5 minutes
- Auto-loads from `data/portfolio.yaml` on boot if DB is empty
- PID lock file (`data/price_monitor.pid`) prevents multiple instances

**Alert format:**
```
🔴 STOP HIT — AAPL
Price: $142.30 (level: $143.00)
Entry: $150.00 | P&L: -$38.50 (-5.1%)
Action: Consider selling to protect capital
```

**Trigger:** Continuous loop while running.

**Run:** `./deploy.sh start`

---

## 5. FastAPI Backend (`api/`)

**Purpose:** REST API serving the React frontend and providing programmatic portfolio management.

**How it works:**
- FastAPI app on `localhost:8000` with CORS for React dev server
- Reuses `core/` modules directly — no data duplication
- Auto-loads from `data/portfolio.yaml` on boot if DB is empty
- OHLC chart data via `yfinance` with range-to-interval mapping

**Run:** `./deploy.sh start`

**Endpoints:**

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/portfolio` | GET | All positions with live prices, P&L, stop/target distances |
| `/api/portfolio` | POST | Add position with optional stop/target |
| `/api/portfolio/{ticker}` | DELETE | Remove position |
| `/api/portfolio/{ticker}/levels` | PUT | Set stop/target levels |
| `/api/portfolio/sync` | POST | Upload Wio PDF, parse and sync |
| `/api/portfolio/import` | POST | Import YAML/JSON portfolio (merge or replace mode) |
| `/api/portfolio/export` | GET | Download portfolio as YAML |
| `/api/portfolio/save` | POST | Trigger manual save to `data/portfolio.yaml` |
| `/api/portfolio/reconcile` | POST | LLM-powered stop/target recalculation for all positions |
| `/api/charts/ohlc/{ticker}` | GET | OHLC + volume candle data (range=1d/5d/1mo/3mo/1y/max) |
| `/api/quote/{ticker}` | GET | Live quote from Yahoo Finance |
| `/api/technicals/{ticker}` | GET | RSI, ATR, beta, sector + TradingView signals |
| `/api/alerts` | GET | Recent alert history |
| `/api/invest` | POST | Run investment optimizer |
| `/api/health` | GET | Health check |

---

## Shared Data Layer

All agents share:

- **`core/finance.py`** — Market data fetching (Yahoo Finance fundamentals + technicals + ATR(14), TradingView signals), LLM calls (via LiteLLM proxy), multi-factor scoring prompts (optimizer + analyst), ATR stop calculation (`calculate_atr_stop`, `calculate_default_target`), compact market data format for batch optimization (~112 tokens/ticker vs ~308 expanded), JSON parsing, HTML-safe alert/invest formatting (`_sanitize_html()`), company-to-ticker resolution (`yfinance.Search`). Stateless module with `init_config()` for lazy .env loading. No Telegram dependencies.
  - **Enriched data fields:** FCF, revenue/earnings growth, ROE, operating margins, D/E, current ratio, PEG, P/B, analyst target, short float, ATR(14)
  - **Scoring model:** Fundamental Quality (40pts) + Valuation (25pts) + Technical Momentum (20pts) - Risk Deductions (up to -15pts). Verdicts: BUY (>=60), WAIT (40-59), AVOID (<40)
  - **Portfolio constraints:** max 30% per stock, sector cap (40%), correlation check, ATR-based stops, volatility-adjusted sizing

- **`core/database.py`** — SQLite database (`data/portfolio.db`) with WAL journaling for concurrent access. Tables:
  - `positions` — open/closed positions with entry/exit prices, stop_loss, target_price, stop_method, unit_cost
  - `alerts_log` — alert dedup tracking (ticker, type, price, level, timestamp)
  - `price_history` — time-series price snapshots per ticker (legacy, kept for fallback)
  - `portfolio_snapshots` — periodic total portfolio value records (legacy, kept for fallback)

- **`core/timeseries.py`** — InfluxDB 2.x client wrapper for time-series telemetry. Writes price and portfolio snapshots. Graceful degradation if InfluxDB is unreachable. Used by the price monitor telemetry loop.

- **`core/wio_parser.py`** — Parses Wio Invest PDF account statements (DriveWealth equities + GTN UCITS ETFs). Extracts holdings and activity records, syncs to portfolio DB via `upsert_position`.

- **`core/portfolio_yaml.py`** — YAML persistence for portfolio state. `save_portfolio()` dumps DB to `data/portfolio.yaml`, `load_portfolio()` restores from YAML with optional ATR stop calculation. Used by price monitor (auto-save every 5 min), API server (auto-load on boot), and import/export endpoints.

- **`data/extracted.jsonl`** — Append-only log written by Channel Monitor, read by MCP server.

- **`data/portfolio.yaml`** — Persistent portfolio snapshot, auto-saved every 5 minutes by the price monitor. Loaded on boot if DB is empty.

## Infrastructure

- **LiteLLM** — Docker container (`docker-compose.yml`). Provider-agnostic LLM proxy on `localhost:4000`. Configure backend in `litellm/config.yaml`.
- **InfluxDB 2.7** — Docker container (`docker-compose.yml`). Auto-setup with org=`shares`, bucket=`telemetry`, 90-day retention. Runs on `localhost:8086`.

## Running all agents

```bash
# Start everything
./deploy.sh start

# Stop everything
./deploy.sh stop

# Check what's running
./deploy.sh status

# Claude Code (MCP server auto-starts)
claude
```

All five can run simultaneously. The SQLite WAL mode handles concurrent reads/writes safely. InfluxDB handles time-series telemetry.
