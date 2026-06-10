import json
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import streamlit as st
import plotly.graph_objects as go
import pandas as pd

from core.finance import init_config, fetch_yahoo_data, _fmt, read_analysis_history
from core import database as db
from core import timeseries as ts

init_config()
db.init_db()
ts.init_influx()

st.set_page_config(page_title="Portfolio Dashboard", page_icon="📊", layout="wide")

# ---------------------------------------------------------------------------
# Telemetry collector (background thread)
# ---------------------------------------------------------------------------

_collector_started = False
_executor = ThreadPoolExecutor(max_workers=3)


def _is_market_hours() -> bool:
    now = datetime.now(timezone.utc)
    et_offset = -4
    et_hour = (now.hour + et_offset) % 24
    weekday = now.weekday()
    return weekday < 5 and 9 <= et_hour < 16


def _collect_prices():
    tickers = db.get_open_tickers()
    if not tickers:
        return

    total_value = 0.0
    total_cost = 0.0
    positions = db.get_portfolio()

    for ticker in tickers:
        try:
            data = fetch_yahoo_data(ticker)
            price = data.get("current_price")
            if price:
                ts.write_price_snapshot(
                    ticker, price,
                    volume=data.get("volume"),
                    rsi=data.get("rsi_14"),
                )
        except Exception:
            pass

    for pos in positions:
        cost = pos["shares"] * pos["entry_price"]
        total_cost += cost
        try:
            data = fetch_yahoo_data(pos["ticker"])
            price = data.get("current_price")
            total_value += pos["shares"] * price if price else cost
        except Exception:
            total_value += cost

    if total_cost > 0:
        ts.write_portfolio_snapshot(total_value, total_cost)


def _telemetry_loop():
    while True:
        try:
            if _is_market_hours():
                _collect_prices()
            else:
                time.sleep(60)
                continue
        except Exception:
            pass
        time.sleep(300)


def start_telemetry():
    global _collector_started
    if not _collector_started:
        _collector_started = True
        t = threading.Thread(target=_telemetry_loop, daemon=True)
        t.start()


start_telemetry()


# ---------------------------------------------------------------------------
# Dashboard UI
# ---------------------------------------------------------------------------

st.title("📊 Portfolio Dashboard")

positions = db.get_portfolio()

if not positions:
    st.info("Portfolio is empty. Add positions via Claude Code (`add_to_portfolio` tool) or Telegram bot (`/add` command).")
    st.stop()

# Fetch live data for all positions
enriched = []
total_cost = 0.0
total_value = 0.0

with st.spinner("Fetching live prices..."):
    for pos in positions:
        ticker = pos["ticker"]
        data = fetch_yahoo_data(ticker)
        price = data.get("current_price")
        cost = pos["shares"] * pos["entry_price"]
        value = pos["shares"] * price if price else cost
        pnl = value - cost
        pnl_pct = (pnl / cost * 100) if cost > 0 else 0
        total_cost += cost
        total_value += value
        enriched.append({
            "Ticker": ticker,
            "Shares": pos["shares"],
            "Entry": pos["entry_price"],
            "Current": price,
            "Cost": round(cost, 2),
            "Value": round(value, 2),
            "P&L ($)": round(pnl, 2),
            "P&L (%)": round(pnl_pct, 2),
            "Source": pos["source"],
            "Date": pos["entry_date"][:10],
            "rsi": data.get("rsi_14"),
            "sector": data.get("sector", ""),
        })

total_pnl = total_value - total_cost
total_pnl_pct = (total_pnl / total_cost * 100) if total_cost > 0 else 0

# Header metrics
col1, col2, col3, col4 = st.columns(4)
col1.metric("Portfolio Value", f"${total_value:,.2f}")
col2.metric("Total Cost", f"${total_cost:,.2f}")
col3.metric("P&L", f"${total_pnl:,.2f}", f"{total_pnl_pct:+.1f}%")
col4.metric("Positions", len(enriched))

st.divider()

# Portfolio table
st.subheader("Open Positions")
df = pd.DataFrame(enriched)
display_cols = ["Ticker", "Shares", "Entry", "Current", "Cost", "Value", "P&L ($)", "P&L (%)", "Source", "Date"]
st.dataframe(
    df[display_cols].style.map(
        lambda v: "color: green" if isinstance(v, (int, float)) and v > 0
        else ("color: red" if isinstance(v, (int, float)) and v < 0 else ""),
        subset=["P&L ($)", "P&L (%)"]
    ),
    use_container_width=True,
    hide_index=True,
)

st.divider()

# Allocation chart and P&L chart side by side
col_left, col_right = st.columns(2)

with col_left:
    st.subheader("Allocation")
    fig_alloc = go.Figure(data=[go.Pie(
        labels=[e["Ticker"] for e in enriched],
        values=[e["Value"] for e in enriched],
        hole=0.4,
        textinfo="label+percent",
    )])
    fig_alloc.update_layout(height=350, margin=dict(t=20, b=20, l=20, r=20))
    st.plotly_chart(fig_alloc, use_container_width=True)

with col_right:
    st.subheader("P&L by Position")
    colors = ["green" if e["P&L ($)"] >= 0 else "red" for e in enriched]
    fig_pnl = go.Figure(data=[go.Bar(
        x=[e["Ticker"] for e in enriched],
        y=[e["P&L ($)"] for e in enriched],
        marker_color=colors,
        text=[f"{e['P&L (%)']:+.1f}%" for e in enriched],
        textposition="outside",
    )])
    fig_pnl.update_layout(
        height=350,
        margin=dict(t=20, b=20, l=20, r=20),
        yaxis_title="P&L ($)",
    )
    st.plotly_chart(fig_pnl, use_container_width=True)

st.divider()

# Portfolio value over time
snapshots = ts.query_portfolio_history(days=30)
if not snapshots:
    snapshots = db.get_portfolio_history(days=30)
if snapshots:
    st.subheader("Portfolio Value Over Time")
    snap_df = pd.DataFrame(snapshots)
    snap_df["timestamp"] = pd.to_datetime(snap_df["timestamp"])
    fig_hist = go.Figure()
    fig_hist.add_trace(go.Scatter(
        x=snap_df["timestamp"], y=snap_df["total_value"],
        name="Value", line=dict(color="blue", width=2),
    ))
    fig_hist.add_trace(go.Scatter(
        x=snap_df["timestamp"], y=snap_df["total_cost"],
        name="Cost Basis", line=dict(color="gray", width=1, dash="dash"),
    ))
    fig_hist.update_layout(
        height=350,
        margin=dict(t=20, b=20, l=20, r=20),
        yaxis_title="USD",
        legend=dict(orientation="h", yanchor="bottom", y=1.02),
    )
    st.plotly_chart(fig_hist, use_container_width=True)

st.divider()

# Individual stock detail
st.subheader("Stock Detail")
selected = st.selectbox("Select ticker", [e["Ticker"] for e in enriched])

if selected:
    col_chart, col_tech = st.columns([2, 1])

    with col_chart:
        entry_date = next((e["Date"] for e in enriched if e["Ticker"] == selected), None)
        start_ts = entry_date if entry_date else None
        price_hist = ts.query_price_history(selected, start=start_ts)
        if not price_hist:
            price_hist = db.get_price_history(selected, limit=500)
        if price_hist:
            ph_df = pd.DataFrame(price_hist)
            ph_df["timestamp"] = pd.to_datetime(ph_df["timestamp"])
            fig_stock = go.Figure()
            fig_stock.add_trace(go.Scatter(
                x=ph_df["timestamp"], y=ph_df["price"],
                name="Price", line=dict(color="blue", width=2),
            ))
            entry = next((e["Entry"] for e in enriched if e["Ticker"] == selected), None)
            if entry:
                fig_stock.add_hline(y=entry, line_dash="dash", line_color="orange",
                                    annotation_text=f"Entry: ${entry:.2f}")
            fig_stock.update_layout(
                title=f"{selected} Price History",
                height=350,
                margin=dict(t=40, b=20, l=20, r=20),
                yaxis_title="USD",
            )
            st.plotly_chart(fig_stock, use_container_width=True)
        else:
            st.info(f"No price history for {selected} yet. Data collects every 5 min during market hours.")

    with col_tech:
        pos_data = next((e for e in enriched if e["Ticker"] == selected), None)
        if pos_data:
            st.markdown(f"**{selected}**")
            st.markdown(f"Current: **${pos_data['Current']:.2f}**" if pos_data["Current"] else "Current: N/A")
            st.markdown(f"Entry: ${pos_data['Entry']:.2f}")
            st.markdown(f"Shares: {pos_data['Shares']}")
            st.markdown(f"P&L: **${pos_data['P&L ($)']:.2f}** ({pos_data['P&L (%)']:+.1f}%)")
            st.markdown(f"Sector: {pos_data.get('sector', 'N/A')}")
            rsi = pos_data.get("rsi")
            if rsi:
                rsi_color = "red" if rsi > 70 else ("green" if rsi < 30 else "gray")
                rsi_label = "Overbought" if rsi > 70 else ("Oversold" if rsi < 30 else "Neutral")
                st.markdown(f"RSI(14): **:{rsi_color}[{rsi:.1f}]** ({rsi_label})")

st.divider()

# Recent alerts from channel monitor
st.subheader("Recent Channel Alerts")
alerts = read_analysis_history(alerts_only=True, limit=5)
if alerts:
    for alert in alerts:
        analysis = alert.get("analysis", {})
        ticker = alert.get("ticker", "???")
        verdict = analysis.get("verdict", "???")
        confidence = analysis.get("confidence", "?")
        icon = {"BUY": "✅", "SELL": "🔴", "HOLD": "⏸"}.get(verdict, "❓")
        ts = alert.get("timestamp", "")[:16]
        channel = alert.get("channel", "")
        st.markdown(
            f"{icon} **{ticker}** — {verdict} ({confidence}) | "
            f"{analysis.get('summary', 'N/A')[:100]} | "
            f"_{channel}_ {ts}"
        )
else:
    st.info("No alerts yet. Alerts appear when the channel monitor detects BUY/SELL recommendations.")

# Auto-refresh
st.markdown("---")
st.caption(f"Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | Telemetry: every 5 min during market hours")
try:
    from streamlit_autorefresh import st_autorefresh
    st_autorefresh(interval=60_000, key="dashboard_refresh")
except ImportError:
    pass
