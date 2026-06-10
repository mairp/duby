# Portfolio Monitor — React Frontend

React + TypeScript + Vite web UI for the Share Analysis Agent portfolio monitoring system.

## Stack

- React 19 + TypeScript
- Vite 6 (dev server + build)
- Tailwind CSS 4
- TanStack Query (data fetching + caching)
- Lightweight Charts v5 (TradingView's open-source candlestick charts)
- React Router v7

## Running

```bash
npm install
npm run dev     # Dev server at localhost:5173 (proxies /api to localhost:8000)
npm run build   # Production build to dist/
```

Requires the FastAPI backend running on port 8000 (`./deploy.sh start` from the project root).

## Pages

- **Dashboard** (`/`) — Summary cards (value, cost, P&L, position count), positions table with value/P&L/stop/target columns. Click any row to view position detail.
- **All Charts** (`/overview`) — Responsive grid of mini candlestick charts for all positions with a shared range selector (1D/5D/1MO/3MO/1Y). Click any chart to navigate to its detail page.
- **Position Detail** (`/position/:ticker`) — Full candlestick chart with volume histogram, entry/stop/target price lines, range selector (1D/5D/1MO/3MO/1Y/MAX), position info panel, technicals panel.

## API Proxy

The Vite dev server proxies all `/api/*` requests to `http://localhost:8000` (configured in `vite.config.ts`).
