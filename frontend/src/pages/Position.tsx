import { useState } from 'react';
import { useParams, Link } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { api } from '../api/client';
import CandlestickChart from '../components/CandlestickChart';

const RANGES = ['1d', '5d', '1mo', '3mo', '1y', 'max'] as const;

function fmt(n: number) {
  return n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

export default function PositionPage() {
  const { ticker } = useParams<{ ticker: string }>();
  const [range, setRange] = useState<string>('3mo');

  const { data: portfolio } = useQuery({
    queryKey: ['portfolio'],
    queryFn: api.getPortfolio,
  });

  const { data: ohlc, isLoading: chartLoading } = useQuery({
    queryKey: ['ohlc', ticker, range],
    queryFn: () => api.getOhlc(ticker!, range),
    enabled: !!ticker,
  });

  const { data: technicals } = useQuery({
    queryKey: ['technicals', ticker],
    queryFn: () => api.getTechnicals(ticker!),
    enabled: !!ticker,
  });

  const pos = portfolio?.positions.find(p => p.ticker === ticker?.toUpperCase());
  const yahoo = (technicals as any)?.yahoo || {};
  const tv = (technicals as any)?.tradingview || {};

  const entryPrice = pos ? (pos.unit_cost || pos.entry_price) : null;

  return (
    <div className="p-6 max-w-7xl mx-auto">
      <Link to="/" className="text-blue-400 hover:text-blue-300 text-sm mb-4 inline-block">
        ← Portfolio
      </Link>

      <div className="flex items-baseline gap-4 mb-2">
        <h1 className="text-2xl font-semibold text-white">{ticker?.toUpperCase()}</h1>
        {yahoo.name && <span className="text-gray-400">{yahoo.name}</span>}
      </div>

      {pos?.current_price && (
        <div className="flex items-baseline gap-3 mb-6">
          <span className="text-3xl font-semibold text-white">${fmt(pos.current_price)}</span>
          <span className={pos.pnl >= 0 ? 'text-green-400 text-lg' : 'text-red-400 text-lg'}>
            {pos.pnl >= 0 ? '+' : ''}${fmt(pos.pnl)} ({pos.pnl >= 0 ? '+' : ''}{pos.pnl_pct.toFixed(1)}%)
          </span>
        </div>
      )}

      <div className="flex gap-1 mb-4">
        {RANGES.map(r => (
          <button
            key={r}
            onClick={() => setRange(r)}
            className={`px-3 py-1 rounded text-sm font-medium transition-colors ${
              range === r
                ? 'bg-blue-600 text-white'
                : 'bg-gray-800 text-gray-400 hover:bg-gray-700 hover:text-gray-200'
            }`}
          >
            {r.toUpperCase()}
          </button>
        ))}
      </div>

      <div className="bg-gray-900 rounded-lg p-2 mb-6">
        {chartLoading ? (
          <div className="h-[500px] flex items-center justify-center text-gray-500">Loading chart...</div>
        ) : ohlc?.data?.length ? (
          <CandlestickChart
            data={ohlc.data}
            entryPrice={entryPrice}
            stopLoss={pos?.stop_loss}
            targetPrice={pos?.target_price}
          />
        ) : (
          <div className="h-[500px] flex items-center justify-center text-gray-500">No data available</div>
        )}
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        {pos && (
          <div className="bg-gray-900 rounded-lg p-5">
            <h2 className="text-sm font-medium text-gray-400 mb-4">Position</h2>
            <dl className="space-y-2 text-sm">
              <Row label="Shares" value={pos.shares.toFixed(4)} />
              <Row label="Avg Cost" value={`$${fmt(entryPrice || pos.entry_price)}`} />
              <Row label="Cost Basis" value={`$${fmt(pos.cost)}`} />
              <Row label="Market Value" value={`$${fmt(pos.value)}`} />
              <Row
                label="P&L"
                value={`${pos.pnl >= 0 ? '+' : ''}$${fmt(pos.pnl)} (${pos.pnl >= 0 ? '+' : ''}${pos.pnl_pct.toFixed(1)}%)`}
                color={pos.pnl >= 0 ? 'text-green-400' : 'text-red-400'}
              />
              <Row
                label="Stop Loss"
                value={pos.stop_loss ? `$${fmt(pos.stop_loss)}` : '—'}
                sub={pos.stop_distance_pct != null ? `${pos.stop_distance_pct.toFixed(1)}% away` : undefined}
                color={pos.stop_distance_pct != null && pos.stop_distance_pct < 3 ? 'text-red-400' : undefined}
              />
              <Row
                label="Target"
                value={pos.target_price ? `$${fmt(pos.target_price)}` : '—'}
                sub={pos.target_distance_pct != null ? `${pos.target_distance_pct.toFixed(1)}% away` : undefined}
                color={pos.target_distance_pct != null && pos.target_distance_pct < 3 ? 'text-green-400' : undefined}
              />
              {pos.stop_method && <Row label="Stop Method" value={pos.stop_method} />}
              <Row label="Source" value={pos.source} />
            </dl>
          </div>
        )}

        <div className="bg-gray-900 rounded-lg p-5">
          <h2 className="text-sm font-medium text-gray-400 mb-4">Technicals</h2>
          <dl className="space-y-2 text-sm">
            <Row label="RSI(14)" value={yahoo.rsi_14 ? `${yahoo.rsi_14.toFixed(1)}` : '—'}
              color={yahoo.rsi_14 > 70 ? 'text-red-400' : yahoo.rsi_14 < 30 ? 'text-green-400' : undefined} />
            <Row label="ATR(14)" value={yahoo.atr_14 ? `$${yahoo.atr_14.toFixed(2)}` : '—'} />
            <Row label="Beta" value={yahoo.beta ? yahoo.beta.toFixed(2) : '—'} />
            <Row label="P/E" value={yahoo.trailing_pe ? yahoo.trailing_pe.toFixed(1) : '—'} />
            <Row label="52W Range" value={
              yahoo['52w_low'] && yahoo['52w_high']
                ? `$${fmt(yahoo['52w_low'])} — $${fmt(yahoo['52w_high'])}`
                : '—'
            } />
            <Row label="Sector" value={yahoo.sector || '—'} />
            {tv.summary && <Row label="TradingView" value={tv.summary} />}
          </dl>
        </div>
      </div>
    </div>
  );
}

function Row({ label, value, sub, color }: { label: string; value: string; sub?: string; color?: string }) {
  return (
    <div className="flex justify-between">
      <dt className="text-gray-500">{label}</dt>
      <dd className={color || 'text-gray-200'}>
        {value}
        {sub && <span className="text-xs ml-1 text-gray-500">({sub})</span>}
      </dd>
    </div>
  );
}
