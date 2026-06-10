import { useQuery } from '@tanstack/react-query';
import { useNavigate, Link } from 'react-router-dom';
import { api, type Position } from '../api/client';

function fmt(n: number) {
  return n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function PnlCell({ value, pct }: { value: number; pct: number }) {
  const color = value >= 0 ? 'text-green-400' : 'text-red-400';
  const sign = value >= 0 ? '+' : '';
  return (
    <span className={color}>
      {sign}${fmt(value)} ({sign}{pct.toFixed(1)}%)
    </span>
  );
}

export default function Dashboard() {
  const navigate = useNavigate();
  const { data, isLoading, error } = useQuery({
    queryKey: ['portfolio'],
    queryFn: api.getPortfolio,
    refetchInterval: 60_000,
  });

  if (isLoading) return <div className="p-8 text-gray-400">Loading portfolio...</div>;
  if (error) return <div className="p-8 text-red-400">Error: {(error as Error).message}</div>;
  if (!data) return null;

  const { positions, summary } = data;

  return (
    <div className="p-6 max-w-7xl mx-auto">
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-semibold text-white">Portfolio</h1>
        <Link
          to="/overview"
          className="px-4 py-2 bg-gray-800 text-gray-300 rounded-lg text-sm font-medium hover:bg-gray-700 hover:text-white transition-colors"
        >
          All Charts
        </Link>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-8">
        <Card label="Total Value" value={`$${fmt(summary.total_value)}`} />
        <Card label="Total Cost" value={`$${fmt(summary.total_cost)}`} />
        <Card
          label="P&L"
          value={`${summary.pnl >= 0 ? '+' : ''}$${fmt(summary.pnl)}`}
          sub={`${summary.pnl >= 0 ? '+' : ''}${summary.pnl_pct.toFixed(2)}%`}
          color={summary.pnl >= 0 ? 'text-green-400' : 'text-red-400'}
        />
        <Card label="Positions" value={String(summary.position_count)} />
      </div>

      <div className="bg-gray-900 rounded-lg overflow-hidden">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-gray-800 text-gray-400 text-left">
              <th className="px-4 py-3">Ticker</th>
              <th className="px-4 py-3 text-right">Shares</th>
              <th className="px-4 py-3 text-right">Price</th>
              <th className="px-4 py-3 text-right">Value</th>
              <th className="px-4 py-3 text-right">P&L</th>
              <th className="px-4 py-3 text-right">Stop</th>
              <th className="px-4 py-3 text-right">Target</th>
              <th className="px-4 py-3 text-right">To Stop</th>
              <th className="px-4 py-3 text-right">To Target</th>
            </tr>
          </thead>
          <tbody>
            {positions.map((pos: Position) => (
              <tr
                key={pos.id}
                className="border-b border-gray-800/50 hover:bg-gray-800/50 cursor-pointer transition-colors"
                onClick={() => navigate(`/position/${pos.ticker}`)}
              >
                <td className="px-4 py-3 font-medium text-white">{pos.ticker}</td>
                <td className="px-4 py-3 text-right text-gray-300">{pos.shares.toFixed(2)}</td>
                <td className="px-4 py-3 text-right text-gray-300">
                  {pos.current_price ? `$${fmt(pos.current_price)}` : '—'}
                </td>
                <td className="px-4 py-3 text-right text-gray-300">
                  ${fmt(pos.value)}
                </td>
                <td className="px-4 py-3 text-right">
                  <PnlCell value={pos.pnl} pct={pos.pnl_pct} />
                </td>
                <td className="px-4 py-3 text-right text-gray-400">
                  {pos.stop_loss ? `$${fmt(pos.stop_loss)}` : '—'}
                </td>
                <td className="px-4 py-3 text-right text-gray-400">
                  {pos.target_price ? `$${fmt(pos.target_price)}` : '—'}
                </td>
                <td className="px-4 py-3 text-right">
                  {pos.stop_distance_pct != null ? (
                    <span className={pos.stop_distance_pct < 3 ? 'text-red-400 font-medium' : 'text-gray-400'}>
                      {pos.stop_distance_pct.toFixed(1)}%
                    </span>
                  ) : '—'}
                </td>
                <td className="px-4 py-3 text-right">
                  {pos.target_distance_pct != null ? (
                    <span className={pos.target_distance_pct < 3 ? 'text-green-400 font-medium' : 'text-gray-400'}>
                      {pos.target_distance_pct.toFixed(1)}%
                    </span>
                  ) : '—'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function Card({ label, value, sub, color }: { label: string; value: string; sub?: string; color?: string }) {
  return (
    <div className="bg-gray-900 rounded-lg p-4">
      <div className="text-gray-400 text-xs mb-1">{label}</div>
      <div className={`text-xl font-semibold ${color || 'text-white'}`}>{value}</div>
      {sub && <div className={`text-sm ${color || 'text-gray-400'}`}>{sub}</div>}
    </div>
  );
}
