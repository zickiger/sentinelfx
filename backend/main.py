"""
╔══════════════════════════════════════════════════════════════╗
║         SENTINEL FX — FastAPI Backend  v1.0                  ║
║   Serves real data to the portal from all agent subsystems   ║
╚══════════════════════════════════════════════════════════════╝

Install:
    pip install fastapi uvicorn[standard] aiohttp yfinance \
                sqlite-utils pydantic python-dotenv websockets

Run:
    uvicorn main:app --host 0.0.0.0 --port 8000 --reload

Portal connects to:
    http://localhost:8000
    ws://localhost:8000/ws/live
"""

import asyncio
import json
import sqlite3
import os
import sys
import subprocess
import time
import random
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import yfinance as yf
import aiohttp
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────────────────────────
# APP INIT
# ─────────────────────────────────────────────────────────────

app = FastAPI(
    title="SentinelFX API",
    description="Multi-Agent Trading System Backend",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[os.environ.get("ALLOWED_ORIGIN", "*")],          # tighten in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─────────────────────────────────────────────────────────────
# DATABASE HELPERS
# ─────────────────────────────────────────────────────────────

DB_PATH        = os.environ.get("SENTINEL_DB",   "../architect/data/setups.db")
ARCHITECT_DIR  = Path(os.environ.get("ARCHITECT_DIR", "../architect"))
EXECUTOR_URL   = os.environ.get("EXECUTOR_URL", "http://localhost:8080")

def get_db():
    """Get a connection to the sentinel SQLite database."""
    if not Path(DB_PATH).exists():
        raise HTTPException(
            status_code=503,
            detail=f"Database not found at {DB_PATH}. Run research_sentinel.py first."
        )
    return sqlite3.connect(DB_PATH, check_same_thread=False)

def query(sql: str, params: tuple = (), db_path: str = DB_PATH):
    """Execute a read query and return list of dicts."""
    try:
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        cur = conn.execute(sql, params)
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows
    except Exception as e:
        return []

# ─────────────────────────────────────────────────────────────
# MARKET DATA (via yfinance)
# ─────────────────────────────────────────────────────────────

# Cache to avoid hammering yfinance on every request
_ticker_cache: dict = {}
_ticker_cache_time: float = 0
CACHE_TTL = 15  # seconds

WATCHLIST = {
    "ES=F":   "ES1!",
    "NQ=F":   "NQ1!",
    "CL=F":   "CL1!",
    "GC=F":   "GC1!",
    "EURUSD=X": "EUR/USD",
    "GBPUSD=X": "GBP/USD",
    "JPY=X":  "USD/JPY",
    "BTC-USD": "BTC/USD",
    "^VIX":   "VIX",
    "DX-Y.NYB": "DXY",
}

async def fetch_market_data() -> list[dict]:
    """Fetch live quotes with accurate daily change % using 2-day history."""
    global _ticker_cache, _ticker_cache_time

    if time.time() - _ticker_cache_time < CACHE_TTL and _ticker_cache:
        return list(_ticker_cache.values())

    results = []
    for symbol, display in WATCHLIST.items():
        try:
            ticker = yf.Ticker(symbol)
            # 2-day daily history gives accurate prev_close vs current close
            hist = ticker.history(period="2d", interval="1d")
            if len(hist) >= 2:
                prev_close = float(hist["Close"].iloc[-2])
                price      = float(hist["Close"].iloc[-1])
            elif len(hist) == 1:
                prev_close = float(hist["Open"].iloc[-1])
                price      = float(hist["Close"].iloc[-1])
            else:
                info       = ticker.fast_info
                price      = getattr(info, "last_price", 0) or 0
                prev_close = getattr(info, "previous_close", price) or price
            chg     = price - prev_close
            chg_pct = (chg / prev_close * 100) if prev_close else 0
            results.append({
                "symbol":     display,
                "price":      round(price, 4),
                "change":     round(chg, 4),
                "change_pct": round(chg_pct, 2),
                "up":         chg >= 0,
            })
        except Exception:
            if display in _ticker_cache:
                results.append(_ticker_cache[display])

    if results:
        _ticker_cache = {r["symbol"]: r for r in results}
        _ticker_cache_time = time.time()
    return results if results else list(_ticker_cache.values())


async def fetch_equity_curve(days: int = 30) -> list[dict]:
    """Fetch ES futures OHLCV as a proxy equity curve."""
    try:
        ticker = yf.Ticker("ES=F")
        hist = ticker.history(period=f"{days}d", interval="1d")
        result = []
        for date, row in hist.iterrows():
            result.append({
                "date":  date.strftime("%Y-%m-%d"),
                "close": round(float(row["Close"]), 2),
                "volume": int(row["Volume"]),
            })
        return result
    except Exception:
        return []

# ─────────────────────────────────────────────────────────────
# AGENT STATE
# ─────────────────────────────────────────────────────────────

class AgentStateStore:
    """In-memory store for live agent state, updated by agent processes."""

    def __init__(self):
        self._state = {
            "sentinel": {
                "status": "running",
                "task": "Idle — awaiting next cycle",
                "progress": 0,
                "last_run": None,
                "papers_scraped": 0,
                "signals_extracted": 0,
                "current_batch": 0,
            },
            "architect": {
                "status": "idle",
                "task": "No active build",
                "progress": 0,
                "current_file": None,
                "builds_complete": 0,
                "queue_depth": 0,
            },
            "validator": {
                "status": "idle",
                "task": "No active validation",
                "progress": 0,
                "current_strategy": None,
                "mc_iterations": 0,
                "mc_total": 10000,
                "last_sharpe": None,
                "last_calmar": None,
                "last_pruin": None,
            },
            "executor": {
                "status": "standby",
                "task": "Webhook listener active",
                "latency_ms": 0,
                "orders_today": 0,
                "pnl_today": 0.0,
                "last_alert_time": None,
                "connected_broker": os.environ.get("BROKER_NAME", "Topstep SIM"),
            },
        }
        self._log: list[dict] = []
        self._lock = asyncio.Lock()

    def get_all(self) -> dict:
        return self._state.copy()

    def get_agent(self, name: str) -> dict:
        return self._state.get(name, {})

    def update_agent(self, name: str, updates: dict):
        if name in self._state:
            self._state[name].update(updates)

    def add_log(self, agent: str, message: str):
        self._log.insert(0, {
            "time": datetime.utcnow().strftime("%H:%M:%S"),
            "agent": agent,
            "message": message,
        })
        self._log = self._log[:100]  # keep last 100

    def get_log(self, limit: int = 20) -> list[dict]:
        return self._log[:limit]


agent_store = AgentStateStore()

# Seed some log entries on startup
startup_logs = [
    ("system",    "SentinelFX backend started — all agents initializing"),
    ("sentinel",  "Research Sentinel ready — awaiting cycle trigger"),
    ("executor",  "Webhook bridge listening on :8080"),
    ("validator", "Validation Engine loaded — Monte Carlo ready"),
    ("architect", "Architect Agent standing by — module library loaded"),
]
for a, m in startup_logs:
    agent_store.add_log(a, m)

# ─────────────────────────────────────────────────────────────
# WEBSOCKET MANAGER
# ─────────────────────────────────────────────────────────────

class ConnectionManager:
    def __init__(self):
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket):
        self.active = [c for c in self.active if c != ws]

    async def broadcast(self, data: dict):
        dead = []
        for ws in self.active:
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


manager = ConnectionManager()

# ─────────────────────────────────────────────────────────────
# BACKGROUND TASKS
# ─────────────────────────────────────────────────────────────

async def broadcast_loop():
    """Push live updates to all connected WebSocket clients every 2s."""
    while True:
        try:
            market = await fetch_market_data()
            payload = {
                "type":    "live_update",
                "ts":      datetime.utcnow().isoformat(),
                "market":  market,
                "agents":  agent_store.get_all(),
                "log":     agent_store.get_log(10),
            }
            await manager.broadcast(payload)
        except Exception:
            pass
        await asyncio.sleep(2)


async def simulate_agent_activity():
    """
    Simulates realistic agent state changes.
    Replace with real subprocess/IPC calls when agents are running live.
    """
    while True:
        # Sentinel: cycle every ~60s in simulation
        await asyncio.sleep(random.uniform(8, 14))

        # Sentinel progresses
        s = agent_store.get_agent("sentinel")
        progress = (s.get("progress", 0) + random.uniform(5, 15)) % 100
        agent_store.update_agent("sentinel", {
            "progress": round(progress, 1),
            "status": "running" if progress < 95 else "idle",
            "task": random.choice([
                "Scraping ArXiv q-fin.TR batch...",
                "Parsing SSRN working papers...",
                "NLP extraction: scoring signals...",
                "Filtering Reddit r/SMCtrading posts...",
                "Updating alpha queue database...",
            ]),
            "papers_scraped": s.get("papers_scraped", 0) + random.randint(0, 3),
        })

        if random.random() > 0.6:
            msg = random.choice([
                "New FVG signal extracted — confidence 0.81",
                "Order block confluence detected in ArXiv paper",
                "Liquidity sweep pattern indexed from Reddit",
                "High-alpha signal queued for Architect Agent",
                "Scrape cycle complete — 4 new signals",
            ])
            agent_store.add_log("sentinel", msg)

        # Architect: intermittent builds
        a = agent_store.get_agent("architect")
        arch_prog = a.get("progress", 0)
        if arch_prog > 0:
            arch_prog = min(100, arch_prog + random.uniform(3, 10))
            agent_store.update_agent("architect", {
                "progress": round(arch_prog, 1),
                "status": "building" if arch_prog < 100 else "idle",
            })
            if arch_prog >= 100:
                fname = a.get("current_file", "strategy.pine")
                agent_store.add_log("architect", f"Build complete: {fname}")
                agent_store.update_agent("architect", {
                    "progress": 0,
                    "current_file": None,
                    "builds_complete": a.get("builds_complete", 0) + 1,
                })

        # Validator: MC progress
        v = agent_store.get_agent("validator")
        mc_iter = v.get("mc_iterations", 0)
        mc_total = v.get("mc_total", 10000)
        if mc_iter > 0 and mc_iter < mc_total:
            new_iter = min(mc_total, mc_iter + random.randint(200, 600))
            agent_store.update_agent("validator", {
                "mc_iterations": new_iter,
                "progress": round(new_iter / mc_total * 100, 1),
                "status": "testing",
            })
            if new_iter >= mc_total:
                sharpe = round(random.uniform(1.6, 2.8), 2)
                calmar = round(random.uniform(2.0, 3.5), 2)
                pruin  = round(random.uniform(0.8, 2.5), 1)
                passed = sharpe >= 1.8 and calmar >= 2.0 and pruin <= 2.0
                agent_store.update_agent("validator", {
                    "mc_iterations": 0, "progress": 0,
                    "status": "idle",
                    "last_sharpe": sharpe,
                    "last_calmar": calmar,
                    "last_pruin":  pruin,
                })
                result = "PASSED" if passed else "FAILED"
                strat = v.get("current_strategy", "Unknown")
                agent_store.add_log(
                    "validator",
                    f"{strat} MC {result} — Sharpe {sharpe}, Calmar {calmar}, P(ruin) {pruin}%"
                )

        # Executor: latency ping
        agent_store.update_agent("executor", {
            "latency_ms": random.randint(2, 9),
        })


@app.on_event("startup")
async def startup_event():
    asyncio.create_task(broadcast_loop())
    asyncio.create_task(simulate_agent_activity())


# ─────────────────────────────────────────────────────────────
# PYDANTIC MODELS
# ─────────────────────────────────────────────────────────────

class RunCycleRequest(BaseModel):
    source: Optional[str] = None   # "arxiv" | "ssrn" | "reddit" | None = all

class DeployRequest(BaseModel):
    strategy_ids: list[str]
    broker: str = "topstep_sim"

class RiskConfigRequest(BaseModel):
    max_contracts: int = 3
    risk_per_trade_pct: float = 0.5
    max_open_positions: int = 2
    daily_loss_limit: float = 1000.0
    kill_switch_armed: bool = True
    anti_detection: bool = True

class WebhookAlertRequest(BaseModel):
    """Incoming TradingView alert payload"""
    strategy: str
    action: str           # "buy" | "sell" | "close"
    symbol: str
    contracts: int = 1
    price: Optional[float] = None
    sl: Optional[float] = None
    tp: Optional[float] = None
    secret: str = ""      # webhook secret for auth


# ─────────────────────────────────────────────────────────────
# ROUTES — SYSTEM
# ─────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return {"service": "SentinelFX API", "version": "1.0.0", "status": "online"}


@app.get("/api/health")
async def health():
    db_ok = Path(DB_PATH).exists()
    return {
        "status":    "healthy",
        "db":        "connected" if db_ok else "missing — run setup_hunter.py --scan first",
        "agents":    len(agent_store.get_all()),
        "ws_clients": len(manager.active),
        "ts":        datetime.utcnow().isoformat(),
    }


# ─────────────────────────────────────────────────────────────
# ROUTES — MARKET DATA
# ─────────────────────────────────────────────────────────────

@app.get("/api/market/quotes")
async def market_quotes():
    """Live quotes for the watchlist."""
    data = await fetch_market_data()
    return {"quotes": data, "ts": datetime.utcnow().isoformat()}


@app.get("/api/market/equity-curve")
async def equity_curve(days: int = 30):
    """ES futures daily closes as reference equity curve."""
    data = await fetch_equity_curve(days)
    return {"curve": data, "symbol": "ES=F", "days": days}


@app.get("/api/market/regime")
async def market_regime():
    """
    Simple regime detection using VIX + trend.
    Replace with your own regime model.
    """
    try:
        vix = yf.Ticker("^VIX")
        vix_price = vix.fast_info.last_price or 15.0

        es = yf.Ticker("ES=F")
        hist = es.history(period="20d", interval="1d")
        if len(hist) >= 10:
            ma10 = hist["Close"].rolling(10).mean().iloc[-1]
            last = hist["Close"].iloc[-1]
            trend = "up" if last > ma10 else "down"
        else:
            trend = "unknown"

        if vix_price > 30:
            regime = "HIGH VOLATILITY"
        elif vix_price > 20 and trend == "down":
            regime = "BEAR REGIME"
        elif vix_price < 15 and trend == "up":
            regime = "BULL REGIME"
        elif vix_price < 15:
            regime = "LOW VOLATILITY"
        else:
            regime = "RANGE BOUND"

        return {"regime": regime, "vix": round(vix_price, 2), "trend": trend}
    except Exception:
        return {"regime": "UNKNOWN", "vix": None, "trend": "unknown"}


# ─────────────────────────────────────────────────────────────
# ROUTES — AGENTS
# ─────────────────────────────────────────────────────────────

@app.get("/api/agents")
async def get_agents():
    """Current state of all 4 agents."""
    return {"agents": agent_store.get_all()}


@app.get("/api/agents/{agent_name}")
async def get_agent(agent_name: str):
    state = agent_store.get_agent(agent_name)
    if not state:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_name}' not found")
    return {"agent": agent_name, "state": state}


@app.post("/api/agents/sentinel/run")
async def run_sentinel(req: RunCycleRequest):
    """Trigger a Research Sentinel scraping cycle."""
    agent_store.update_agent("sentinel", {
        "status":   "running",
        "progress": 0,
        "task":     f"Starting cycle — source: {req.source or 'all'}",
        "last_run": datetime.utcnow().isoformat(),
    })
    agent_store.add_log("sentinel", f"Cycle triggered via API — source: {req.source or 'all'}")

    setup_hunter = ARCHITECT_DIR / "setup_hunter.py"

    if setup_hunter.exists():
        async def _real_cycle():
            cmd = [sys.executable, str(setup_hunter), "--scan"]
            if req.source:
                cmd += ["--source", req.source]
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    cwd=str(ARCHITECT_DIR),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                progress = 5
                async for line in proc.stdout:
                    text = line.decode(errors="replace").strip()
                    if text:
                        agent_store.add_log("sentinel", text[:120])
                        progress = min(progress + 3, 95)
                        agent_store.update_agent("sentinel", {"progress": progress, "task": text[:80]})
                await proc.wait()
                s = agent_store.get_agent("sentinel")
                agent_store.update_agent("sentinel", {
                    "status":            "idle",
                    "progress":          100,
                    "task":              "Scan complete — DB updated",
                    "signals_extracted": s.get("signals_extracted", 0) + 1,
                })
                agent_store.add_log("sentinel", "Real scan complete — new signals in database")
            except Exception as e:
                agent_store.update_agent("sentinel", {"status": "error", "task": f"Error: {e}"})
                agent_store.add_log("sentinel", f"Scan error: {e}")

        asyncio.create_task(_real_cycle())
    else:
        # setup_hunter.py not found — simulate for demo
        async def _sim_cycle():
            steps = [
                (10, "Connecting to sources..."),
                (30, "Parsing papers and posts..."),
                (55, "Running NLP extraction..."),
                (70, "Scoring with Ollama critic..."),
                (85, "Filtering alpha signals..."),
                (95, "Writing to database..."),
                (100, "Cycle complete"),
            ]
            for pct, task in steps:
                await asyncio.sleep(1.5)
                agent_store.update_agent("sentinel", {
                    "progress": pct, "task": task,
                    "last_run": datetime.utcnow().isoformat(),
                })
                agent_store.add_log("sentinel", task)
            s = agent_store.get_agent("sentinel")
            agent_store.update_agent("sentinel", {
                "status":            "idle",
                "signals_extracted": s.get("signals_extracted", 0) + random.randint(3, 8),
            })
            agent_store.add_log("sentinel", "Cycle complete — new signals queued for Architect")

        asyncio.create_task(_sim_cycle())

    return {"status": "started", "source": req.source or "all", "real": setup_hunter.exists()}


@app.post("/api/agents/architect/build")
async def trigger_build(signal_id: str):
    """Start an Architect Agent build for a given signal."""
    agent_store.update_agent("architect", {
        "status":       "building",
        "progress":     0,
        "current_file": f"strategy_{signal_id[:8]}.pine",
        "task":         f"Building Pine Script for signal {signal_id[:8]}...",
        "queue_depth":  agent_store.get_agent("architect").get("queue_depth", 0) + 1,
    })
    agent_store.add_log("architect", f"Build started — strategy_{signal_id[:8]}.pine")
    return {"status": "building", "file": f"strategy_{signal_id[:8]}.pine"}


@app.post("/api/agents/validator/run")
async def run_validator(strategy_name: str, iterations: int = 10000):
    """Start Monte Carlo + walk-forward validation."""
    agent_store.update_agent("validator", {
        "status":            "testing",
        "progress":          0,
        "mc_iterations":     0,
        "mc_total":          iterations,
        "current_strategy":  strategy_name,
        "task":              f"Starting MC simulation — {iterations:,} iterations",
    })
    agent_store.add_log("validator", f"Validation started: {strategy_name} ({iterations:,} MC)")
    return {"status": "started", "strategy": strategy_name, "iterations": iterations}


# ─────────────────────────────────────────────────────────────
# ROUTES — SIGNALS (from DB)
# ─────────────────────────────────────────────────────────────

@app.get("/api/signals")
async def get_signals(
    min_score: float = 0.0,
    concept_type: Optional[str] = None,
    limit: int = 50,
):
    """Fetch scored setups from setups.db (setup_hunter output)."""
    sql = """
        SELECT r.setup_id, r.source, r.title, r.url, r.author, r.engagement,
               v.confidence, v.verdict, v.concept_type, v.timeframe, v.market,
               v.entry_logic, v.sl_logic, v.tp_logic,
               v.strengths, v.weaknesses, v.red_flags,
               v.pine_viable, v.macro_impact, v.critic_reasoning,
               v.pine_built, v.backtest_run, v.backtest_sharpe, v.backtest_winrate,
               v.scored_at
        FROM raw_setups r
        JOIN critic_verdicts v ON r.setup_id = v.setup_id
        WHERE v.confidence >= ?
    """
    params: list = [min_score]

    if concept_type:
        sql += " AND v.concept_type = ?"
        params.append(concept_type.upper())

    sql += " ORDER BY v.confidence DESC LIMIT ?"
    params.append(limit)

    rows = query(sql, tuple(params))
    return {"signals": rows or [], "count": len(rows)}


@app.get("/api/signals/stats")
async def signal_stats():
    """Aggregated stats by concept type from setups.db."""
    rows = query("""
        SELECT v.concept_type,
               COUNT(*)                        AS count,
               ROUND(AVG(v.confidence), 1)     AS avg_score,
               MAX(v.confidence)               AS best_score,
               SUM(v.pine_built)               AS pine_ready,
               SUM(v.backtest_run)             AS backtested,
               ROUND(AVG(v.backtest_sharpe),2) AS avg_sharpe
        FROM critic_verdicts v
        GROUP BY v.concept_type
        ORDER BY avg_score DESC
    """)
    return {"stats": rows or []}


@app.get("/api/signals/passing")
async def get_passing_signals():
    """Signals that passed the critic threshold (65+) with Pine scripts built."""
    rows = query("""
        SELECT r.setup_id, r.source, r.title, r.url, r.author,
               v.confidence, v.concept_type, v.timeframe, v.market,
               v.entry_logic, v.sl_logic, v.tp_logic, v.macro_impact,
               v.pine_built, v.backtest_sharpe, v.backtest_winrate,
               v.backtest_result, v.scored_at
        FROM raw_setups r
        JOIN critic_verdicts v ON r.setup_id = v.setup_id
        WHERE v.confidence >= 65
        ORDER BY v.confidence DESC
    """)
    return {"signals": rows or [], "count": len(rows)}


# ─────────────────────────────────────────────────────────────
# ROUTES — STRATEGIES
# ─────────────────────────────────────────────────────────────

PINE_DIR = Path(os.environ.get("PINE_DIR", str(ARCHITECT_DIR / "pine_scripts")))

def _strategies_from_db() -> list[dict]:
    """Read built strategies from setups.db + scan pine_scripts folder."""
    rows = query("""
        SELECT r.setup_id AS id, r.title AS name, r.source, r.author, r.url,
               v.concept_type AS type, v.market, v.timeframe AS tf,
               v.confidence, v.backtest_sharpe AS sharpe,
               v.backtest_winrate AS winrate, v.backtest_result,
               v.pine_built, v.scored_at,
               CASE
                 WHEN v.backtest_sharpe >= 1.5 AND v.pine_built = 1 THEN "live"
                 WHEN v.pine_built = 1 THEN "testing"
                 ELSE "pending"
               END AS status
        FROM raw_setups r
        JOIN critic_verdicts v ON r.setup_id = v.setup_id
        WHERE v.confidence >= 65
        ORDER BY v.confidence DESC
    """)
    # Enrich with pine filename if it exists
    for row in rows:
        sid = row["id"][:8]
        ctype = (row.get("type") or "unknown").lower()
        conf  = row.get("confidence", 0)
        pattern = f"{ctype}_{conf}_{sid}.pine"
        pine_path = PINE_DIR / pattern
        # fallback: search for any matching file
        if not pine_path.exists() and PINE_DIR.exists():
            matches = list(PINE_DIR.glob(f"*{sid}*.pine"))
            if matches:
                pine_path = matches[0]
        row["pine_file"] = pine_path.name if pine_path.exists() else None
        # Parse backtest result
        try:
            bt = json.loads(row.get("backtest_result") or "{}")
            row["calmar"]   = round(bt.get("total_return", 0) / max(bt.get("max_dd_pct", 1), 0.01), 2)
            row["pruin"]    = bt.get("max_dd_pct", 0)
            row["pnl"]      = round(bt.get("total_return", 0), 2)
        except Exception:
            row["calmar"] = 0
            row["pruin"]  = 0
            row["pnl"]    = 0
    return rows


@app.get("/api/strategies")
async def get_strategies(status: Optional[str] = None):
    rows = _strategies_from_db()
    if status:
        rows = [s for s in rows if s.get("status") == status]
    total_pnl = sum(s.get("pnl", 0) for s in rows)
    return {"strategies": rows, "count": len(rows), "total_pnl": round(total_pnl, 2)}


@app.get("/api/strategies/{strategy_id}")
async def get_strategy(strategy_id: str):
    rows = _strategies_from_db()
    s = next((s for s in rows if s["id"] == strategy_id), None)
    if not s:
        raise HTTPException(status_code=404, detail="Strategy not found")
    return s


@app.post("/api/strategies/deploy")
async def deploy_strategies(req: DeployRequest):
    deployed = []
    for sid in req.strategy_ids:
        rows = _strategies_from_db()
        s = next((s for s in rows if s["id"] == sid), None)
        if s:
            deployed.append(s.get("name", sid))
            agent_store.add_log("executor", f"Strategy queued: {s.get('name','?')} -> {req.broker}")
    return {"deployed": deployed, "broker": req.broker, "count": len(deployed)}


# ─────────────────────────────────────────────────────────────
# ROUTES — RISK
# ─────────────────────────────────────────────────────────────

_risk_config = {
    "max_contracts":      3,
    "risk_per_trade_pct": 0.5,
    "max_open_positions": 2,
    "daily_loss_limit":   1000.0,
    "kill_switch_armed":  True,
    "anti_detection":     True,
    "dd_used_pct":        3.2,
    "dd_limit_pct":       4.5,
    "prop_firm":          os.environ.get("PROP_FIRM", "Topstep SIM"),
    "consistency_rule_pct": 30,
}

@app.get("/api/risk/config")
async def get_risk_config():
    return _risk_config

@app.post("/api/risk/config")
async def update_risk_config(req: RiskConfigRequest):
    _risk_config.update(req.dict())
    agent_store.add_log("system", f"Risk config updated — DD limit {_risk_config['dd_limit_pct']}%, kill_switch={'ON' if req.kill_switch_armed else 'OFF'}")
    # Push risk params to executor bridge so both stay in sync
    asyncio.create_task(_executor_post("/risk/config", {
        "dd_limit_pct":      _risk_config["dd_limit_pct"],
        "max_contracts":     _risk_config["max_contracts"],
        "kill_switch_armed": _risk_config["kill_switch_armed"],
    }))
    return {"status": "saved", "config": _risk_config}

@app.get("/api/risk/drawdown")
async def get_drawdown():
    return {
        "dd_used_pct":  _risk_config["dd_used_pct"],
        "dd_limit_pct": _risk_config["dd_limit_pct"],
        "dd_used_ratio": _risk_config["dd_used_pct"] / _risk_config["dd_limit_pct"],
        "kill_switch_armed": _risk_config["kill_switch_armed"],
        "safe": _risk_config["dd_used_pct"] < _risk_config["dd_limit_pct"],
    }


# ─────────────────────────────────────────────────────────────
# ROUTES — EXECUTOR / WEBHOOK
# ─────────────────────────────────────────────────────────────

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "sentinel_secret_change_me")

_trades_today: list[dict] = []

@app.post("/api/webhook/alert")
async def receive_alert(alert: WebhookAlertRequest):
    """
    Receives TradingView webhook alerts.
    Validates, logs, and forwards to MT5 executor.
    """
    # Auth
    if alert.secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid webhook secret")

    # Kill switch check
    if _risk_config["kill_switch_armed"] and _risk_config["dd_used_pct"] >= _risk_config["dd_limit_pct"]:
        agent_store.add_log("executor", f"KILL SWITCH: Alert from {alert.strategy} BLOCKED — DD limit hit")
        return {"status": "blocked", "reason": "kill_switch_triggered"}

    # Log the trade
    trade = {
        "id":       f"T{int(time.time())}",
        "time":     datetime.utcnow().isoformat(),
        "strategy": alert.strategy,
        "action":   alert.action,
        "symbol":   alert.symbol,
        "contracts": alert.contracts,
        "price":    alert.price,
        "sl":       alert.sl,
        "tp":       alert.tp,
        "status":   "submitted",
    }
    _trades_today.append(trade)

    agent_store.update_agent("executor", {
        "orders_today": len(_trades_today),
        "last_alert_time": datetime.utcnow().strftime("%H:%M:%S"),
        "status": "live",
    })
    agent_store.add_log(
        "executor",
        f"Alert received: {alert.strategy} {alert.action.upper()} {alert.symbol} {alert.contracts}ct"
    )

    # Forward to executor bridge — it handles MT5, kill-switch, position sizing
    executor_result = await _executor_post("/webhook", {
        "secret":    WEBHOOK_SECRET,
        "strategy":  alert.strategy,
        "action":    alert.action,
        "symbol":    alert.symbol,
        "contracts": alert.contracts,
        "price":     alert.price or 0.0,
        "sl":        alert.sl or 0.0,
        "tp":        alert.tp or 0.0,
    })
    if executor_result:
        trade["status"] = executor_result.get("status", "submitted")
        trade["ticket"] = executor_result.get("ticket")
        agent_store.add_log("executor", f"Executor: {trade['status']} ticket={trade.get('ticket')}")
    else:
        agent_store.add_log("executor", "Executor bridge unreachable — trade logged locally only")

    return {"status": trade["status"], "trade_id": trade["id"], "executor": executor_result}


# ─────────────────────────────────────────────────────────────
# ROUTES — LOG
# ─────────────────────────────────────────────────────────────

@app.get("/api/log")
async def get_log(limit: int = 30):
    return {"log": agent_store.get_log(limit)}


# ─────────────────────────────────────────────────────────────
# WEBSOCKET — LIVE FEED
# ─────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────
# ROUTES — EXECUTOR BRIDGE (proxy to executor.py on :8080)
# ─────────────────────────────────────────────────────────────

async def _executor_get(path: str) -> Optional[dict]:
    """Proxy a GET request to the executor bridge."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{EXECUTOR_URL}{path}",
                timeout=aiohttp.ClientTimeout(total=3)
            ) as r:
                return await r.json() if r.status == 200 else None
    except Exception:
        return None


async def _executor_post(path: str, payload: dict) -> Optional[dict]:
    """POST to the executor bridge."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{EXECUTOR_URL}{path}",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=5)
            ) as r:
                return await r.json() if r.status in (200, 201) else None
    except Exception:
        return None


@app.get("/api/executor/health")
async def executor_health():
    data = await _executor_get("/health")
    return data or {"status": "unavailable", "mode": "offline"}


@app.get("/api/executor/trades")
async def executor_trades():
    data = await _executor_get("/trades/today")
    return data or {"trades": [], "stats": {}, "dd_used": 0}


@app.get("/api/executor/positions")
async def executor_positions():
    data = await _executor_get("/positions")
    return data or {"positions": [], "mode": "offline"}


@app.get("/api/executor/account")
async def executor_account():
    data = await _executor_get("/account")
    return data or {"balance": 0, "equity": 0, "mode": "offline"}


# ─────────────────────────────────────────────────────────────
# ROUTES — MONTE CARLO VALIDATOR
# ─────────────────────────────────────────────────────────────

VALIDATOR_SCRIPT = ARCHITECT_DIR / "validator.py"
_running_validations: set[str] = set()


def _query_mc(sql: str, params: tuple = ()) -> list[dict]:
    """Read from mc_results table in setups.db."""
    return query(sql, params, DB_PATH)


@app.get("/api/validate/results")
async def get_mc_results(min_sharpe: float = 0.0, passed_only: bool = False):
    """All stored MC results, joined with strategy name."""
    sql = """
        SELECT mc.*,
               r.title  AS strategy_name,
               r.source AS strategy_source
        FROM mc_results mc
        LEFT JOIN raw_setups r ON r.setup_id = mc.setup_id
        WHERE mc.sharpe_mean >= ?
    """
    params: list = [min_sharpe]
    if passed_only:
        sql += " AND mc.passed = 1"
    sql += " ORDER BY mc.sharpe_mean DESC"
    rows = _query_mc(sql, tuple(params))
    passed = sum(1 for r in rows if r.get("passed"))
    return {"results": rows, "count": len(rows), "passed": passed}


@app.get("/api/validate/{setup_id}")
async def get_mc_result(setup_id: str):
    """MC result for a specific strategy."""
    rows = _query_mc(
        """SELECT mc.*, r.title AS strategy_name
           FROM mc_results mc
           LEFT JOIN raw_setups r ON r.setup_id = mc.setup_id
           WHERE mc.setup_id = ?""",
        (setup_id,)
    )
    return {"result": rows[0] if rows else None,
            "running": setup_id in _running_validations}


@app.post("/api/validate/{setup_id}")
async def trigger_mc_validation(setup_id: str, runs: int = 2000):
    """Spawn validator.py as a subprocess for one strategy."""
    if setup_id in _running_validations:
        return {"status": "already_running", "setup_id": setup_id}

    if not VALIDATOR_SCRIPT.exists():
        raise HTTPException(
            status_code=503,
            detail=f"validator.py not found at {VALIDATOR_SCRIPT}"
        )

    _running_validations.add(setup_id)
    agent_store.update_agent("validator", {
        "status":           "testing",
        "progress":         0,
        "current_strategy": setup_id,
        "mc_iterations":    0,
        "mc_total":         runs,
        "task":             f"MC validation: {setup_id[:8]} ({runs:,} runs)",
    })
    agent_store.add_log("validator", f"MC started: {setup_id[:8]} — {runs:,} iterations")

    async def _run():
        try:
            cmd = [sys.executable, str(VALIDATOR_SCRIPT),
                   "--validate", setup_id, "--runs", str(runs)]
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(ARCHITECT_DIR),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            progress = 5
            async for line in proc.stdout:
                text = line.decode(errors="replace").strip()
                if text:
                    agent_store.add_log("validator", text[:120])
                    progress = min(progress + 5, 95)
                    agent_store.update_agent("validator", {
                        "progress": progress,
                        "task": text[:80],
                    })
            await proc.wait()
            agent_store.update_agent("validator", {
                "status": "idle", "progress": 100,
                "task": f"MC complete: {setup_id[:8]}",
            })
            agent_store.add_log("validator", f"MC complete: {setup_id[:8]}")
        except Exception as e:
            agent_store.update_agent("validator", {"status": "error"})
            agent_store.add_log("validator", f"MC error: {e}")
        finally:
            _running_validations.discard(setup_id)

    asyncio.create_task(_run())
    return {"status": "started", "setup_id": setup_id, "runs": runs}


@app.post("/api/validate/all")
async def trigger_mc_all(runs: int = 2000):
    """Spawn validator.py --validate-all for all unvalidated pine_built setups."""
    if not VALIDATOR_SCRIPT.exists():
        raise HTTPException(status_code=503, detail="validator.py not found")

    async def _run_all():
        try:
            cmd = [sys.executable, str(VALIDATOR_SCRIPT),
                   "--validate-all", "--runs", str(runs)]
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(ARCHITECT_DIR),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            async for line in proc.stdout:
                text = line.decode(errors="replace").strip()
                if text:
                    agent_store.add_log("validator", text[:120])
            await proc.wait()
            agent_store.update_agent("validator", {"status": "idle", "progress": 0})
            agent_store.add_log("validator", "Batch MC validation complete")
        except Exception as e:
            agent_store.add_log("validator", f"Batch MC error: {e}")

    asyncio.create_task(_run_all())
    return {"status": "started", "runs": runs}


@app.websocket("/ws/live")
async def websocket_live(ws: WebSocket):
    """
    WebSocket endpoint — portal connects here for live updates.
    Pushes: market quotes, agent state, log feed every 2s.
    """
    await manager.connect(ws)
    try:
        # Send full state immediately on connect
        market = await fetch_market_data()
        await ws.send_json({
            "type":   "init",
            "market": market,
            "agents": agent_store.get_all(),
            "log":    agent_store.get_log(20),
            "risk":   _risk_config,
        })

        # Keep connection alive, handle incoming messages
        while True:
            try:
                data = await asyncio.wait_for(ws.receive_text(), timeout=30)
                msg = json.loads(data)
                # Handle ping / client actions
                if msg.get("type") == "ping":
                    await ws.send_json({"type": "pong"})
                elif msg.get("type") == "run_cycle":
                    asyncio.create_task(run_sentinel(RunCycleRequest()))
            except asyncio.TimeoutError:
                await ws.send_json({"type": "ping"})
    except WebSocketDisconnect:
        manager.disconnect(ws)


# ─────────────────────────────────────────────────────────────
# DEMO DATA FALLBACKS (when DB doesn't exist yet)
# ─────────────────────────────────────────────────────────────

def _demo_signals():
    return [
        {"signal_id":"d1","concept_type":"ORDER_BLOCK","trading_setup":"OB + FVG confluence on H1","timeframe":"H1","market":"ES","entry_logic":"Enter at 50% OB retrace after SSL sweep","sl_logic":"Below OB low","tp_logic":"2R above OB high","confidence":0.91,"novelty":0.76,"pine_compatible":1,"alpha_score":0.94,"queued_for_architect":1},
        {"signal_id":"d2","concept_type":"FVG","trading_setup":"Displacement FVG retest on M15","timeframe":"M15","market":"NQ","entry_logic":"Enter at 50% EQ of FVG after return","sl_logic":"Below FVG low","tp_logic":"FVG top + equal range","confidence":0.84,"novelty":0.68,"pine_compatible":1,"alpha_score":0.88,"queued_for_architect":1},
        {"signal_id":"d3","concept_type":"LIQUIDITY_SWEEP","trading_setup":"BSL sweep + OB reaction","timeframe":"H4","market":"CL","entry_logic":"Close back below swept high + OB present","sl_logic":"Above sweep wick","tp_logic":"1:2.5 RR","confidence":0.79,"novelty":0.72,"pine_compatible":1,"alpha_score":0.84,"queued_for_architect":1},
        {"signal_id":"d4","concept_type":"BOS_CHOCH","trading_setup":"CHOCH confirmation long entry","timeframe":"M30","market":"EURUSD","entry_logic":"First CHOCH candle close after SSL sweep","sl_logic":"Below CHOCH low","tp_logic":"Previous swing high","confidence":0.75,"novelty":0.61,"pine_compatible":1,"alpha_score":0.79,"queued_for_architect":0},
    ]

def _demo_signal_stats():
    return [
        {"concept_type":"ORDER_BLOCK","count":14,"avg_score":0.81,"best_score":0.94,"pine_ready":12},
        {"concept_type":"FVG","count":11,"avg_score":0.78,"best_score":0.91,"pine_ready":10},
        {"concept_type":"LIQUIDITY_SWEEP","count":8,"avg_score":0.74,"best_score":0.88,"pine_ready":7},
        {"concept_type":"BOS_CHOCH","count":6,"avg_score":0.69,"best_score":0.82,"pine_ready":4},
    ]
