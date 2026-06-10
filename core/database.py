import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "portfolio.db"


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    with _conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                shares REAL NOT NULL,
                entry_price REAL NOT NULL,
                entry_date TEXT NOT NULL,
                exit_price REAL,
                exit_date TEXT,
                source TEXT DEFAULT 'manual',
                status TEXT DEFAULT 'open'
            );
            CREATE INDEX IF NOT EXISTS idx_positions_ticker ON positions(ticker);
            CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);

            CREATE TABLE IF NOT EXISTS price_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                price REAL NOT NULL,
                volume INTEGER,
                rsi REAL,
                timestamp TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_price_history_ticker ON price_history(ticker);
            CREATE INDEX IF NOT EXISTS idx_price_history_ts ON price_history(timestamp);

            CREATE TABLE IF NOT EXISTS portfolio_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                total_value REAL NOT NULL,
                total_cost REAL NOT NULL,
                pnl REAL NOT NULL,
                pnl_pct REAL NOT NULL,
                timestamp TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS alerts_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                alert_type TEXT NOT NULL,
                price_at_alert REAL NOT NULL,
                level REAL NOT NULL,
                sent_at TEXT NOT NULL,
                position_id INTEGER REFERENCES positions(id)
            );
            CREATE INDEX IF NOT EXISTS idx_alerts_log_ticker ON alerts_log(ticker);
            CREATE INDEX IF NOT EXISTS idx_alerts_log_sent ON alerts_log(sent_at);
        """)
        for col, col_type in [
            ("stop_loss", "REAL"),
            ("target_price", "REAL"),
            ("stop_method", "TEXT"),
            ("unit_cost", "REAL"),
            ("notes", "TEXT"),
        ]:
            try:
                conn.execute(f"ALTER TABLE positions ADD COLUMN {col} {col_type}")
            except sqlite3.OperationalError:
                pass


def add_position(
    ticker: str,
    shares: float,
    entry_price: float,
    source: str = "manual",
    stop_loss: float | None = None,
    target_price: float | None = None,
    stop_method: str | None = None,
    unit_cost: float | None = None,
) -> int:
    init_db()
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO positions (ticker, shares, entry_price, entry_date, source, stop_loss, target_price, stop_method, unit_cost) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ticker.upper(), shares, entry_price, now, source, stop_loss, target_price, stop_method, unit_cost),
        )
        return cur.lastrowid


def close_position(ticker: str, exit_price: float | None = None) -> bool:
    init_db()
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE positions SET status='closed', exit_price=?, exit_date=? WHERE ticker=? AND status='open'",
            (exit_price, now, ticker.upper()),
        )
        return cur.rowcount > 0


def remove_position(ticker: str) -> bool:
    init_db()
    with _conn() as conn:
        cur = conn.execute(
            "DELETE FROM positions WHERE ticker=? AND status='open'",
            (ticker.upper(),),
        )
        return cur.rowcount > 0


def get_portfolio() -> list[dict]:
    init_db()
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM positions WHERE status='open' ORDER BY entry_date DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def get_all_positions(include_closed: bool = False) -> list[dict]:
    init_db()
    with _conn() as conn:
        if include_closed:
            rows = conn.execute("SELECT * FROM positions ORDER BY entry_date DESC").fetchall()
        else:
            rows = conn.execute("SELECT * FROM positions WHERE status='open' ORDER BY entry_date DESC").fetchall()
        return [dict(r) for r in rows]


def get_portfolio_history(days: int = 30) -> list[dict]:
    init_db()
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM portfolio_snapshots ORDER BY timestamp DESC LIMIT ?",
            (days * 48,),
        ).fetchall()
        return [dict(r) for r in reversed(rows)]


def record_price_snapshot(ticker: str, price: float, volume: int | None = None, rsi: float | None = None):
    init_db()
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        conn.execute(
            "INSERT INTO price_history (ticker, price, volume, rsi, timestamp) VALUES (?, ?, ?, ?, ?)",
            (ticker.upper(), price, volume, rsi, now),
        )


def record_portfolio_snapshot(total_value: float, total_cost: float):
    init_db()
    pnl = total_value - total_cost
    pnl_pct = (pnl / total_cost * 100) if total_cost > 0 else 0.0
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        conn.execute(
            "INSERT INTO portfolio_snapshots (total_value, total_cost, pnl, pnl_pct, timestamp) VALUES (?, ?, ?, ?, ?)",
            (total_value, total_cost, pnl, pnl_pct, now),
        )


def get_price_history(ticker: str, limit: int = 500) -> list[dict]:
    init_db()
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM price_history WHERE ticker=? ORDER BY timestamp DESC LIMIT ?",
            (ticker.upper(), limit),
        ).fetchall()
        return [dict(r) for r in reversed(rows)]


def delete_all_positions() -> int:
    init_db()
    with _conn() as conn:
        cur = conn.execute("DELETE FROM positions WHERE status='open'")
        return cur.rowcount


def get_portfolio_summary() -> dict:
    init_db()
    positions = get_portfolio()
    total_invested = sum(p["shares"] * p["entry_price"] for p in positions)
    tickers = [p["ticker"] for p in positions]
    return {
        "total_invested": round(total_invested, 2),
        "position_count": len(positions),
        "tickers": tickers,
        "positions": positions,
    }


def get_open_tickers() -> list[str]:
    init_db()
    with _conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT ticker FROM positions WHERE status='open'"
        ).fetchall()
        return [r["ticker"] for r in rows]


def update_position_levels(
    ticker: str,
    stop_loss: float | None = None,
    target_price: float | None = None,
    stop_method: str | None = None,
) -> bool:
    init_db()
    with _conn() as conn:
        parts, vals = [], []
        if stop_loss is not None:
            parts.append("stop_loss=?")
            vals.append(stop_loss)
        if target_price is not None:
            parts.append("target_price=?")
            vals.append(target_price)
        if stop_method is not None:
            parts.append("stop_method=?")
            vals.append(stop_method)
        if not parts:
            return False
        vals.append(ticker.upper())
        cur = conn.execute(
            f"UPDATE positions SET {', '.join(parts)} WHERE ticker=? AND status='open'",
            vals,
        )
        return cur.rowcount > 0


def get_positions_with_levels() -> list[dict]:
    init_db()
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM positions WHERE status='open' AND (stop_loss IS NOT NULL OR target_price IS NOT NULL) ORDER BY ticker"
        ).fetchall()
        return [dict(r) for r in rows]


def upsert_position(
    ticker: str,
    shares: float,
    entry_price: float,
    source: str = "wio",
    unit_cost: float | None = None,
    stop_loss: float | None = None,
    target_price: float | None = None,
    stop_method: str | None = None,
) -> int:
    init_db()
    with _conn() as conn:
        existing = conn.execute(
            "SELECT id FROM positions WHERE ticker=? AND status='open'",
            (ticker.upper(),),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE positions SET shares=?, entry_price=?, unit_cost=?, source=?, "
                "stop_loss=COALESCE(?, stop_loss), target_price=COALESCE(?, target_price), "
                "stop_method=COALESCE(?, stop_method) WHERE id=?",
                (shares, entry_price, unit_cost, source,
                 stop_loss, target_price, stop_method, existing["id"]),
            )
            return existing["id"]
        now = datetime.now(timezone.utc).isoformat()
        cur = conn.execute(
            "INSERT INTO positions (ticker, shares, entry_price, entry_date, source, unit_cost, stop_loss, target_price, stop_method) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ticker.upper(), shares, entry_price, now, source, unit_cost, stop_loss, target_price, stop_method),
        )
        return cur.lastrowid


def clear_all_positions():
    init_db()
    with _conn() as conn:
        conn.execute("DELETE FROM positions WHERE status='open'")


def record_alert(
    ticker: str,
    alert_type: str,
    price: float,
    level: float,
    position_id: int | None = None,
):
    init_db()
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        conn.execute(
            "INSERT INTO alerts_log (ticker, alert_type, price_at_alert, level, sent_at, position_id) VALUES (?, ?, ?, ?, ?, ?)",
            (ticker.upper(), alert_type, price, level, now, position_id),
        )


def was_alert_sent(ticker: str, alert_type: str, since_hours: int = 24) -> bool:
    init_db()
    cutoff = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM alerts_log WHERE ticker=? AND alert_type=? AND sent_at > datetime(?, '-' || ? || ' hours')",
            (ticker.upper(), alert_type, cutoff, since_hours),
        ).fetchone()
        return row["cnt"] > 0


def get_recent_alerts(limit: int = 50) -> list[dict]:
    init_db()
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM alerts_log ORDER BY sent_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
