/**
 * SentinelFX — Frontend Runtime Config
 *
 * After Railway deploys your backend:
 *   1. Copy your Railway public URL (e.g. https://sentinelfx-production.up.railway.app)
 *   2. Replace RAILWAY_URL below
 *   3. Commit + push — Vercel redeploys automatically
 *
 * For local dev, run:
 *   uvicorn main:app --reload   (in /backend)
 *   open frontend/index.html    (or use Live Server)
 */

const SENTINELFX_CONFIG = {
  // Auto-selects localhost for local dev, Railway URL for production.
  // After Railway deploy: replace the string below with your Railway URL.
  BACKEND_URL: (window.location.hostname === "localhost" || window.location.hostname === "127.0.0.1")
    ? "http://localhost:8000"
    : "https://YOUR-APP.up.railway.app",

  // Automatically uses ws:// for http:// and wss:// for https://
  get WS_URL() {
    return this.BACKEND_URL
      .replace(/^https/, "wss")
      .replace(/^http/,  "ws")
      + "/ws/live";
  },
};

// Freeze so nothing accidentally mutates it
Object.freeze(SENTINELFX_CONFIG);
