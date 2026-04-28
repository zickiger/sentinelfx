# SentinelFX

Multi-agent algorithmic trading system. Scrapes alpha signals → AI critic scores them → generates Pine Script strategies → backtests → executes via MetaTrader 5.

## Architecture

```
architect/setup_hunter.py   — scraper + Ollama AI critic + Pine Script generator
backend/main.py             — FastAPI hub (port 8000): signals API, WebSocket, risk config
executor/executor.py        — FastAPI executor bridge (port 8080): MT5 orders, kill-switch
frontend/index.html         — single-file portal (deployed to Vercel)
frontend/config.js          — backend URL config (auto-detects localhost vs production)
```

### Data flow
1. `setup_hunter.py --scan` → writes `architect/data/setups.db`
2. Backend reads `setups.db` → serves `/api/signals`, `/api/strategies`
3. TradingView alert → `POST /api/webhook/alert` on backend → forwarded to executor `:8080/webhook`
4. Executor validates secret, checks kill-switch, sizes position, places MT5 order
5. Portal WebSocket (`/ws/live`) streams live agent state + market quotes

## Commands

```bash
# Start everything locally
start.bat

# Individual services
cd backend  && uvicorn main:app --reload --port 8000
cd executor && python executor.py          # port 8080
ollama serve                               # required for setup_hunter critic

# Run a research scan
cd architect && python setup_hunter.py --scan
cd architect && python setup_hunter.py --scan --source reddit
cd architect && python setup_hunter.py --list
```

## Environment variables

- `backend/.env` — copy from `backend/.env.example`; set `WEBHOOK_SECRET`, `ALLOWED_ORIGIN`
- `executor/.env` — copy from `executor/.env.example`; set `WEBHOOK_SECRET` (must match backend), `MT5_LOGIN`, `MT5_PASSWORD`, `MT5_SERVER`
- Both secrets must be identical or TradingView alerts will be rejected

## Key connections

- Backend → Executor: `POST {EXECUTOR_URL}/webhook` (forwarding TV alerts)
- Backend → Executor: `POST {EXECUTOR_URL}/risk/config` (syncing DD limits)
- Backend reads DB: `SENTINEL_DB=../architect/data/setups.db`
- Frontend: update `frontend/config.js` Railway URL before production deploy

## Deployment

- Backend + Executor → Railway (uses `backend/Dockerfile`)
- Frontend → Vercel (static `frontend/` folder)
- After Railway deploy: update `BACKEND_URL` in `frontend/config.js` and push

## Conventions

- Python 3.10+, FastAPI, SQLite (no ORM)
- Pine Script v6 for all generated strategies
- All monetary values in USD; DD percentages are of account balance
- `WEBHOOK_SECRET` must be set before any live trading
