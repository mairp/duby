const BASE = '/api';

async function fetchJson<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${url}`, init);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

export interface Position {
  id: number;
  ticker: string;
  shares: number;
  entry_price: number;
  unit_cost: number | null;
  stop_loss: number | null;
  target_price: number | null;
  stop_method: string | null;
  current_price: number | null;
  value: number;
  cost: number;
  pnl: number;
  pnl_pct: number;
  stop_distance_pct: number | null;
  target_distance_pct: number | null;
  source: string;
  entry_date: string;
}

export interface PortfolioSummary {
  total_value: number;
  total_cost: number;
  pnl: number;
  pnl_pct: number;
  position_count: number;
}

export interface PortfolioResponse {
  positions: Position[];
  summary: PortfolioSummary;
}

export interface OhlcBar {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface OhlcResponse {
  ticker: string;
  range: string;
  interval: string;
  data: OhlcBar[];
}

export interface Alert {
  id: number;
  ticker: string;
  alert_type: string;
  price_at_alert: number;
  level: number;
  sent_at: string;
}

export const api = {
  getPortfolio: () => fetchJson<PortfolioResponse>('/portfolio'),
  getOhlc: (ticker: string, range = '3mo') =>
    fetchJson<OhlcResponse>(`/charts/ohlc/${ticker}?range=${range}`),
  getQuote: (ticker: string) => fetchJson<Record<string, unknown>>(`/quote/${ticker}`),
  getTechnicals: (ticker: string) => fetchJson<Record<string, unknown>>(`/technicals/${ticker}`),
  getAlerts: (limit = 50) => fetchJson<Alert[]>(`/alerts?limit=${limit}`),
  setLevels: (ticker: string, stop_loss?: number, target_price?: number) =>
    fetchJson(`/portfolio/${ticker}/levels`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ stop_loss, target_price }),
    }),
  removePosition: (ticker: string) =>
    fetchJson(`/portfolio/${ticker}`, { method: 'DELETE' }),
};
