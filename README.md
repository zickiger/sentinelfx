# SentinelFX — Multi-Agent Algorithmic Trading System

> Research Sentinel · Architect Agent · Validation Engine · Executor Bridge

---

## Repository Structure

```
sentinelfx/
├── frontend/               ← Vercel (static HTML portal)
│   ├── index.html          ← Main portal UI
│   └── config.js           ← Swap your Railway URL here
│
├── backend/                ← Railway (FastAPI + WebSocket server)
│   ├── main.py             ← All API routes + WebSocket feed
│   ├── requirements.txt
│   ├── Dockerfile
│   └── .env.example        ← Copy to .env, never commit .env
│
├── research_sentinel/      ← Run locally or as Railway cron
│   ├── research_sentinel.py
│   ├── signal_mapper.py
│   └── sentinel_config.yaml
│
├── vercel.json             ← Vercel deployment config
├── railway.toml            ← Railway deployment config
└── .gitignore
```

---

## Setup: GitHub

```bash
# 1. Create a new repo on github.com, then:
git clone https://github.com/YOUR_USERNAME/sentinelfx.git
cd sentinelfx

# 2. Copy all project files into it (match structure above)

# 3. Initial commit
git add .
git commit -m "feat: initial SentinelFX system"
git push origin main
```

---

## Deploy: Railway (Backend)

Railway hosts the FastAPI server with WebSocket support.

### Steps

1. Go to [railway.app](https://railway.app) → **New Project** → **Deploy from GitHub repo**
2. Select your `sentinelfx` repo
3. Railway auto-detects the `Dockerfile` in `/backend`
4. Go to **Variables** tab and add every key from `backend/.env.example`:

```
SENTINEL_DB        /app/data/alpha_queue.db
PROP_FIRM          Topstep SIM
WEBHOOK_SECRET     your_random_secret_here
OPENAI_API_KEY     sk-...
REDDIT_CLIENT_ID   ...
REDDIT_CLIENT_SECRET ...
ALLOWED_ORIGIN     https://YOUR-APP.vercel.app
```

5. Click **Deploy** — Railway builds the Docker image and gives you a URL like:
   `https://sentinelfx-production.up.railway.app`

6. Test it: `curl https://sentinelfx-production.up.railway.app/api/health`

---

## Deploy: Vercel (Frontend)

Vercel hosts the static HTML portal.

### Steps

1. Go to [vercel.com](https://vercel.com) → **Add New Project** → Import your GitHub repo
2. **Framework Preset**: Other
3. **Root Directory**: leave blank (vercel.json handles routing)
4. **Build Command**: leave blank (static files, no build step)
5. **Output Directory**: `frontend`
6. Click **Deploy**

### After Railway gives you a URL

Edit `frontend/config.js`:
```js
BACKEND_URL: "https://sentinelfx-production.up.railway.app",
```

Commit and push — Vercel redeploys in ~10 seconds.

---

## Local Development

```bash
# Terminal 1 — backend
cd backend
pip install -r requirements.txt
cp .env.example .env        # fill in your keys
uvicorn main:app --reload --port 8000

# Terminal 2 — frontend
# Just open frontend/index.html in your browser
# OR use VS Code Live Server extension
# config.js already has localhost:8000 as fallback comment
```

To switch `config.js` to local dev temporarily:
```js
BACKEND_URL: "http://localhost:8000",
```

---

## TradingView Webhook Setup

Once Railway is live, point your TradingView alerts at:
```
POST https://sentinelfx-production.up.railway.app/api/webhook/alert
```

Alert message JSON:
```json
{
  "strategy": "SMC_OB_v4",
  "action":   "{{strategy.order.action}}",
  "symbol":   "{{ticker}}",
  "contracts": 1,
  "price":    {{close}},
  "sl":       0,
  "tp":       0,
  "secret":   "your_random_secret_here"
}
```

The `secret` must match `WEBHOOK_SECRET` in your Railway environment variables.

---

## Environment Variables Reference

| Variable | Where | Description |
|---|---|---|
| `SENTINEL_DB` | Railway | Path to SQLite DB |
| `PROP_FIRM` | Railway | Label shown in portal |
| `WEBHOOK_SECRET` | Railway + TradingView | Alert auth token |
| `OPENAI_API_KEY` | Railway | NLP signal extraction |
| `REDDIT_CLIENT_ID` | Railway | Reddit scraper |
| `REDDIT_CLIENT_SECRET` | Railway | Reddit scraper |
| `ALLOWED_ORIGIN` | Railway | Your Vercel domain for CORS |
| `BACKEND_URL` | `frontend/config.js` | Your Railway public URL |

---

## CORS Note

In `backend/main.py`, the `ALLOWED_ORIGIN` env var controls which domains
can call the API. In production set it to your exact Vercel URL:

```
ALLOWED_ORIGIN=https://sentinelfx.vercel.app
```

During development `*` (allow all) is fine.
