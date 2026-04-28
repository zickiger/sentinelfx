"""
╔══════════════════════════════════════════════════════════════╗
║   SENTINEL FX — EXECUTOR BRIDGE  v1.0                        ║
║                                                              ║
║   TradingView Alert -> This server -> MT5 -> Prop Firm       ║
║                                                              ║
║   Install:                                                   ║
║     pip install fastapi uvicorn MetaTrader5 python-dotenv    ║
║                                                              ║
║   Run:                                                       ║
║     python executor.py                                       ║
║                                                              ║
║   TradingView alert URL:                                     ║
║     http://YOUR-IP:8080/webhook                              ║
║                                                              ║
║   NOTE: MetaTrader5 only works on Windows with MT5 installed ║
╚══════════════════════════════════════════════════════════════╝
"""

import json
import logging
import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional

load_dotenv()

# ─────────────────────────────────────────────────────────────
# CONFIG  (set these in .env or Railway variables)
# ─────────────────────────────────────────────────────────────

WEBHOOK_SECRET   = os.environ.get("WEBHOOK_SECRET", "change_me")
MT5_LOGIN        = int(os.environ.get("MT5_LOGIN", "0"))
MT5_PASSWORD     = os.environ.get("MT5_PASSWORD", "")
MT5_SERVER       = os.environ.get("MT5_SERVER", "")
PROP_FIRM        = os.environ.get("PROP_FIRM", "Topstep SIM")

# Risk limits — must match your prop firm rules
DD_LIMIT_PCT     = float(os.environ.get("DD_LIMIT_PCT",    "4.5"))
MAX_CONTRACTS    = int(os.environ.get("MAX_CONTRACTS",      "3"))
RISK_PER_TRADE   = float(os.environ.get("RISK_PER_TRADE",  "0.5"))   # % of account
CONSISTENCY_PCT  = float(os.environ.get("CONSISTENCY_PCT", "30.0"))  # max % of target in one day
ACCOUNT_SIZE     = float(os.environ.get("ACCOUNT_SIZE",    "100000"))

# Anti-detection: randomise entry by 1-2 ticks
ANTI_DETECT      = os.environ.get("ANTI_DETECT", "true").lower() == "true"

DB_PATH          = Path(os.environ.get("EXECUTOR_DB", "data/executor.db"))

# ─────────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────────

Path("logs").mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("logs/executor.log"),
        logging.StreamHandler(),
    ]
)
log = logging.getLogger("executor")

# ─────────────────────────────────────────────────────────────
# DATABASE
# ─────────────────────────────────────────────────────────────

DB_PATH.parent.mkdir(parents=True, exist_ok=True)

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS trades (
            id           TEXT PRIMARY KEY,
            ts           TEXT,
            strategy     TEXT,
            action       TEXT,
            symbol       TEXT,
            contracts    INTEGER,
            entry_price  REAL,
            sl_price     REAL,
            tp_price     REAL,
            mt5_ticket   INTEGER,
            status       TEXT,
            pnl          REAL,
            closed_at    TEXT,
            error        TEXT
        );
        CREATE TABLE IF NOT EXISTS daily_stats (
            date         TEXT PRIMARY KEY,
            trades       INTEGER DEFAULT 0,
            winners      INTEGER DEFAULT 0,
            losers       INTEGER DEFAULT 0,
            gross_pnl    REAL DEFAULT 0,
            dd_used_pct  REAL DEFAULT 0,
            kill_switch  INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS kill_switch_log (
            ts      TEXT,
            reason  TEXT,
            dd_pct  REAL
        );
    """)
    conn.commit()
    conn.close()

init_db()

def db_conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c

def save_trade(trade: dict):
    with db_conn() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO trades
            VALUES (:id,:ts,:strategy,:action,:symbol,:contracts,
                    :entry_price,:sl_price,:tp_price,:mt5_ticket,
                    :status,:pnl,:closed_at,:error)
        """, trade)

def get_today_stats() -> dict:
    today = datetime.utcnow().strftime("%Y-%m-%d")
    with db_conn() as conn:
        row = conn.execute(
            "SELECT * FROM daily_stats WHERE date=?", (today,)
        ).fetchone()
        if not row:
            conn.execute(
                "INSERT OR IGNORE INTO daily_stats (date) VALUES (?)", (today,)
            )
            conn.commit()
            return {"date": today, "trades": 0, "winners": 0, "losers": 0,
                    "gross_pnl": 0.0, "dd_used_pct": 0.0, "kill_switch": 0}
        return dict(row)

def update_today_stats(pnl: float, won: bool):
    today = datetime.utcnow().strftime("%Y-%m-%d")
    with db_conn() as conn:
        conn.execute("""
            INSERT INTO daily_stats (date) VALUES (?) ON CONFLICT(date) DO NOTHING
        """, (today,))
        conn.execute("""
            UPDATE daily_stats SET
                trades   = trades + 1,
                winners  = winners + CASE WHEN ? THEN 1 ELSE 0 END,
                losers   = losers  + CASE WHEN ? THEN 0 ELSE 1 END,
                gross_pnl = gross_pnl + ?
            WHERE date = ?
        """, (won, won, pnl, today))
        conn.commit()

def get_all_trades(limit: int = 50) -> list[dict]:
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM trades ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

# ─────────────────────────────────────────────────────────────
# MT5 CONNECTION
# ─────────────────────────────────────────────────────────────

mt5_available = False
mt5 = None

try:
    import MetaTrader5 as mt5_lib
    mt5 = mt5_lib
    mt5_available = True
    log.info("MetaTrader5 library loaded")
except ImportError:
    log.warning(
        "MetaTrader5 not installed or not on Windows. "
        "Running in SIMULATION mode. "
        "Install: pip install MetaTrader5"
    )


def mt5_connect() -> bool:
    if not mt5_available or not MT5_LOGIN:
        return False
    try:
        if not mt5.initialize():
            log.error(f"MT5 initialize failed: {mt5.last_error()}")
            return False
        if not mt5.login(MT5_LOGIN, password=MT5_PASSWORD, server=MT5_SERVER):
            log.error(f"MT5 login failed: {mt5.last_error()}")
            return False
        info = mt5.account_info()
        log.info(
            f"MT5 connected: {info.name} | "
            f"Balance: ${info.balance:,.2f} | "
            f"Server: {MT5_SERVER}"
        )
        return True
    except Exception as e:
        log.error(f"MT5 connection error: {e}")
        return False


def mt5_disconnect():
    if mt5_available:
        try: mt5.shutdown()
        except Exception: pass


def get_account_info() -> dict:
    if not mt5_available or not MT5_LOGIN:
        return {
            "balance": ACCOUNT_SIZE,
            "equity":  ACCOUNT_SIZE,
            "profit":  0.0,
            "margin":  0.0,
            "mode":    "simulation"
        }
    try:
        info = mt5.account_info()
        return {
            "balance": info.balance,
            "equity":  info.equity,
            "profit":  info.profit,
            "margin":  info.margin,
            "mode":    "live"
        }
    except Exception:
        return {"balance": ACCOUNT_SIZE, "equity": ACCOUNT_SIZE,
                "profit": 0.0, "margin": 0.0, "mode": "error"}


def calc_drawdown() -> float:
    """Calculate today's drawdown as % of account balance."""
    try:
        if mt5_available and MT5_LOGIN:
            info = mt5.account_info()
            # Compare today's starting balance to current equity
            stats = get_today_stats()
            start_equity = info.balance - stats.get("gross_pnl", 0)
            if start_equity <= 0:
                return 0.0
            dd = (start_equity - info.equity) / start_equity * 100
            return max(dd, 0.0)
        else:
            stats = get_today_stats()
            pnl   = stats.get("gross_pnl", 0)
            if pnl >= 0:
                return 0.0
            return abs(pnl) / ACCOUNT_SIZE * 100
    except Exception:
        return 0.0


def calc_position_size(entry: float, sl: float, symbol: str) -> int:
    """Risk-based position sizing — never exceed MAX_CONTRACTS."""
    try:
        risk_dollars = ACCOUNT_SIZE * (RISK_PER_TRADE / 100)
        sl_distance  = abs(entry - sl)
        if sl_distance <= 0:
            return 1

        if mt5_available and MT5_LOGIN:
            info = mt5.symbol_info(symbol)
            if info:
                tick_value = info.trade_tick_value
                tick_size  = info.trade_tick_size
                sl_ticks   = sl_distance / tick_size
                contracts  = int(risk_dollars / (sl_ticks * tick_value))
                return max(1, min(contracts, MAX_CONTRACTS))

        # Fallback: simple % sizing
        sl_pct    = sl_distance / entry
        contracts = int(risk_dollars / (ACCOUNT_SIZE * sl_pct))
        return max(1, min(contracts, MAX_CONTRACTS))
    except Exception:
        return 1


def place_order(
    symbol: str,
    action: str,        # "buy" | "sell"
    contracts: int,
    price: float,
    sl: float,
    tp: float,
    comment: str = "SentinelFX"
) -> tuple[bool, int, str]:
    """
    Place order on MT5. Returns (success, ticket_number, error_msg).
    In simulation mode, returns a fake ticket.
    """
    # Anti-detection: randomise entry by 1-2 ticks
    if ANTI_DETECT and mt5_available and MT5_LOGIN:
        try:
            sym_info  = mt5.symbol_info(symbol)
            tick_size = sym_info.trade_tick_size if sym_info else 0.01
            import random
            jitter = random.choice([-1, 0, 1, 2]) * tick_size
            price += jitter
        except Exception:
            pass

    if not mt5_available or not MT5_LOGIN:
        # Simulation mode
        fake_ticket = int(time.time() * 1000) % 999999
        log.info(
            f"[SIM] {action.upper()} {contracts}x {symbol} "
            f"@ {price:.4f} SL={sl:.4f} TP={tp:.4f} "
            f"ticket={fake_ticket}"
        )
        return True, fake_ticket, ""

    try:
        order_type = mt5.ORDER_TYPE_BUY if action == "buy" else mt5.ORDER_TYPE_SELL

        request = {
            "action":      mt5.TRADE_ACTION_DEAL,
            "symbol":      symbol,
            "volume":      float(contracts),
            "type":        order_type,
            "price":       price,
            "sl":          sl,
            "tp":          tp,
            "deviation":   10,
            "magic":       20250101,
            "comment":     comment[:31],
            "type_time":   mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        result = mt5.order_send(request)

        if result.retcode == mt5.TRADE_RETCODE_DONE:
            log.info(
                f"Order placed: {action.upper()} {contracts}x {symbol} "
                f"ticket={result.order}"
            )
            return True, result.order, ""
        else:
            err = f"MT5 error {result.retcode}: {result.comment}"
            log.error(err)
            return False, 0, err

    except Exception as e:
        log.error(f"order_send exception: {e}")
        return False, 0, str(e)


def close_all_positions(reason: str = "kill_switch"):
    """Flatten all open positions immediately."""
    if not mt5_available or not MT5_LOGIN:
        log.info(f"[SIM] Close all positions — reason: {reason}")
        return

    try:
        positions = mt5.positions_get()
        if not positions:
            return
        for pos in positions:
            close_type = (mt5.ORDER_TYPE_SELL
                          if pos.type == mt5.POSITION_TYPE_BUY
                          else mt5.ORDER_TYPE_BUY)
            price = (mt5.symbol_info_tick(pos.symbol).bid
                     if pos.type == mt5.POSITION_TYPE_BUY
                     else mt5.symbol_info_tick(pos.symbol).ask)
            request = {
                "action":   mt5.TRADE_ACTION_DEAL,
                "symbol":   pos.symbol,
                "volume":   pos.volume,
                "type":     close_type,
                "position": pos.ticket,
                "price":    price,
                "deviation": 20,
                "magic":    20250101,
                "comment":  f"SentinelFX {reason}",
            }
            result = mt5.order_send(request)
            if result.retcode == mt5.TRADE_RETCODE_DONE:
                log.info(f"Closed position {pos.ticket} ({pos.symbol})")
            else:
                log.error(f"Failed to close {pos.ticket}: {result.comment}")
    except Exception as e:
        log.error(f"close_all error: {e}")

# ─────────────────────────────────────────────────────────────
# KILL SWITCH
# ─────────────────────────────────────────────────────────────

_kill_switch_armed   = True
_kill_switch_tripped = False

def check_kill_switch() -> tuple[bool, str]:
    """
    Returns (blocked, reason).
    Checks daily drawdown, consistency rule, and manual kill.
    """
    global _kill_switch_tripped

    if not _kill_switch_armed:
        return False, ""

    if _kill_switch_tripped:
        return True, "Kill switch previously tripped — reset required"

    dd = calc_drawdown()
    if dd >= DD_LIMIT_PCT:
        _kill_switch_tripped = True
        close_all_positions("dd_kill_switch")
        reason = f"Daily drawdown {dd:.2f}% >= limit {DD_LIMIT_PCT}%"
        with db_conn() as conn:
            conn.execute(
                "INSERT INTO kill_switch_log VALUES (?,?,?)",
                (datetime.utcnow().isoformat(), reason, dd)
            )
            today = datetime.utcnow().strftime("%Y-%m-%d")
            conn.execute(
                "UPDATE daily_stats SET kill_switch=1 WHERE date=?", (today,)
            )
            conn.commit()
        log.warning(f"KILL SWITCH TRIPPED: {reason}")
        return True, reason

    return False, ""

# ─────────────────────────────────────────────────────────────
# FASTAPI APP
# ─────────────────────────────────────────────────────────────

app = FastAPI(title="SentinelFX Executor Bridge", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─────────────────────────────────────────────────────────────
# MODELS
# ─────────────────────────────────────────────────────────────

class WebhookPayload(BaseModel):
    """
    TradingView alert message format.
    Paste this JSON into your TradingView alert message box:

    {
      "secret":    "your_webhook_secret",
      "strategy":  "{{strategy.order.comment}}",
      "action":    "{{strategy.order.action}}",
      "symbol":    "{{ticker}}",
      "contracts": 1,
      "price":     {{close}},
      "sl":        0,
      "tp":        0
    }
    """
    secret:    str
    strategy:  str
    action:    str           # "buy" | "sell" | "close_all"
    symbol:    str
    contracts: int   = 1
    price:     float = 0.0
    sl:        float = 0.0
    tp:        float = 0.0


class ManualTradeRequest(BaseModel):
    strategy:  str
    action:    str
    symbol:    str
    contracts: int   = 1
    price:     float = 0.0
    sl:        float = 0.0
    tp:        float = 0.0


class RiskConfigUpdate(BaseModel):
    dd_limit_pct:      Optional[float] = None
    max_contracts:     Optional[int]   = None
    kill_switch_armed: Optional[bool]  = None

# ─────────────────────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return {
        "service":      "SentinelFX Executor Bridge",
        "version":      "1.0.0",
        "mode":         "live" if (mt5_available and MT5_LOGIN) else "simulation",
        "prop_firm":    PROP_FIRM,
        "kill_armed":   _kill_switch_armed,
        "kill_tripped": _kill_switch_tripped,
    }


@app.get("/health")
async def health():
    dd     = calc_drawdown()
    stats  = get_today_stats()
    killed, reason = check_kill_switch()
    acct   = get_account_info()
    return {
        "status":        "ok",
        "mode":          acct["mode"],
        "dd_used_pct":   round(dd, 2),
        "dd_limit_pct":  DD_LIMIT_PCT,
        "kill_tripped":  _kill_switch_tripped,
        "kill_armed":    _kill_switch_armed,
        "trades_today":  stats.get("trades", 0),
        "pnl_today":     stats.get("gross_pnl", 0),
        "balance":       acct.get("balance", 0),
        "equity":        acct.get("equity", 0),
        "ts":            datetime.utcnow().isoformat(),
    }


@app.post("/webhook")
async def receive_webhook(payload: WebhookPayload):
    """
    Main webhook endpoint — TradingView sends alerts here.
    Point your TradingView alert at:
      http://YOUR-IP:8080/webhook
    """
    # ── Auth ──────────────────────────────────────────────────
    if payload.secret != WEBHOOK_SECRET:
        log.warning(f"Invalid webhook secret from {payload.strategy}")
        raise HTTPException(status_code=401, detail="Invalid secret")

    trade_id = f"T{int(time.time()*1000)}"
    log.info(
        f"Webhook received: {payload.strategy} | "
        f"{payload.action.upper()} {payload.symbol} "
        f"{payload.contracts}ct @ {payload.price}"
    )

    # ── Close all ─────────────────────────────────────────────
    if payload.action.lower() == "close_all":
        close_all_positions(f"manual_{payload.strategy}")
        return {"status": "closed_all", "trade_id": trade_id}

    # ── Kill switch check ─────────────────────────────────────
    blocked, reason = check_kill_switch()
    if blocked:
        log.warning(f"Trade BLOCKED — kill switch: {reason}")
        save_trade({
            "id": trade_id, "ts": datetime.utcnow().isoformat(),
            "strategy": payload.strategy, "action": payload.action,
            "symbol": payload.symbol, "contracts": payload.contracts,
            "entry_price": payload.price, "sl_price": payload.sl,
            "tp_price": payload.tp, "mt5_ticket": 0,
            "status": "blocked", "pnl": 0, "closed_at": None,
            "error": reason
        })
        return {"status": "blocked", "reason": reason, "trade_id": trade_id}

    # ── Validate inputs ───────────────────────────────────────
    action = payload.action.lower()
    if action not in ("buy", "sell"):
        raise HTTPException(status_code=400, detail=f"Invalid action: {action}")

    # If SL/TP not provided by TV alert, use defaults
    price = payload.price
    sl    = payload.sl
    tp    = payload.tp

    # Auto SL/TP if not set (0.3% SL, 2R TP)
    if sl == 0 or tp == 0:
        sl_pct = 0.003
        rr     = 2.0
        if action == "buy":
            sl = price * (1 - sl_pct) if sl == 0 else sl
            tp = price + (price - sl) * rr if tp == 0 else tp
        else:
            sl = price * (1 + sl_pct) if sl == 0 else sl
            tp = price - (sl - price) * rr if tp == 0 else tp

    # Auto position size
    contracts = payload.contracts
    if contracts <= 0:
        contracts = calc_position_size(price, sl, payload.symbol)

    # ── Place order ───────────────────────────────────────────
    success, ticket, error = place_order(
        symbol=payload.symbol,
        action=action,
        contracts=contracts,
        price=price,
        sl=sl,
        tp=tp,
        comment=f"SFX_{payload.strategy[:10]}"
    )

    status = "submitted" if success else "failed"

    save_trade({
        "id": trade_id, "ts": datetime.utcnow().isoformat(),
        "strategy": payload.strategy, "action": action,
        "symbol": payload.symbol, "contracts": contracts,
        "entry_price": price, "sl_price": sl,
        "tp_price": tp, "mt5_ticket": ticket,
        "status": status, "pnl": 0, "closed_at": None,
        "error": error
    })

    return {
        "status":    status,
        "trade_id":  trade_id,
        "ticket":    ticket,
        "symbol":    payload.symbol,
        "action":    action,
        "contracts": contracts,
        "price":     price,
        "sl":        sl,
        "tp":        tp,
        "error":     error or None,
    }


@app.post("/trade/manual")
async def manual_trade(req: ManualTradeRequest):
    """Place a trade manually (for testing without TradingView)."""
    fake_payload = WebhookPayload(
        secret=WEBHOOK_SECRET,
        **req.dict()
    )
    return await receive_webhook(fake_payload)


@app.get("/trades")
async def get_trades(limit: int = 50):
    """All executed trades."""
    trades = get_all_trades(limit)
    total_pnl = sum(t.get("pnl", 0) for t in trades)
    winners   = sum(1 for t in trades if t.get("pnl", 0) > 0)
    return {
        "trades":    trades,
        "count":     len(trades),
        "total_pnl": round(total_pnl, 2),
        "win_rate":  round(winners / len(trades) * 100, 1) if trades else 0,
    }


@app.get("/trades/today")
async def get_today_trades():
    """Today's trades and stats."""
    today  = datetime.utcnow().strftime("%Y-%m-%d")
    stats  = get_today_stats()
    dd     = calc_drawdown()
    trades = [t for t in get_all_trades(200) if t["ts"].startswith(today)]
    return {
        "date":       today,
        "trades":     trades,
        "stats":      stats,
        "dd_used":    round(dd, 2),
        "dd_limit":   DD_LIMIT_PCT,
        "kill_armed": _kill_switch_armed,
        "kill_tripped": _kill_switch_tripped,
    }


@app.post("/risk/config")
async def update_risk_config(cfg: RiskConfigUpdate):
    """Receive risk config updates pushed from the backend."""
    global DD_LIMIT_PCT, MAX_CONTRACTS, _kill_switch_armed
    if cfg.dd_limit_pct is not None:
        DD_LIMIT_PCT = cfg.dd_limit_pct
    if cfg.max_contracts is not None:
        MAX_CONTRACTS = cfg.max_contracts
    if cfg.kill_switch_armed is not None:
        _kill_switch_armed = cfg.kill_switch_armed
    log.info(f"Risk config updated: DD={DD_LIMIT_PCT}% max_ct={MAX_CONTRACTS} kill={_kill_switch_armed}")
    return {"status": "updated", "dd_limit_pct": DD_LIMIT_PCT,
            "max_contracts": MAX_CONTRACTS, "kill_switch_armed": _kill_switch_armed}


@app.post("/kill-switch/reset")
async def reset_kill_switch():
    """Reset kill switch after reviewing the day."""
    global _kill_switch_tripped
    _kill_switch_tripped = False
    log.info("Kill switch manually reset")
    return {"status": "reset", "ts": datetime.utcnow().isoformat()}


@app.post("/kill-switch/toggle")
async def toggle_kill_switch():
    """Arm or disarm the kill switch."""
    global _kill_switch_armed
    _kill_switch_armed = not _kill_switch_armed
    state = "armed" if _kill_switch_armed else "disarmed"
    log.info(f"Kill switch {state}")
    return {"status": state, "armed": _kill_switch_armed}


@app.get("/account")
async def get_account():
    """MT5 account info."""
    info = get_account_info()
    dd   = calc_drawdown()
    return {
        **info,
        "prop_firm":   PROP_FIRM,
        "dd_used_pct": round(dd, 2),
        "dd_limit_pct": DD_LIMIT_PCT,
        "max_contracts": MAX_CONTRACTS,
        "risk_per_trade": RISK_PER_TRADE,
        "anti_detect":   ANTI_DETECT,
    }


@app.get("/positions")
async def get_positions():
    """Open MT5 positions."""
    if not mt5_available or not MT5_LOGIN:
        return {"positions": [], "mode": "simulation"}
    try:
        positions = mt5.positions_get() or []
        return {
            "positions": [
                {
                    "ticket":  p.ticket,
                    "symbol":  p.symbol,
                    "type":    "buy" if p.type == 0 else "sell",
                    "volume":  p.volume,
                    "price":   p.price_open,
                    "sl":      p.sl,
                    "tp":      p.tp,
                    "profit":  p.profit,
                    "comment": p.comment,
                }
                for p in positions
            ],
            "count": len(positions),
            "mode":  "live"
        }
    except Exception as e:
        return {"positions": [], "error": str(e)}


@app.on_event("startup")
async def startup():
    log.info("SentinelFX Executor Bridge starting...")
    if MT5_LOGIN:
        connected = mt5_connect()
        if connected:
            log.info("MT5 connected successfully")
        else:
            log.warning("MT5 connection failed — running in simulation mode")
    else:
        log.info("No MT5 credentials — running in simulation mode")
    log.info(f"Webhook endpoint: POST /webhook")
    log.info(f"Kill switch: ARMED | DD limit: {DD_LIMIT_PCT}%")


@app.on_event("shutdown")
async def shutdown():
    mt5_disconnect()
    log.info("Executor Bridge shutdown")


# ─────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run("executor:app", host="0.0.0.0", port=port, reload=False)
