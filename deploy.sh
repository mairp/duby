#!/usr/bin/env bash
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT="${PROJECT:-$DIR}"
UV="uv run --project $PROJECT"
PIDFILE="$DIR/data/deploy.pids"

usage() {
    echo "Usage: $0 {start|stop|restart|status}"
    echo ""
    echo "  start    — Start all services (Docker + Python + Frontend)"
    echo "  stop     — Stop everything"
    echo "  restart  — Stop then start"
    echo "  status   — Show what's running"
    exit 1
}

start_services() {
    echo "========================================"
    echo "  Share Analysis Agent — Starting"
    echo "========================================"
    echo ""

    cd "$DIR"
    mkdir -p "$DIR/data"
    rm -f "$PIDFILE"

    # 1. Docker (LiteLLM + InfluxDB)
    echo "[deploy] Starting Docker services..."
    docker compose -f "$DIR/docker-compose.yml" up -d
    echo ""

    # 2. Channel monitor
    echo "[deploy] Starting channel monitor..."
    $UV python -m agents.channel_monitor > /tmp/channel_monitor.log 2>&1 &
    echo "$!" >> "$PIDFILE"
    echo "[deploy] Channel monitor PID: $!"

    # 3. Telegram bot
    echo "[deploy] Starting Telegram bot..."
    $UV python -m agents.telegram_bot > /tmp/telegram_bot.log 2>&1 &
    echo "$!" >> "$PIDFILE"
    echo "[deploy] Telegram bot PID: $!"

    # 4. Price monitor
    echo "[deploy] Starting price monitor..."
    $UV python -m agents.price_monitor > /tmp/price_monitor.log 2>&1 &
    echo "$!" >> "$PIDFILE"
    echo "[deploy] Price monitor PID: $!"

    # 5. FastAPI backend
    echo "[deploy] Starting API server..."
    $UV uvicorn api.main:app --host 0.0.0.0 --port 8000 > /tmp/api_server.log 2>&1 &
    echo "$!" >> "$PIDFILE"
    echo "[deploy] API server PID: $!"

    # 6. React frontend
    echo "[deploy] Starting frontend..."
    cd "$DIR/frontend" && npm run dev > /tmp/frontend.log 2>&1 &
    echo "$!" >> "$PIDFILE"
    echo "[deploy] Frontend PID: $!"
    cd "$DIR"

    echo ""
    echo "========================================"
    echo "  All services started"
    echo "  API:      http://localhost:8000"
    echo "  Frontend: http://localhost:5173"
    echo "  LiteLLM:  http://localhost:4000"
    echo "  InfluxDB: http://localhost:8086"
    echo "  Logs:     /tmp/{channel_monitor,telegram_bot,price_monitor,api_server,frontend}.log"
    echo "  Stop:     $0 stop"
    echo "========================================"
}

stop_services() {
    echo "[deploy] Stopping services..."

    # Kill tracked PIDs
    if [ -f "$PIDFILE" ]; then
        while read -r pid; do
            if kill -0 "$pid" 2>/dev/null; then
                kill "$pid" 2>/dev/null && echo "[deploy] Stopped PID $pid"
            fi
        done < "$PIDFILE"
        rm -f "$PIDFILE"
    fi

    # Kill any remaining service processes
    pkill -f "python -m agents.channel_monitor" 2>/dev/null && echo "[deploy] Stopped channel monitor" || true
    pkill -f "python -m agents.telegram_bot" 2>/dev/null && echo "[deploy] Stopped telegram bot" || true
    pkill -f "python -m agents.price_monitor" 2>/dev/null && echo "[deploy] Stopped price monitor" || true
    pkill -f "uvicorn api.main:app" 2>/dev/null && echo "[deploy] Stopped API server" || true
    pkill -f "vite.*--port" 2>/dev/null && echo "[deploy] Stopped frontend" || true

    sleep 1

    # Stop Docker
    echo "[deploy] Stopping Docker services..."
    docker compose -f "$DIR/docker-compose.yml" down 2>/dev/null

    # Clean up PID files
    rm -f "$DIR/data/telegram_bot.pid" "$DIR/data/price_monitor.pid" "$PIDFILE"

    echo "[deploy] All services stopped."
}

show_status() {
    echo "=== Docker ==="
    docker compose -f "$DIR/docker-compose.yml" ps 2>/dev/null || echo "  (not running)"
    echo ""
    echo "=== Python services ==="
    ps aux | grep -E "agents\.(channel_monitor|telegram_bot|price_monitor)|uvicorn api" | grep -v grep || echo "  (none running)"
    echo ""
    echo "=== Frontend ==="
    ps aux | grep -E "vite" | grep -v grep || echo "  (not running)"
}

case "${1:-}" in
    start)   start_services ;;
    stop)    stop_services ;;
    restart) stop_services; echo ""; start_services ;;
    status)  show_status ;;
    *)       usage ;;
esac
