"""
╔══════════════════════════════════════════════════════════════╗
║   SENTINEL FX — MONTE CARLO VALIDATOR  v1.0                  ║
║                                                              ║
║   For each pine_built strategy in setups.db:                 ║
║     1. Fetch historical OHLCV via yfinance                   ║
║     2. Detect signals via concept-specific rules             ║
║     3. Bootstrap-resample trade list N times (MC)            ║
║     4. Compute Sharpe, Calmar, P(ruin) distributions         ║
║     5. Store results in mc_results table                     ║
║                                                              ║
║   Usage:                                                     ║
║     python validator.py --validate <setup_id>                ║
║     python validator.py --validate-all                       ║
║     python validator.py --list                               ║
╚══════════════════════════════════════════════════════════════╝
"""
import argparse
import json
import math
import os
import random
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import yfinance as yf
from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────

DB_PATH        = Path(os.environ.get("DB_PATH",        "data/setups.db"))
MC_RUNS        = int(os.environ.get("MC_RUNS",         "2000"))
RISK_PER_TRADE = float(os.environ.get("RISK_PER_TRADE", "0.5"))   # % equity per trade
RR_RATIO       = float(os.environ.get("RR_RATIO",       "2.0"))
RUIN_THRESHOLD = float(os.environ.get("RUIN_THRESHOLD", "20.0"))   # % DD = ruin

PASS_SHARPE = 1.5
PASS_CALMAR = 1.8
PASS_PRUIN  = 3.0   # max % of MC runs that hit ruin

SYMBOL_MAP = {
    "ES": "ES=F", "NQ": "NQ=F", "CL": "CL=F", "GC": "GC=F",
    "SI": "SI=F", "ZB": "ZB=F",
    "EURUSD": "EURUSD=X", "EUR/USD": "EURUSD=X",
    "GBPUSD": "GBPUSD=X", "GBP/USD": "GBPUSD=X",
    "USDJPY": "JPY=X",    "USD/JPY": "JPY=X",
    "BTC": "BTC-USD",     "BTC/USD": "BTC-USD",
    "DXY": "DX-Y.NYB",   "VIX": "^VIX",
}

# timeframe key → (yfinance period, yfinance interval)
TIMEFRAME_MAP = {
    "M5":  ("7d",   "5m"),
    "M15": ("60d",  "15m"),
    "M30": ("60d",  "30m"),
    "H1":  ("730d", "1h"),
    "H4":  ("730d", "4h"),
    "D1":  ("5y",   "1d"),
    "W1":  ("10y",  "1wk"),
}

# ─────────────────────────────────────────────────────────────
# DATABASE
# ─────────────────────────────────────────────────────────────

def init_mc_table():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mc_results (
            id                  TEXT PRIMARY KEY,
            setup_id            TEXT NOT NULL,
            run_at              TEXT,
            market              TEXT,
            timeframe           TEXT,
            concept_type        TEXT,
            n_trades            INTEGER,
            n_runs              INTEGER,
            sharpe_mean         REAL,
            sharpe_std          REAL,
            sharpe_p5           REAL,
            sharpe_p95          REAL,
            calmar_mean         REAL,
            calmar_std          REAL,
            max_dd_mean         REAL,
            max_dd_worst        REAL,
            win_rate            REAL,
            p_ruin              REAL,
            final_return_mean   REAL,
            final_return_std    REAL,
            equity_curves_json  TEXT,
            sharpe_dist_json    TEXT,
            dd_dist_json        TEXT,
            passed              INTEGER,
            verdict_reason      TEXT,
            FOREIGN KEY (setup_id) REFERENCES raw_setups(setup_id)
        )
    """)
    conn.commit()
    conn.close()


def get_pending_setups() -> list[dict]:
    """Setups with pine_built=1 not yet MC-validated."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT r.setup_id, v.concept_type, v.market, v.timeframe, v.confidence
        FROM raw_setups r
        JOIN critic_verdicts v ON r.setup_id = v.setup_id
        WHERE v.pine_built = 1
          AND r.setup_id NOT IN (SELECT setup_id FROM mc_results)
        ORDER BY v.confidence DESC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_setup(setup_id: str) -> dict | None:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute("""
        SELECT r.setup_id, v.concept_type, v.market, v.timeframe, v.confidence
        FROM raw_setups r
        JOIN critic_verdicts v ON r.setup_id = v.setup_id
        WHERE r.setup_id = ?
    """, (setup_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def save_mc_result(result: dict):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT OR REPLACE INTO mc_results VALUES (
            :id, :setup_id, :run_at, :market, :timeframe, :concept_type,
            :n_trades, :n_runs,
            :sharpe_mean, :sharpe_std, :sharpe_p5, :sharpe_p95,
            :calmar_mean, :calmar_std, :max_dd_mean, :max_dd_worst,
            :win_rate, :p_ruin, :final_return_mean, :final_return_std,
            :equity_curves_json, :sharpe_dist_json, :dd_dist_json,
            :passed, :verdict_reason
        )
    """, result)
    conn.commit()
    conn.close()


def update_verdict_mc(setup_id: str, passed: bool, sharpe: float,
                      calmar: float, pruin: float):
    """Write MC summary back to critic_verdicts for backend queries."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        UPDATE critic_verdicts
        SET backtest_run=1, backtest_sharpe=?, backtest_result=?
        WHERE setup_id=?
    """, (
        round(sharpe, 2),
        json.dumps({"mc_passed": passed, "calmar": calmar, "p_ruin": pruin}),
        setup_id,
    ))
    conn.commit()
    conn.close()


# ─────────────────────────────────────────────────────────────
# DATA FETCHING
# ─────────────────────────────────────────────────────────────

def fetch_ohlcv(market: str, timeframe: str):
    """Return (opens, highs, lows, closes) as numpy arrays, or None."""
    raw    = market.upper().replace("/", "").replace(" ", "")
    symbol = next((v for k, v in SYMBOL_MAP.items() if k in raw), "ES=F")

    tf_key = timeframe.upper().replace("MIN", "M").replace("HOUR", "H")
    period, interval = TIMEFRAME_MAP.get(tf_key, ("730d", "1h"))

    try:
        df = yf.download(symbol, period=period, interval=interval,
                         progress=False, auto_adjust=True)
        if df is None or len(df) < 50:
            return None
        # Handle MultiIndex columns from yfinance
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
        o = df["Open"].values.astype(float)
        h = df["High"].values.astype(float)
        l = df["Low"].values.astype(float)
        c = df["Close"].values.astype(float)
        return o, h, l, c
    except Exception as e:
        print(f"[validator] fetch error {symbol}: {e}")
        return None


# ─────────────────────────────────────────────────────────────
# SIGNAL DETECTION
# ─────────────────────────────────────────────────────────────

def _outcome(highs, lows, start, n, direction, sl, tp):
    """Walk bars from `start` until SL or TP is hit. Returns R-multiple."""
    for k in range(start, min(start + 40, n)):
        if direction == 1:
            if lows[k]  <= sl: return -1.0
            if highs[k] >= tp: return RR_RATIO
        else:
            if highs[k] >= sl: return -1.0
            if lows[k]  <= tp: return RR_RATIO
    return 0.0   # timeout — flat


def detect_fvg(o, h, l, c) -> list[float]:
    """Fair Value Gap: 3-candle imbalance, enter at 50% fill."""
    trades, n = [], len(c)
    for i in range(2, n - 40):
        # Bullish FVG
        if h[i-2] < l[i] and c[i] > o[i]:
            gap_low, gap_high = h[i-2], l[i]
            entry = (gap_low + gap_high) / 2
            sl    = gap_low  * 0.999
            tp    = entry + (entry - sl) * RR_RATIO
            for j in range(i + 1, min(i + 15, n - 1)):
                if l[j] <= entry:
                    r = _outcome(h, l, j + 1, n, 1, sl, tp)
                    if r != 0.0:
                        trades.append(r)
                    break
        # Bearish FVG
        if l[i-2] > h[i] and c[i] < o[i]:
            gap_high, gap_low = l[i-2], h[i]
            entry = (gap_low + gap_high) / 2
            sl    = gap_high * 1.001
            tp    = entry - (sl - entry) * RR_RATIO
            for j in range(i + 1, min(i + 15, n - 1)):
                if h[j] >= entry:
                    r = _outcome(h, l, j + 1, n, -1, sl, tp)
                    if r != 0.0:
                        trades.append(r)
                    break
    return trades


def detect_order_block(o, h, l, c) -> list[float]:
    """Order Block: last opposing candle before a strong impulse move."""
    trades, n = [], len(c)
    for i in range(3, n - 40):
        body     = abs(c[i] - o[i])
        avg_body = float(np.mean(np.abs(c[max(0,i-20):i] - o[max(0,i-20):i])))
        if avg_body < 1e-8:
            continue
        # Bullish impulse → bearish OB one candle back
        if c[i] > o[i] and body > 1.5 * avg_body:
            if c[i-1] < o[i-1]:
                entry = (o[i-1] + c[i-1]) / 2
                sl    = c[i-1] * 0.999
                tp    = entry + (entry - sl) * RR_RATIO
                for j in range(i + 1, min(i + 20, n - 1)):
                    if l[j] <= entry:
                        if l[j] <= sl:
                            trades.append(-1.0)
                        else:
                            r = _outcome(h, l, j + 1, n, 1, sl, tp)
                            if r != 0.0:
                                trades.append(r)
                        break
        # Bearish impulse → bullish OB one candle back
        if c[i] < o[i] and body > 1.5 * avg_body:
            if c[i-1] > o[i-1]:
                entry = (o[i-1] + c[i-1]) / 2
                sl    = c[i-1] * 1.001
                tp    = entry - (sl - entry) * RR_RATIO
                for j in range(i + 1, min(i + 20, n - 1)):
                    if h[j] >= entry:
                        if h[j] >= sl:
                            trades.append(-1.0)
                        else:
                            r = _outcome(h, l, j + 1, n, -1, sl, tp)
                            if r != 0.0:
                                trades.append(r)
                        break
    return trades


def detect_liquidity_sweep(o, h, l, c) -> list[float]:
    """Liquidity Sweep: wick beyond swing high/low, close back inside."""
    trades, n = [], len(c)
    lb = 20
    for i in range(lb, n - 40):
        s_high = float(np.max(h[i-lb:i]))
        s_low  = float(np.min(l[i-lb:i]))
        # Bearish sweep — wick above swing high, body stays below
        if h[i] > s_high and c[i] < s_high:
            entry = c[i]
            sl    = h[i] * 1.001
            tp    = entry - (sl - entry) * RR_RATIO
            r = _outcome(h, l, i + 1, n, -1, sl, tp)
            if r != 0.0:
                trades.append(r)
        # Bullish sweep — wick below swing low, body stays above
        if l[i] < s_low and c[i] > s_low:
            entry = c[i]
            sl    = l[i] * 0.999
            tp    = entry + (entry - sl) * RR_RATIO
            r = _outcome(h, l, i + 1, n, 1, sl, tp)
            if r != 0.0:
                trades.append(r)
    return trades


def detect_bos_choch(o, h, l, c) -> list[float]:
    """Break of Structure: close beyond prior swing, enter on pullback to level."""
    trades, n = [], len(c)
    lb = 15
    for i in range(lb, n - 40):
        p_high = float(np.max(h[i-lb:i]))
        p_low  = float(np.min(l[i-lb:i]))
        # Bullish BOS
        if c[i] > p_high and c[i-1] <= p_high:
            entry = p_high
            sl    = float(np.min(l[max(0,i-5):i])) * 0.999
            tp    = entry + (entry - sl) * RR_RATIO
            for j in range(i + 1, min(i + 15, n - 1)):
                if l[j] <= entry:
                    if l[j] <= sl:
                        trades.append(-1.0)
                    else:
                        r = _outcome(h, l, j + 1, n, 1, sl, tp)
                        if r != 0.0:
                            trades.append(r)
                    break
        # Bearish BOS
        if c[i] < p_low and c[i-1] >= p_low:
            entry = p_low
            sl    = float(np.max(h[max(0,i-5):i])) * 1.001
            tp    = entry - (sl - entry) * RR_RATIO
            for j in range(i + 1, min(i + 15, n - 1)):
                if h[j] >= entry:
                    if h[j] >= sl:
                        trades.append(-1.0)
                    else:
                        r = _outcome(h, l, j + 1, n, -1, sl, tp)
                        if r != 0.0:
                            trades.append(r)
                    break
    return trades


DETECTORS = {
    "FVG":             detect_fvg,
    "ORDER_BLOCK":     detect_order_block,
    "LIQUIDITY_SWEEP": detect_liquidity_sweep,
    "BOS_CHOCH":       detect_bos_choch,
}


# ─────────────────────────────────────────────────────────────
# METRICS
# ─────────────────────────────────────────────────────────────

def equity_curve(trades: list[float], risk_pct: float = RISK_PER_TRADE) -> np.ndarray:
    eq = [100.0]
    for r in trades:
        eq.append(eq[-1] * (1.0 + r * risk_pct / 100.0))
    return np.array(eq)


def max_drawdown_pct(eq: np.ndarray) -> float:
    peak = np.maximum.accumulate(eq)
    dd   = (peak - eq) / peak * 100
    return float(np.max(dd))


def sharpe(eq: np.ndarray, ann: int = 252) -> float:
    rets = np.diff(eq) / eq[:-1]
    if len(rets) < 2 or np.std(rets) < 1e-10:
        return 0.0
    return float(np.mean(rets) / np.std(rets) * math.sqrt(ann))


def calmar(eq: np.ndarray, ann: int = 252) -> float:
    total_r = eq[-1] / eq[0] - 1.0
    n_years = max(len(eq) / ann, 0.01)
    ann_r   = (1.0 + total_r) ** (1.0 / n_years) - 1.0
    mdd     = max_drawdown_pct(eq) / 100.0
    return float(ann_r / mdd) if mdd > 0 else 10.0


# ─────────────────────────────────────────────────────────────
# MONTE CARLO ENGINE
# ─────────────────────────────────────────────────────────────

def run_monte_carlo(trades: list[float], n_runs: int = MC_RUNS) -> dict:
    """
    Bootstrap MC: resample the trade list n_runs times.
    Returns full stat dict + sample equity curves for charts.
    """
    sharpes, calmars, max_dds, final_rets = [], [], [], []
    ruin_count   = 0
    sample_curves: list[list[float]] = []
    k = max(len(trades), 30)

    for i in range(n_runs):
        sample = random.choices(trades, k=k)
        eq     = equity_curve(sample)
        sh     = sharpe(eq)
        ca     = calmar(eq)
        md     = max_drawdown_pct(eq)
        fr     = float((eq[-1] / eq[0] - 1.0) * 100.0)

        sharpes.append(sh)
        calmars.append(ca)
        max_dds.append(md)
        final_rets.append(fr)

        if md >= RUIN_THRESHOLD:
            ruin_count += 1
        if i < 40:
            sample_curves.append([round(v, 2) for v in eq.tolist()])

    sh_arr  = np.array(sharpes)
    ca_arr  = np.array(calmars)
    dd_arr  = np.array(max_dds)
    fr_arr  = np.array(final_rets)

    win_rate = sum(1 for t in trades if t > 0) / len(trades) * 100

    sh_hist, sh_edges = np.histogram(sh_arr, bins=30)
    dd_hist, dd_edges = np.histogram(dd_arr, bins=30)

    return {
        "n_runs":            n_runs,
        "n_trades":          len(trades),
        "win_rate":          round(win_rate, 1),
        "p_ruin":            round(ruin_count / n_runs * 100.0, 2),
        "sharpe_mean":       round(float(np.mean(sh_arr)),           3),
        "sharpe_std":        round(float(np.std(sh_arr)),            3),
        "sharpe_p5":         round(float(np.percentile(sh_arr,  5)), 3),
        "sharpe_p25":        round(float(np.percentile(sh_arr, 25)), 3),
        "sharpe_p75":        round(float(np.percentile(sh_arr, 75)), 3),
        "sharpe_p95":        round(float(np.percentile(sh_arr, 95)), 3),
        "calmar_mean":       round(float(np.mean(ca_arr)),           3),
        "calmar_std":        round(float(np.std(ca_arr)),            3),
        "max_dd_mean":       round(float(np.mean(dd_arr)),           2),
        "max_dd_worst":      round(float(np.max(dd_arr)),            2),
        "final_return_mean": round(float(np.mean(fr_arr)),           2),
        "final_return_std":  round(float(np.std(fr_arr)),            2),
        "equity_curves":     sample_curves,
        "sharpe_dist": {
            "counts": sh_hist.tolist(),
            "edges":  [round(e, 3) for e in sh_edges.tolist()],
        },
        "dd_dist": {
            "counts": dd_hist.tolist(),
            "edges":  [round(e, 2) for e in dd_edges.tolist()],
        },
    }


# ─────────────────────────────────────────────────────────────
# VALIDATE ONE SETUP
# ─────────────────────────────────────────────────────────────

def validate_setup(setup: dict, n_runs: int = MC_RUNS) -> dict | None:
    sid      = setup["setup_id"]
    concept  = (setup.get("concept_type") or "FVG").upper()
    market   = (setup.get("market")       or "ES").upper()
    timeframe= (setup.get("timeframe")    or "H1").upper()

    print(f"[validator] {sid[:8]} | {concept} | {market} {timeframe}")

    data = fetch_ohlcv(market, timeframe)
    if data is None:
        print(f"[validator] No data for {market} {timeframe} — skipping")
        return None

    o, h, l, c = data
    detector   = DETECTORS.get(concept, detect_fvg)
    trades     = detector(o, h, l, c)
    print(f"[validator] {len(trades)} trades detected from {len(c)} bars")

    if len(trades) < 10:
        print(f"[validator] Insufficient trades — skipping")
        return None

    print(f"[validator] Running {n_runs} MC iterations...")
    mc = run_monte_carlo(trades, n_runs)

    passed = (
        mc["sharpe_mean"] >= PASS_SHARPE and
        mc["sharpe_p5"]   >= 0.8          and
        mc["calmar_mean"] >= PASS_CALMAR  and
        mc["p_ruin"]      <= PASS_PRUIN
    )
    reasons = []
    if mc["sharpe_mean"] < PASS_SHARPE:
        reasons.append(f"Sharpe {mc['sharpe_mean']:.2f} < {PASS_SHARPE}")
    if mc["calmar_mean"] < PASS_CALMAR:
        reasons.append(f"Calmar {mc['calmar_mean']:.2f} < {PASS_CALMAR}")
    if mc["p_ruin"] > PASS_PRUIN:
        reasons.append(f"P(ruin) {mc['p_ruin']:.1f}% > {PASS_PRUIN}%")
    verdict_reason = ", ".join(reasons) if reasons else "All criteria met"

    result = {
        "id":                f"MC{sid[:12]}{int(time.time())}",
        "setup_id":          sid,
        "run_at":            datetime.utcnow().isoformat(),
        "market":            market,
        "timeframe":         timeframe,
        "concept_type":      concept,
        "n_trades":          mc["n_trades"],
        "n_runs":            mc["n_runs"],
        "sharpe_mean":       mc["sharpe_mean"],
        "sharpe_std":        mc["sharpe_std"],
        "sharpe_p5":         mc["sharpe_p5"],
        "sharpe_p95":        mc["sharpe_p95"],
        "calmar_mean":       mc["calmar_mean"],
        "calmar_std":        mc["calmar_std"],
        "max_dd_mean":       mc["max_dd_mean"],
        "max_dd_worst":      mc["max_dd_worst"],
        "win_rate":          mc["win_rate"],
        "p_ruin":            mc["p_ruin"],
        "final_return_mean": mc["final_return_mean"],
        "final_return_std":  mc["final_return_std"],
        "equity_curves_json": json.dumps(mc["equity_curves"]),
        "sharpe_dist_json":   json.dumps(mc["sharpe_dist"]),
        "dd_dist_json":       json.dumps(mc["dd_dist"]),
        "passed":            int(passed),
        "verdict_reason":    verdict_reason,
    }

    save_mc_result(result)
    update_verdict_mc(sid, passed, mc["sharpe_mean"], mc["calmar_mean"], mc["p_ruin"])

    status = "PASSED" if passed else "FAILED"
    print(
        f"[validator] {status} — "
        f"Sharpe {mc['sharpe_mean']:.2f} (p5={mc['sharpe_p5']:.2f}) | "
        f"Calmar {mc['calmar_mean']:.2f} | "
        f"P(ruin) {mc['p_ruin']:.1f}% | "
        f"WR {mc['win_rate']:.1f}%"
    )
    return result


# ─────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────

def cmd_list(min_sharpe: float = 0.0):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT mc.setup_id, mc.concept_type, mc.market, mc.timeframe,
               mc.n_trades, mc.sharpe_mean, mc.calmar_mean,
               mc.p_ruin, mc.win_rate, mc.passed, mc.run_at
        FROM mc_results mc
        WHERE mc.sharpe_mean >= ?
        ORDER BY mc.sharpe_mean DESC
    """, (min_sharpe,)).fetchall()
    conn.close()

    if not rows:
        print("No MC results. Run: python validator.py --validate-all")
        return

    header = f"{'ID':8}  {'CONCEPT':16} {'MKT':8} {'TF':6} {'TRADES':7} {'SHARPE':7} {'CALMAR':7} {'P(RUIN)':8} {'WR':6} {'PASS':5}"
    print(f"\n{header}")
    print("─" * len(header))
    for r in rows:
        p = "✓" if r["passed"] else "✗"
        print(
            f"{r['setup_id'][:8]:8}  {(r['concept_type'] or '?'):16} "
            f"{(r['market'] or '?'):8} {(r['timeframe'] or '?'):6} "
            f"{r['n_trades']:7}  {r['sharpe_mean']:6.2f}  "
            f"{r['calmar_mean']:6.2f}  {r['p_ruin']:6.1f}%  "
            f"{r['win_rate']:5.1f}%  {p:5}"
        )


def main():
    init_mc_table()
    parser = argparse.ArgumentParser(description="SentinelFX Monte Carlo Validator")
    parser.add_argument("--validate",     metavar="SETUP_ID",
                        help="Validate a specific setup_id")
    parser.add_argument("--validate-all", action="store_true",
                        help="Validate all pine_built setups not yet MC-validated")
    parser.add_argument("--list",         action="store_true",
                        help="Show all MC results")
    parser.add_argument("--min-sharpe",   type=float, default=0.0,
                        metavar="N",      help="Filter --list by min Sharpe")
    parser.add_argument("--runs",         type=int, default=MC_RUNS,
                        metavar="N",      help="MC bootstrap iterations (default 2000)")
    args = parser.parse_args()

    if args.list:
        cmd_list(args.min_sharpe)
        return

    if args.validate:
        setup = get_setup(args.validate)
        if not setup:
            print(f"Setup '{args.validate}' not found or has no critic verdict.")
            return
        validate_setup(setup, args.runs)
        return

    if args.validate_all:
        pending = get_pending_setups()
        print(f"[validator] {len(pending)} setups pending MC validation")
        for s in pending:
            validate_setup(s, args.runs)
        print("[validator] Done")
        return

    parser.print_help()


if __name__ == "__main__":
    main()
