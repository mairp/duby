# Share Analysis Agent

## Overview
AI-powered investment analysis platform that monitors Telegram channels for stock recommendations, enriches them with live market data, and provides multi-interface access: Claude Code MCP tools, interactive Telegram bot (configurable via `ALERT_BOT_TOKEN`), React web UI with TradingView-style candlestick charts, and a continuous price monitoring agent with Telegram alerts.

## Stack
- **Runtime:** Python 3.13+ via `uv` (project at repo root)
- **Telegram client:** Telethon (user API for channel monitoring)
- **Telegram bot:** Bot API via aiohttp (interactive chat + alerts)
- **LLM:** LiteLLM proxy (Docker, port 4000) — provider-agnostic gateway to any OpenAI-compatible API
- **Market data:** yfinance (price, fundamentals, technicals), tradingview-ta (signals) — both free, no API keys required
- **PDF processing:** PyMuPDF (`fitz`) — includes Wio Invest statement parser
- **MCP:** FastMCP server for Claude Code integration (stdio transport)
- **API:** FastAPI backend serving REST endpoints on port 8000
- **Frontend:** React 19 + Vite + TypeScript + Tailwind CSS + Lightweight Charts (TradingView)
- **Telemetry:** InfluxDB 2.7 via Docker (time-series price/portfolio snapshots, 90-day retention)
- **Portfolio store:** SQLite (`portfolio.db`) — shared across all interfaces (WAL mode)
- **Config:** `.env` file (do NOT commit — contains API keys)

## Running

```bash
cd /path/to/duby

# Start everything (Docker + all agents + API + frontend)
./deploy.sh start

# Stop everything
./deploy.sh stop

# Check what's running
./deploy.sh status
```

The MCP server starts automatically when Claude Code loads (configured in `.mcp.json`).

All services can run concurrently without conflicts — they share `data/portfolio.db` (WAL mode) and `data/extracted.jsonl` but use separate processes.

## Project structure

```
shares/
├── core/                   # Shared libraries (no import side effects)
│   ├── __init__.py         # Re-exports all public functions
│   ├── finance.py          # Market data, LLM, analysis utilities
│   ├── database.py         # SQLite portfolio store (WAL mode)
│   ├── timeseries.py       # InfluxDB telemetry (price + portfolio snapshots)
│   ├── wio_parser.py       # Wio Invest PDF statement parser
│   └── portfolio_yaml.py   # YAML persistence (save/load portfolio state)
├── agents/                 # Standalone agent processes
│   ├── __init__.py
│   ├── channel_monitor.py  # Telethon channel monitor (3-stage pipeline)
│   ├── mcp_server.py       # FastMCP server (9 tools, stdio transport)
│   ├── telegram_bot.py     # Interactive Telegram bot (Bot API)
│   └── price_monitor.py    # Continuous price monitor with stop/target alerts
├── api/                    # FastAPI REST backend
│   ├── main.py             # App, CORS, lifespan
│   └── routes/
│       ├── portfolio.py    # Positions CRUD, levels, Wio PDF sync, YAML import/export, LLM reconcile
│       ├── market.py       # Quotes, technicals, OHLC chart data
│       ├── alerts.py       # Alert history
│       └── invest.py       # Investment optimizer endpoint
├── frontend/               # React+Vite+TypeScript web UI
│   ├── src/
│   │   ├── api/client.ts   # Typed API client
│   │   ├── components/
│   │   │   └── CandlestickChart.tsx  # Lightweight Charts candlestick + volume + price lines
│   │   └── pages/
│   │       ├── Dashboard.tsx   # Portfolio overview with summary cards + positions table + value column
│   │       ├── Overview.tsx    # Grid of all candlestick charts with shared range selector
│   │       └── Position.tsx    # Stock detail: chart + position info + technicals
│   ├── vite.config.ts      # Tailwind + API proxy to localhost:8000
│   └── package.json
├── data/                   # Runtime data (gitignored)
│   ├── portfolio.db        # SQLite database (created automatically)
│   ├── portfolio.yaml      # Persistent portfolio state (auto-saved every 5 min)
│   ├── extracted.jsonl     # Append-only analysis log
│   └── media/              # Downloaded images and PDFs
├── litellm/
│   └── config.yaml         # LiteLLM proxy config (model routing)
├── docs/                   # Screenshots and documentation assets
├── my_portfolio.yaml       # User-editable portfolio template (mode: replace)
├── scripts/
│   └── get_channel_id.sh   # Utility to find Telegram channel IDs
├── .mcp.json               # Claude Code MCP server configuration
├── .env                    # Credentials (not in git)
├── .env.example            # Template for required variables
├── telegram_session.session # Telethon auth (do NOT delete)
├── docker-compose.yml      # LiteLLM + InfluxDB containers
└── deploy.sh               # Start/stop/restart/status for all services
```

### Key modules
- `core/finance.py` — shared market data (enriched fundamentals: FCF, ROE, margins, D/E, ATR(14)), LLM calls, multi-factor scoring prompts, ATR stop calculation, compact market data format for batch optimization, HTML-safe message formatting. No import side effects. Call `init_config()` before use.
- `core/database.py` — SQLite portfolio store with WAL mode. Tables: `positions` (with stop_loss, target_price, stop_method, unit_cost columns), `price_history`, `portfolio_snapshots`, `alerts_log`.
- `core/timeseries.py` — InfluxDB 2.x wrapper for time-series telemetry. Graceful degradation if InfluxDB is down.
- `core/wio_parser.py` — Parses Wio Invest PDF statements (DriveWealth equities + GTN UCITS ETFs). Extracts holdings and activity, syncs to portfolio DB.
- `agents/channel_monitor.py` — channel monitoring agent (Telethon, 3-stage pipeline). Imports from `core`.
- `agents/mcp_server.py` — MCP server exposing 9 finance tools to Claude Code via stdio.
- `agents/telegram_bot.py` — interactive Telegram bot. Bot API long-polling via aiohttp. Only responds to `ALERT_CHAT_ID`. Skips bot self-messages. Smart message chunking (splits on newline boundaries), HTML error recovery (falls back to plain text on parse failure). Server-side candlestick chart rendering via mplfinance (`/chart` command).
- `core/portfolio_yaml.py` — YAML persistence for portfolio state. `save_portfolio()` dumps DB to `data/portfolio.yaml`, `load_portfolio()` restores from YAML with optional ATR stop calculation. Auto-saved every 5 min by price monitor, auto-loaded on boot if DB is empty.
- `agents/price_monitor.py` — continuous price monitoring agent. Polls prices (60s market hours, 5min off-hours), checks stop/target levels, sends Telegram alerts with 24h dedup, writes telemetry to InfluxDB, auto-saves portfolio YAML every 5 minutes.
- `api/` — FastAPI backend reusing `core/` modules directly. CORS configured for React dev server. Includes YAML import/export, LLM-powered stop/target reconciliation.

## Architecture

### Data flow
```
Telegram Channels ──Telethon──> agents/channel_monitor.py ──> data/extracted.jsonl
                                       |
Claude Code ──MCP──> agents/mcp_server.py ──┐
                                             |──> core/database.py ──> data/portfolio.db
Telegram Bot ──Bot API──> agents/telegram_bot.py ──┘         |
                                                              |
agents/price_monitor.py ──polls prices──> alerts via Telegram  |
                           └──> core/timeseries.py ──> InfluxDB
                                                              |
React UI ──> api/ (FastAPI :8000) ──> core/ ──> portfolio.db + yfinance
```

### Channel Monitor (3-stage pipeline)
1. **Extractor:** Telethon listens to configured channels. Incoming messages (text, image, PDF) are sent to the LLM to extract ticker symbols, entry price, targets, stop loss.
2. **Market Data:** For each ticker, fetches concurrently from Yahoo Finance (fundamentals + technicals) and TradingView (signal counts + consensus).
3. **Trading Analyst:** LLM evaluates the recommendation against live data and the client's budget. Returns verdict (BUY/SELL/HOLD/SKIP), position sizing, risk/reward.
4. If verdict warrants an alert (BUY/SELL with medium/high confidence), a rich formatted message is sent via the Telegram bot.

### Price Monitor Agent
- Polls prices for all positions with stop/target levels set
- 60s during US market hours (9:30-16:00 ET), 5min off-hours
- Alert types: `stop_hit`, `target_reached`, `approaching_stop` (2%), `approaching_target` (2%)
- Dedup via `alerts_log` table (24h window, 15min cooldown per ticker)
- Writes telemetry to InfluxDB (price + portfolio snapshots to InfluxDB every 5 min during market hours)
- Auto-saves portfolio to `data/portfolio.yaml` every 5 minutes
- Auto-loads from `data/portfolio.yaml` on boot if DB is empty

### FastAPI Endpoints
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

### React Frontend (Lightweight Charts)
- **Dashboard:** Summary cards (value, cost, P&L, position count) + positions table with value/P&L/stop/target columns (click row → detail page). "All Charts" button links to overview.
- **All Charts (Overview):** Responsive grid of mini candlestick charts for all positions with shared range selector. Click any chart → position detail.
- **Position detail:** Candlestick chart with volume histogram, entry/stop/target price lines, range selector (1D/5D/1MO/3MO/1Y/MAX), position info panel, technicals panel

### MCP Tools (Claude Code)
| Tool | Description |
|------|-------------|
| `get_stock_quote(ticker)` | Yahoo Finance price, P/E, market cap, 52W range, volume, sector |
| `get_technical_analysis(ticker)` | RSI(14), MACD, 20/50-day MAs + TradingView signals |
| `get_full_analysis(ticker)` | Combined Yahoo + TradingView market data |
| `search_analysis_history(ticker?, limit?)` | Search past analyses from extracted.jsonl |
| `get_recent_alerts(limit?)` | Recent BUY/SELL alerts sent to Telegram |
| `add_to_portfolio(ticker, shares, entry_price, stop_loss?, target_price?)` | Add stock position with optional levels |
| `set_position_levels(ticker, stop_loss?, target_price?)` | Set stop/target for existing position |
| `remove_from_portfolio(ticker)` | Remove stock from portfolio |
| `get_portfolio()` | Current portfolio with live P&L, stop/target distances |

### Telegram Bot Commands
- `/brief TICKER` — Quick BUY/SCALE-IN/WAIT/AVOID verdict with portfolio-aware sizing
- `/analyze TICKER` — Full detailed analysis (verdict first, then market data + technicals)
- `/invest AMOUNT [TICKERS]` — Multi-factor portfolio optimization with narrative-aware allocation
- `/quote TICKER` — Raw price quote with key metrics
- `/chart TICKER [RANGE]` — Candlestick chart image (1d/5d/1mo/3mo/1y) with entry/stop/target lines
- `/add TICKER SHARES PRICE [STOP] [TARGET]` — Add stock to portfolio (auto-calculates stop from ATR if omitted)
- `/setstop TICKER STOP [TARGET]` — Set/update stop-loss and target levels
- `/levels` — Show all positions with stop/target distances
- `/sync` — Upload Wio PDF statement to sync portfolio
- `/remove TICKER` — Remove stock from portfolio
- `/portfolio` — View all positions with live P&L and stop/target info
- `/reset` — Clear all portfolio positions (with confirmation)
- `/clear` — Clear conversation history
- `/help` — List commands
- Free-form chat — auto-detects tickers, fetches market data, responds via LLM

### Image & PDF Analysis
Send a portfolio image or PDF to the bot with a caption:
- `/brief` or `?` — concise BUY/WAIT/AVOID verdicts for all stocks
- `/analyze` — full detailed analysis per stock (verdict first)
- `/invest AMOUNT` — optimized allocation plan distributing the amount across BUY-worthy stocks
- `/sync` — parse Wio Invest statement and sync holdings to portfolio
- No caption — portfolio import (extract stocks, ask YES/NO confirmation before adding)

### Investment Optimizer (`/invest`)
Unlike `/brief` (individual verdicts) and `/analyze` (individual detailed analysis), `/invest` performs **holistic portfolio optimization** using a multi-factor scoring model:

**Multi-factor scoring (0-100):**
- Fundamental Quality (40pts): FCF yield, ROE, revenue/earnings growth, operating margins, balance sheet
- Valuation (25pts): PEG, forward P/E vs sector, P/B, analyst target upside
- Technical Momentum (20pts): RSI zone, MACD, TradingView consensus, price momentum
- Risk Deductions (up to -15pts): high beta + speculative, short float, negative FCF + high P/E, high D/E, declining growth

**Verdicts:** BUY (score >= 60), WAIT (40-59), AVOID (<40)

**Portfolio construction constraints:**
- Max 30% per stock, max 40% per sector
- ATR-based stop losses (not arbitrary round numbers)
- Volatility-adjusted sizing: high-beta stocks get smaller positions
- Correlation check: flags sector and momentum concentration
- Quality tags: COMPOUNDER, VALUE, GROWTH, CYCLICAL, SPECULATIVE, TURNAROUND
- Cash deployment scaled by number of BUY candidates (5% minimum reserve)
- Accounts for existing portfolio holdings to avoid doubling down

## Environment variables
- `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` — from my.telegram.org
- `TELEGRAM_CHANNELS` — comma-separated channel IDs (negative numbers starting with -100)
- `LLM_API_KEY` / `LLM_API_URL` / `LLM_MODEL` — LiteLLM proxy connection (default: localhost:4000)
- `LLM_BACKEND_API_BASE` / `LLM_BACKEND_API_KEY` — actual LLM provider credentials (passed to LiteLLM via docker-compose)
- `ALERT_BOT_TOKEN` — bot token from @BotFather (create your own bot)
- `ALERT_CHAT_ID` — user's Telegram numeric ID (from @userinfobot)
- `BUDGET_MIN` / `BUDGET_MAX` — investment budget range in USD (default: 10000/15000)
- `OUTPUT_FILE` — path to JSONL output (default: extracted.jsonl)
- `MEDIA_DIR` — path for downloaded media (default: media/)
- `INFLUXDB_URL` / `INFLUXDB_TOKEN` / `INFLUXDB_ORG` / `INFLUXDB_BUCKET` — InfluxDB 2.x telemetry config (default: localhost:8086, shares org, telemetry bucket)

## Dependencies
- **Python** (in `pyproject.toml`): telethon, aiohttp, pymupdf, python-dotenv, yfinance, tradingview-ta, mcp, fastapi, uvicorn, influxdb-client
- **Frontend** (managed in `frontend/package.json`): react, lightweight-charts, @tanstack/react-query, react-router-dom, tailwindcss
- **Charts** (server-side): mplfinance + matplotlib for Telegram bot chart images

## Important notes
- Never commit `.env`, `portfolio.db`, `telegram_session.session`, or `extracted.jsonl`
- The Telegram session file is tied to the user's phone number — deleting it requires re-authentication
- Yahoo Finance and TradingView data are free but rate-limited — the system uses ThreadPoolExecutor(3) to avoid hammering
- All LLM calls go through the LiteLLM proxy (port 4000) — provider-agnostic, swap backends by editing `litellm/config.yaml`
- The MCP server logs to stderr (stdout is reserved for the MCP protocol)
