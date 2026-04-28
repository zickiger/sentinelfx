# SentinelFX Executor Bridge — Setup Guide

## Architecture

```
TradingView Strategy
    |
    | (alert fires on signal)
    v
POST http://YOUR-IP:8080/webhook
    |
    | (validates secret, checks kill switch)
    v
executor.py
    |
    | (sizes position, places order)
    v
MetaTrader 5
    |
    v
Prop Firm Account (Topstep / FTMO / etc.)
```

---

## Step 1 — Install & Run

```bash
cd sentinelfx\executor
pip install fastapi uvicorn MetaTrader5 python-dotenv
copy .env.example .env
# Edit .env with your MT5 credentials and webhook secret
python executor.py
```

Server starts on port 8080.
Test it: http://localhost:8080/health

---

## Step 2 — Expose to the Internet (for TradingView)

TradingView needs to reach your server from the internet.
Two options:

**Option A — ngrok (easiest for local testing, free)**
```bash
# Download ngrok from https://ngrok.com
ngrok http 8080
# Gives you a public URL like: https://abc123.ngrok.io
```

**Option B — Deploy to Railway (production)**
- Add executor/ folder to your Railway project
- Set all .env variables in Railway Variables tab
- Railway gives you a public URL automatically

---

## Step 3 — Set up TradingView Alert

1. Open your generated Pine Script in TradingView
2. Add it to a chart
3. Click the "Alerts" clock icon -> Create Alert
4. Set condition to your strategy
5. Under "Notifications" -> check "Webhook URL"
6. Enter your URL: `https://YOUR-URL/webhook`
7. In the "Message" box paste this JSON exactly:

```json
{
  "secret":    "your_webhook_secret_here",
  "strategy":  "{{strategy.order.comment}}",
  "action":    "{{strategy.order.action}}",
  "symbol":    "{{ticker}}",
  "contracts": 1,
  "price":     {{close}},
  "sl":        0,
  "tp":        0
}
```

Replace `your_webhook_secret_here` with your WEBHOOK_SECRET from .env.

The `{{}}` values are TradingView variables filled in automatically when the alert fires.

Setting `sl` and `tp` to 0 tells the executor to calculate them automatically
(0.3% SL, 2R TP by default). Or set them explicitly from Pine Script:

```json
{
  "secret":    "your_secret",
  "strategy":  "{{strategy.order.comment}}",
  "action":    "{{strategy.order.action}}",
  "symbol":    "{{ticker}}",
  "contracts": 1,
  "price":     {{close}},
  "sl":        {{strategy.position_avg_price}},
  "tp":        {{strategy.order.contracts}}
}
```

---

## Step 4 — Test Without TradingView

Use the manual trade endpoint to test the full pipeline:

```bash
curl -X POST http://localhost:8080/trade/manual \
  -H "Content-Type: application/json" \
  -d '{
    "strategy": "test",
    "action":   "buy",
    "symbol":   "EURUSD",
    "contracts": 1,
    "price":    1.0850,
    "sl":       1.0820,
    "tp":       1.0910
  }'
```

Or open http://localhost:8080/docs for the interactive Swagger UI.

---

## API Endpoints

| Method | Endpoint              | Description                    |
|--------|-----------------------|--------------------------------|
| GET    | /health               | System status, DD, kill switch |
| GET    | /account              | MT5 account info               |
| GET    | /positions            | Open positions                 |
| GET    | /trades               | All trade history              |
| GET    | /trades/today         | Today's trades + stats         |
| POST   | /webhook              | TradingView alert receiver     |
| POST   | /trade/manual         | Place trade manually           |
| POST   | /kill-switch/toggle   | Arm / disarm kill switch       |
| POST   | /kill-switch/reset    | Reset after trip               |

---

## Kill Switch Behavior

The kill switch fires automatically when:
- Daily drawdown reaches DD_LIMIT_PCT (default 4.5%)

When tripped:
1. All open positions are closed immediately
2. All incoming webhook alerts are blocked
3. Entry logged to kill_switch_log table
4. Manual reset required: POST /kill-switch/reset

---

## Prop Firm Compliance

| Rule               | How it's enforced                          |
|--------------------|---------------------------------------------|
| Daily DD limit     | Kill switch at DD_LIMIT_PCT                 |
| Max position size  | Hard cap at MAX_CONTRACTS                   |
| Risk per trade     | Auto-sizing based on RISK_PER_TRADE %       |
| Anti-detection     | ±1-2 tick entry randomisation               |
| Consistency rule   | Track gross_pnl per day in daily_stats DB   |

---

## MT5 Note

MetaTrader5 Python library only works on **Windows** with MT5 installed.
If you're on Mac/Linux or don't have MT5 installed, the executor runs in
**simulation mode** — all trades are logged but not actually placed.

For prop firms that use MT5 (Topstep, FTMO, The5%ers, MyForexFunds):
1. Download and install MT5 from your prop firm's portal
2. Log into your funded account in MT5
3. Set MT5_LOGIN, MT5_PASSWORD, MT5_SERVER in your .env

---

## Simulation Mode

If MT5 is not configured, every trade prints:
```
[SIM] BUY 1x EURUSD @ 1.0850 SL=1.0820 TP=1.0910 ticket=123456
```

All trades still save to executor.db — you can review them with:
```bash
# Windows
python -c "import sqlite3; conn=sqlite3.connect('data/executor.db'); [print(r) for r in conn.execute('SELECT * FROM trades ORDER BY ts DESC LIMIT 10')]"
```
