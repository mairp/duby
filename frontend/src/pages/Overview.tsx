import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link, useNavigate } from 'react-router-dom';
import { api, type Position } from '../api/client';
import CandlestickChart from '../components/CandlestickChart';

const RANGES = ['1d', '5d', '1mo', '3mo', '1y'] as const;

function fmt(n: number) {
  return n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function MiniChart({ pos, range }: { pos: Position; range: string }) {
  const navigate = useNavigate();
  const { data: ohlc, isLoading } = useQuery({
    queryKey: ['ohlc', pos.ticker, range],
    queryFn: () => api.getOhlc(pos.ticker, range),
  });

  const entryPrice = pos.unit_cost || pos.entry_price;

  return (
    <div
      className="bg-gray-900 rounded-lg p-3 cursor-pointer hover:ring-1 hover:ring-gray-700 transition-all"
      onClick={() => navigate(`/position/${pos.ticker}`)}
    >
      <div className="flex items-baseline justify-between mb-2">
        <span className="text-white font-medium">{pos.ticker}</span>
        {pos.current_price ? (
          <div className="text-right">
            <span className="text-gray-300 text-sm">${fmt(pos.current_price)}</span>
            <span className={`text-xs ml-1.5 ${pos.pnl >= 0 ? 'text-green-400' : 'text-red-400'}`}>
              {pos.pnl >= 0 ? '+' : ''}{pos.pnl_pct.toFixed(1)}%
            </span>
          </div>
        ) : (
          <span className="text-gray-500 text-sm">--</span>
        )}
      </div>
      <div className="rounded overflow-hidden">
        {isLoading ? (
          <div className="h-[200px] flex items-center justify-center text-gray-600 text-xs">Loading...</div>
        ) : ohlc?.data?.length ? (
          <CandlestickChart
            data={ohlc.data}
            entryPrice={entryPrice}
            stopLoss={pos.stop_loss}
            targetPrice={pos.target_price}
            height={200}
          />
        ) : (
          <div className="h-[200px] flex items-center justify-center text-gray-600 text-xs">No data</div>
        )}
      </div>
    </div>
  );
}

export default function Overview() {
  const [range, setRange] = useState<string>('3mo');

  const { data, isLoading } = useQuery({
    queryKey: ['portfolio'],
    queryFn: api.getPortfolio,
  });

  if (isLoading) return <div className="p-8 text-gray-400">Loading portfolio...</div>;
  if (!data?.positions.length) return <div className="p-8 text-gray-400">No positions in portfolio.</div>;

  const positions = data.positions.filter(p => p.current_price != null);

  return (
    <div className="p-6 max-w-[1600px] mx-auto">
      <div className="flex items-center justify-between mb-6">
        <div className="flex items-center gap-4">
          <Link to="/" className="text-blue-400 hover:text-blue-300 text-sm">
            ← Portfolio
          </Link>
          <h1 className="text-2xl font-semibold text-white">All Charts</h1>
          <span className="text-gray-500 text-sm">{positions.length} positions</span>
        </div>
        <div className="flex gap-1">
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
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
        {positions.map(pos => (
          <MiniChart key={pos.id} pos={pos} range={range} />
        ))}
      </div>
    </div>
  );
}
