"""FastAPI backend for the portfolio monitoring system."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from contextlib import asynccontextmanager
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

load_dotenv(Path(__file__).parent.parent / ".env")

import logging

from core.finance import init_config
from core.database import init_db, get_portfolio
from core.timeseries import init_influx
from core.portfolio_yaml import load_portfolio, DEFAULT_PATH

log = logging.getLogger(__name__)

from api.routes import portfolio, market, alerts, invest


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_config()
    init_db()
    init_influx()
    if DEFAULT_PATH.exists() and not get_portfolio():
        log.info("DB empty, loading portfolio from %s", DEFAULT_PATH)
        result = load_portfolio(calc_stops=True)
        log.info("Loaded: %s", result)
    yield


app = FastAPI(title="Share Analysis API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(portfolio.router, prefix="/api")
app.include_router(market.router, prefix="/api")
app.include_router(alerts.router, prefix="/api")
app.include_router(invest.router, prefix="/api")


@app.get("/api/health")
def health():
    return {"status": "ok"}
