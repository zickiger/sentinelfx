"""
╔══════════════════════════════════════════════════════════════════╗
║   SENTINEL FX — SETUP HUNTER + CRITIC AGENT  v1.0               ║
║                                                                  ║
║   Pipeline:                                                      ║
║   1. Scan Reddit, Twitter/X, TradingView, YouTube                ║
║   2. Hyper-critical Ollama AI tears each setup apart             ║
║   3. Score 0-100. Pass = 65+                                     ║
║   4. Auto-generate Pine Script for passing setups                ║
║   5. Auto-backtest via vectorbt in Python                        ║
╚══════════════════════════════════════════════════════════════════╝

Install:
    pip install aiohttp beautifulsoup4 praw rich pyyaml \
                yfinance pandas numpy vectorbt

Run:
    python setup_hunter.py --scan              # Full scan all sources
    python setup_hunter.py --scan --source reddit
    python setup_hunter.py --scan --source tradingview
    python setup_hunter.py --review           # Re-run critic on stored setups
    python setup_hunter.py --list             # Show scored setups
    python setup_hunter.py --build <id>       # Manually trigger build
"""

import asyncio
import aiohttp
import sqlite3
import json
import re
import hashlib
import argparse
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

# praw removed — using Reddit public JSON API
import yfinance as yf
import pandas as pd
import numpy as np
from bs4 import BeautifulSoup
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn

console = Console()

# ─────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────

DB_PATH         = Path("data/setups.db")
PINE_OUT        = Path("pine_scripts")
BACKTEST_OUT    = Path("backtest_results")
OLLAMA_URL      = "http://localhost:11434/api/generate"
OLLAMA_MODEL    = "llama3.1"
CONFIDENCE_GATE = 65        # minimum score to build + backtest
SCAN_LIMIT      = 30        # posts per source per run

REDDIT_SUBS = [
    "Daytrading", "Futures", "FuturesTrading",
    "Forex", "SMCtrading", "AlgoTrading", "RealDayTrading"
]

TRADINGVIEW_SEARCH_TERMS = [
    "order block setup", "fair value gap trade",
    "liquidity sweep entry", "SMC setup today",
    "BOS CHOCH trade", "inducement sweep"
]

YOUTUBE_CHANNELS = [
    "ICT", "SMC concepts", "smart money trading setups"
]

# ─────────────────────────────────────────────────────────────
# DATA MODELS
# ─────────────────────────────────────────────────────────────

@dataclass
class RawSetup:
    setup_id:       str  = ""
    source:         str  = ""       # reddit | tradingview | twitter | youtube
    title:          str  = ""
    body:           str  = ""
    url:            str  = ""
    author:         str  = ""
    engagement:     int  = 0        # upvotes / likes / views
    scraped_at:     str  = field(default_factory=lambda: datetime.utcnow().isoformat())

@dataclass
class CriticVerdict:
    setup_id:           str   = ""
    confidence:         int   = 0       # 0-100
    verdict:            str   = ""      # PASS | FAIL | REVIEW
    concept_type:       str   = ""      # ORDER_BLOCK | FVG | LIQUIDITY_SWEEP | BOS_CHOCH
    timeframe:          str   = ""
    market:             str   = ""
    entry_logic:        str   = ""
    sl_logic:           str   = ""
    tp_logic:           str   = ""
    strengths:          str   = ""
    weaknesses:         str   = ""
    red_flags:          str   = ""
    pine_viable:        bool  = False
    critic_reasoning:   str   = ""
    scored_at:          str   = field(default_factory=lambda: datetime.utcnow().isoformat())

# ─────────────────────────────────────────────────────────────
# DATABASE
# ─────────────────────────────────────────────────────────────

class SetupDB:
    def __init__(self):
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        self._init()

    def _init(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS raw_setups (
                setup_id    TEXT PRIMARY KEY,
                source      TEXT,
                title       TEXT,
                body        TEXT,
                url         TEXT,
                author      TEXT,
                engagement  INTEGER,
                scraped_at  TEXT
            );
            CREATE TABLE IF NOT EXISTS critic_verdicts (
                setup_id         TEXT PRIMARY KEY,
                confidence       INTEGER,
                verdict          TEXT,
                concept_type     TEXT,
                timeframe        TEXT,
                market           TEXT,
                entry_logic      TEXT,
                sl_logic         TEXT,
                tp_logic         TEXT,
                strengths        TEXT,
                weaknesses       TEXT,
                red_flags        TEXT,
                pine_viable      INTEGER,
                critic_reasoning TEXT,
                scored_at        TEXT,
                pine_built       INTEGER DEFAULT 0,
                backtest_run     INTEGER DEFAULT 0,
                backtest_sharpe  REAL,
                backtest_winrate REAL,
                backtest_result  TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_confidence
                ON critic_verdicts(confidence DESC);
            CREATE TABLE IF NOT EXISTS seen_urls (
                url_hash TEXT PRIMARY KEY,
                url      TEXT,
                seen_at  TEXT
            );
        """)
        self.conn.commit()

    def exists(self, setup_id: str) -> bool:
        cur = self.conn.execute(
            "SELECT 1 FROM raw_setups WHERE setup_id=?", (setup_id,))
        return cur.fetchone() is not None

    def url_seen(self, url: str) -> bool:
        """Check if a URL has ever been scraped before."""
        h = hashlib.md5(url.encode()).hexdigest()
        cur = self.conn.execute("SELECT 1 FROM seen_urls WHERE url_hash=?", (h,))
        return cur.fetchone() is not None

    def mark_url_seen(self, url: str):
        """Permanently record a URL so it is never scraped again."""
        h = hashlib.md5(url.encode()).hexdigest()
        self.conn.execute(
            "INSERT OR IGNORE INTO seen_urls VALUES (?,?,?)",
            (h, url, datetime.utcnow().isoformat())
        )
        self.conn.commit()

    def save_setup(self, s: RawSetup):
        self.conn.execute("""
            INSERT OR IGNORE INTO raw_setups
            VALUES (?,?,?,?,?,?,?,?)
        """, (s.setup_id, s.source, s.title, s.body,
              s.url, s.author, s.engagement, s.scraped_at))
        self.conn.commit()
        self.mark_url_seen(s.url)
        self.mark_url_seen(s.setup_id)

    def save_verdict(self, v: CriticVerdict):
        self.conn.execute("""
            INSERT OR REPLACE INTO critic_verdicts
            (setup_id,confidence,verdict,concept_type,timeframe,market,
             entry_logic,sl_logic,tp_logic,strengths,weaknesses,red_flags,
             pine_viable,critic_reasoning,scored_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (v.setup_id, v.confidence, v.verdict, v.concept_type,
              v.timeframe, v.market, v.entry_logic, v.sl_logic, v.tp_logic,
              v.strengths, v.weaknesses, v.red_flags, int(v.pine_viable),
              v.critic_reasoning, v.scored_at))
        self.conn.commit()

    def get_passing(self, min_confidence: int = CONFIDENCE_GATE) -> list[dict]:
        cur = self.conn.execute("""
            SELECT r.*, v.* FROM raw_setups r
            JOIN critic_verdicts v ON r.setup_id = v.setup_id
            WHERE v.confidence >= ? AND v.pine_viable = 1
            ORDER BY v.confidence DESC
        """, (min_confidence,))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def get_all_verdicts(self) -> list[dict]:
        cur = self.conn.execute("""
            SELECT r.title, r.source, r.author, r.engagement,
                   v.confidence, v.verdict, v.concept_type,
                   v.timeframe, v.market, v.pine_built,
                   v.backtest_run, v.backtest_sharpe, v.setup_id
            FROM raw_setups r
            JOIN critic_verdicts v ON r.setup_id = v.setup_id
            ORDER BY v.confidence DESC
        """)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def get_unscored(self) -> list[dict]:
        cur = self.conn.execute("""
            SELECT * FROM raw_setups
            WHERE setup_id NOT IN (SELECT setup_id FROM critic_verdicts)
        """)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def mark_pine_built(self, setup_id: str):
        self.conn.execute(
            "UPDATE critic_verdicts SET pine_built=1 WHERE setup_id=?",
            (setup_id,))
        self.conn.commit()

    def save_backtest(self, setup_id: str, sharpe: float,
                      winrate: float, result_json: str):
        self.conn.execute("""
            UPDATE critic_verdicts
            SET backtest_run=1, backtest_sharpe=?, backtest_winrate=?, backtest_result=?
            WHERE setup_id=?
        """, (sharpe, winrate, result_json, setup_id))
        self.conn.commit()


db = SetupDB()

# ─────────────────────────────────────────────────────────────
# SCRAPERS
# ─────────────────────────────────────────────────────────────

class RedditScanner:
    """
    Scrapes Reddit using the public JSON API — no credentials needed.
    Uses Reddit's .json endpoint which is free and open.
    """
    KEYWORDS = [
        "order block", "fair value gap", "fvg", "liquidity sweep",
        "bos", "choch", "smc", "smart money", "inducement",
        "ob entry", "sweep reversal", "imbalance", "market structure"
    ]
    HEADERS = {"User-Agent": "SentinelFX/1.0 (research bot)"}

    async def scan(self) -> list[RawSetup]:
        setups = []
        async with aiohttp.ClientSession(headers=self.HEADERS) as session:
            for sub_name in REDDIT_SUBS:
                try:
                    url = f"https://www.reddit.com/r/{sub_name}/hot.json?limit={SCAN_LIMIT}"
                    async with session.get(
                        url, timeout=aiohttp.ClientTimeout(total=15)
                    ) as r:
                        if r.status == 429:
                            console.print(f"[dim]Reddit r/{sub_name}: rate limited, skipping[/dim]")
                            await asyncio.sleep(2)
                            continue
                        if r.status != 200:
                            console.print(f"[dim]Reddit r/{sub_name}: HTTP {r.status}[/dim]")
                            continue

                        data = await r.json()
                        posts = data.get("data", {}).get("children", [])

                        for post_wrap in posts:
                            post     = post_wrap.get("data", {})
                            title    = post.get("title", "")
                            selftext = post.get("selftext", "")
                            score    = post.get("score", 0)
                            pid      = post.get("id", "")
                            author   = post.get("author", "")
                            permalink= post.get("permalink", "")

                            if score < 10:
                                continue
                            text = f"{title} {selftext}".lower()
                            if not any(kw in text for kw in self.KEYWORDS):
                                continue

                            sid = hashlib.md5(pid.encode()).hexdigest()
                            if db.exists(sid):
                                continue

                            setups.append(RawSetup(
                                setup_id=sid,
                                source="reddit",
                                title=title,
                                body=selftext[:1500],
                                url=f"https://reddit.com{permalink}",
                                author=author,
                                engagement=score,
                            ))

                    await asyncio.sleep(1)

                except Exception as e:
                    console.print(f"[dim]Reddit r/{sub_name} error: {e}[/dim]")

        console.print(f"[red]Reddit[/red] → {len(setups)} new setups")
        return setups


class TradingViewScanner:
    """Scrapes TradingView public ideas for SMC setups."""

    BASE = "https://www.tradingview.com/ideas/"
    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/120.0.0.0 Safari/537.36"
    }
    KEYWORDS = [
        "order block", "fair value gap", "fvg", "liquidity",
        "smart money", "smc", "bos", "choch", "sweep", "imbalance"
    ]

    async def scan(self) -> list[RawSetup]:
        setups = []
        async with aiohttp.ClientSession(headers=self.HEADERS) as session:
            for term in TRADINGVIEW_SEARCH_TERMS:
                try:
                    url = f"https://www.tradingview.com/ideas/search/{term.replace(' ','-')}/"
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                        if r.status != 200:
                            continue
                        html = await r.text()
                        soup = BeautifulSoup(html, "html.parser")

                        # TradingView idea cards
                        cards = soup.select("article") or soup.select("[class*='card']")
                        for card in cards[:10]:
                            title_el = card.select_one("h2, h3, [class*='title']")
                            desc_el  = card.select_one("p, [class*='desc']")
                            link_el  = card.select_one("a[href]")

                            if not title_el:
                                continue

                            title = title_el.get_text(strip=True)
                            body  = desc_el.get_text(strip=True) if desc_el else ""
                            href  = link_el["href"] if link_el else ""
                            url_full = f"https://www.tradingview.com{href}" if href.startswith("/") else href

                            text = f"{title} {body}".lower()
                            if not any(kw in text for kw in self.KEYWORDS):
                                continue

                            sid = hashlib.md5(url_full.encode()).hexdigest()
                            if db.exists(sid):
                                continue

                            setups.append(RawSetup(
                                setup_id=sid,
                                source="tradingview",
                                title=title,
                                body=body[:1500],
                                url=url_full,
                                author="TradingView",
                                engagement=0,
                            ))
                except Exception as e:
                    console.print(f"[dim]TradingView search error '{term}': {e}[/dim]")

        console.print(f"[blue]TradingView[/blue] → {len(setups)} new setups")
        return setups


class StockTwitsScanner:
    """Scrapes StockTwits for real-time SMC trade ideas."""
    HEADERS  = {"User-Agent": "SentinelFX/1.0"}
    SYMBOLS  = ["ES", "NQ", "GC", "CL", "EURUSD", "SPY", "QQQ", "BTCUSD"]
    KEYWORDS = [
        "order block", "fair value gap", "fvg", "liquidity sweep",
        "bos", "choch", "smc", "smart money", "ob entry", "imbalance",
        "sweep", "inducement", "market structure", "break of structure"
    ]

    async def scan(self) -> list[RawSetup]:
        setups = []
        async with aiohttp.ClientSession(headers=self.HEADERS) as session:
            for symbol in self.SYMBOLS:
                try:
                    url = f"https://api.stocktwits.com/api/2/streams/symbol/{symbol}.json"
                    async with session.get(
                        url, timeout=aiohttp.ClientTimeout(total=12)
                    ) as r:
                        if r.status != 200:
                            continue
                        data = await r.json()
                        messages = data.get("messages", [])

                        for msg in messages:
                            body    = msg.get("body", "")
                            user    = msg.get("user", {}).get("username", "")
                            mid     = str(msg.get("id", ""))
                            created = msg.get("created_at", "")
                            likes   = msg.get("likes", {}).get("total", 0)

                            if likes < 3:
                                continue
                            if not any(kw in body.lower() for kw in self.KEYWORDS):
                                continue

                            sid = hashlib.md5(mid.encode()).hexdigest()
                            if db.exists(sid) or db.url_seen(f"stocktwits_{mid}"):
                                continue

                            setups.append(RawSetup(
                                setup_id=sid,
                                source="stocktwits",
                                title=f"{symbol}: {body[:80]}",
                                body=body[:1500],
                                url=f"https://stocktwits.com/{user}/message/{mid}",
                                author=user,
                                engagement=likes,
                            ))

                    await asyncio.sleep(0.5)
                except Exception as e:
                    console.print(f"[dim]StockTwits {symbol} error: {e}[/dim]")

        console.print(f"[green]StockTwits[/green] → {len(setups)} new setups")
        return setups


class BabypipsScanner:
    """Scrapes Babypips forums for forex SMC setups."""
    BASE     = "https://forums.babypips.com"
    HEADERS  = {"User-Agent": "SentinelFX/1.0"}
    SECTIONS = [
        "/forex-trading/trading-systems-and-strategies",
        "/forex-trading/trading-discussion",
        "/forex-trading/technical-analysis",
    ]
    KEYWORDS = [
        "order block", "fair value gap", "fvg", "smart money",
        "liquidity", "smc", "bos", "choch", "sweep", "imbalance",
        "inducement", "market structure", "ob", "premium", "discount"
    ]

    async def scan(self) -> list[RawSetup]:
        setups = []
        async with aiohttp.ClientSession(headers=self.HEADERS) as session:
            for section in self.SECTIONS:
                try:
                    url = f"{self.BASE}{section}.json"
                    async with session.get(
                        url, timeout=aiohttp.ClientTimeout(total=15)
                    ) as r:
                        if r.status != 200:
                            continue
                        data = await r.json()
                        topics = data.get("topic_list", {}).get("topics", [])

                        for topic in topics[:20]:
                            title  = topic.get("title", "")
                            tid    = str(topic.get("id", ""))
                            posts  = topic.get("posts_count", 0)
                            views  = topic.get("views", 0)
                            slug   = topic.get("slug", "")

                            if not any(kw in title.lower() for kw in self.KEYWORDS):
                                continue
                            if posts < 2:
                                continue

                            topic_url = f"{self.BASE}/t/{slug}/{tid}"
                            sid = hashlib.md5(tid.encode()).hexdigest()
                            if db.exists(sid) or db.url_seen(topic_url):
                                continue

                            # Fetch first post body
                            body = await self._fetch_first_post(session, tid)

                            setups.append(RawSetup(
                                setup_id=sid,
                                source="babypips",
                                title=title,
                                body=body[:1500],
                                url=topic_url,
                                author="babypips",
                                engagement=views,
                            ))

                    await asyncio.sleep(1)
                except Exception as e:
                    console.print(f"[dim]Babypips {section} error: {e}[/dim]")

        console.print(f"[cyan]Babypips[/cyan] → {len(setups)} new setups")
        return setups

    async def _fetch_first_post(self, session, topic_id: str) -> str:
        try:
            url = f"{self.BASE}/t/{topic_id}/posts.json?post_ids[]=1"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 200:
                    data = await r.json()
                    posts = data.get("post_stream", {}).get("posts", [])
                    if posts:
                        raw = posts[0].get("cooked", "")
                        return BeautifulSoup(raw, "html.parser").get_text(strip=True)
        except Exception:
            pass
        return ""


class TradingViewIdeasScanner:
    """Scrapes TradingView public ideas — structured, chart-backed setups."""
    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json",
    }
    KEYWORDS = [
        "order block", "fair value gap", "fvg", "liquidity",
        "smart money", "smc", "bos", "choch", "sweep", "imbalance",
        "ob entry", "premium", "discount", "inducement"
    ]
    SEARCH_TERMS = [
        "order+block", "fair+value+gap", "liquidity+sweep",
        "smart+money+concepts", "BOS+CHOCH", "smc+setup"
    ]

    async def scan(self) -> list[RawSetup]:
        setups = []
        async with aiohttp.ClientSession(headers=self.HEADERS) as session:
            for term in self.SEARCH_TERMS:
                try:
                    # TradingView public ideas search
                    url = f"https://www.tradingview.com/ideas/search/{term}/"
                    async with session.get(
                        url, timeout=aiohttp.ClientTimeout(total=15)
                    ) as r:
                        if r.status != 200:
                            continue
                        html = await r.text()
                        soup = BeautifulSoup(html, "html.parser")

                        for card in soup.select("article, [class*='idea-card'], [class*='card']")[:12]:
                            title_el = card.select_one("h2, h3, [class*='title']")
                            desc_el  = card.select_one("p, [class*='desc'], [class*='body']")
                            link_el  = card.select_one("a[href*='/p/'], a[href*='/ideas/']")
                            like_el  = card.select_one("[class*='like'], [class*='boost']")

                            if not title_el:
                                continue

                            title = title_el.get_text(strip=True)
                            body  = desc_el.get_text(strip=True) if desc_el else ""
                            href  = link_el["href"] if link_el else ""
                            likes = 0
                            if like_el:
                                try: likes = int(re.sub(r"[^0-9]","",like_el.get_text()))
                                except: pass

                            full_url = (f"https://www.tradingview.com{href}"
                                       if href.startswith("/") else href)

                            if not any(kw in f"{title} {body}".lower() for kw in self.KEYWORDS):
                                continue

                            sid = hashlib.md5(full_url.encode()).hexdigest()
                            if db.exists(sid) or db.url_seen(full_url):
                                continue

                            setups.append(RawSetup(
                                setup_id=sid,
                                source="tradingview",
                                title=title,
                                body=body[:1500],
                                url=full_url,
                                author="TradingView",
                                engagement=likes,
                            ))

                    await asyncio.sleep(1)
                except Exception as e:
                    console.print(f"[dim]TradingView '{term}' error: {e}[/dim]")

        console.print(f"[blue]TradingView[/blue] → {len(setups)} new setups")
        return setups


class TwitterScanner:
    """
    Scrapes Twitter/X public search without API key.
    Uses Nitter (open-source Twitter frontend) as proxy.
    Falls back gracefully if unavailable.
    """

    NITTER_INSTANCES = [
        "https://nitter.net",
        "https://nitter.privacydev.net",
        "https://nitter.poast.org",
    ]
    QUERIES = [
        "SMC order block setup",
        "fair value gap entry",
        "liquidity sweep short",
        "BOS CHOCH trade",
        "smart money concepts live",
    ]
    HEADERS = {"User-Agent": "Mozilla/5.0"}

    async def scan(self) -> list[RawSetup]:
        setups = []
        async with aiohttp.ClientSession(headers=self.HEADERS) as session:
            for instance in self.NITTER_INSTANCES:
                try:
                    # Test instance is alive
                    async with session.get(
                        instance, timeout=aiohttp.ClientTimeout(total=5)
                    ) as r:
                        if r.status != 200:
                            continue

                    # Search each query
                    for query in self.QUERIES:
                        url = f"{instance}/search?q={query.replace(' ','+')}&f=tweets"
                        async with session.get(
                            url, timeout=aiohttp.ClientTimeout(total=12)
                        ) as r:
                            if r.status != 200:
                                continue
                            html = await r.text()
                            soup = BeautifulSoup(html, "html.parser")

                            tweets = soup.select(".timeline-item")
                            for tweet in tweets[:8]:
                                content_el = tweet.select_one(".tweet-content")
                                author_el  = tweet.select_one(".username")
                                stats_el   = tweet.select_one(".tweet-stats")
                                link_el    = tweet.select_one("a.tweet-link")

                                if not content_el:
                                    continue

                                text   = content_el.get_text(strip=True)
                                author = author_el.get_text(strip=True) if author_el else "unknown"
                                href   = link_el["href"] if link_el else ""
                                url_t  = f"https://twitter.com{href}" if href else ""

                                # Parse engagement (likes)
                                likes = 0
                                if stats_el:
                                    like_el = stats_el.select_one("[class*='like']")
                                    if like_el:
                                        try: likes = int(like_el.get_text(strip=True).replace(",",""))
                                        except: pass

                                if likes < 20:
                                    continue

                                sid = hashlib.md5(f"{author}{text[:50]}".encode()).hexdigest()
                                if db.exists(sid):
                                    continue

                                setups.append(RawSetup(
                                    setup_id=sid,
                                    source="twitter",
                                    title=text[:100],
                                    body=text[:1500],
                                    url=url_t,
                                    author=author,
                                    engagement=likes,
                                ))

                    # If we got results from this instance, stop trying others
                    if setups:
                        break

                except Exception as e:
                    console.print(f"[dim]Nitter {instance} failed: {e}[/dim]")
                    continue

        console.print(f"[yellow]Twitter/X[/yellow] → {len(setups)} new setups")
        return setups


# ─────────────────────────────────────────────────────────────
# HYPER-CRITICAL AI CRITIC
# ─────────────────────────────────────────────────────────────

CRITIC_PROMPT = """You are an elite, hyper-critical quantitative trading analyst with 20 years experience.
You have seen thousands of retail traders blow up their accounts posting garbage setups online.
Your job is to be brutally honest — most setups posted online are noise, not edge.

You will analyze a trade setup posted on social media or a financial platform.
Be HARSH. Most setups should score below 65. Only genuinely well-defined setups with clear edge pass.

Evaluate on these criteria:
1. CONCEPT CLARITY: Is the setup based on a real, defined concept (OB, FVG, sweep, BOS/CHOCH)?
2. ENTRY PRECISION: Is the exact entry trigger clearly defined? Or is it vague ("price might go up")?
3. RISK DEFINITION: Is stop loss placement logical and specific? Or is it missing/arbitrary?
4. REWARD LOGIC: Is the take profit based on structure/levels? Or just "moon"?
5. CONTEXT: Does it account for higher timeframe bias? Session timing? Market conditions?
6. ORIGINALITY: Is this a genuine observation or recycled generic advice?
7. EXECUTION VIABILITY: Could this actually be coded into an algorithm?
8. RED FLAGS: Signs of pump-and-dump, FOMO bait, vagueness, fake guru nonsense

Return ONLY valid JSON, no markdown, no explanation:
{{
  "confidence": 0,
  "concept_type": "ORDER_BLOCK|FVG|LIQUIDITY_SWEEP|BOS_CHOCH|WYCKOFF|OTHER|NONE",
  "timeframe": "M5|M15|M30|H1|H4|D1|W1|unspecified",
  "market": "instrument name or 'unspecified'",
  "entry_logic": "precise entry trigger or 'undefined'",
  "sl_logic": "stop loss logic or 'undefined'",
  "tp_logic": "take profit logic or 'undefined'",
  "strengths": "what is genuinely good about this setup",
  "weaknesses": "what is missing or weak",
  "red_flags": "any signs this is garbage, fake, or dangerous",
  "pine_viable": true,
  "critic_reasoning": "your full critical reasoning in 2-3 sentences"
}}

confidence: 0-100 integer
  0-30:  Garbage. Vague, dangerous, or fake.
  31-50: Weak. Has a concept but missing critical definition.
  51-64: Mediocre. Identifiable setup but not precise enough to trade.
  65-79: Decent. Clear concept, defined risk, executable.
  80-89: Good. Well-defined edge with proper context.
  90-100: Exceptional. Rare. Near-perfect setup definition.

pine_viable: true only if entry, SL, AND TP are clearly defined enough to code.

Setup to analyze:
SOURCE: {source}
TITLE: {title}
CONTENT: {body}
"""

async def run_critic(setup: RawSetup) -> Optional[CriticVerdict]:
    """Send setup to Ollama critic model and parse verdict."""
    prompt = CRITIC_PROMPT.format(
        source=setup.source,
        title=setup.title,
        body=setup.body[:2000]
    )

    try:
        async with aiohttp.ClientSession() as session:
            payload = {
                "model":  OLLAMA_MODEL,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0.15, "num_predict": 600}
            }
            async with session.post(
                OLLAMA_URL, json=payload,
                timeout=aiohttp.ClientTimeout(total=120)
            ) as r:
                if r.status != 200:
                    return None
                result = await r.json()
                raw = result.get("response", "").strip()

        # Clean and parse JSON
        raw = re.sub(r"```json|```", "", raw).strip()
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        if not match:
            return None
        data = json.loads(match.group(0))

        confidence = int(data.get("confidence", 0))
        verdict    = "PASS" if confidence >= CONFIDENCE_GATE else "FAIL"

        return CriticVerdict(
            setup_id=         setup.setup_id,
            confidence=       confidence,
            verdict=          verdict,
            concept_type=     data.get("concept_type", "NONE"),
            timeframe=        data.get("timeframe", "unspecified"),
            market=           data.get("market", "unspecified"),
            entry_logic=      data.get("entry_logic", ""),
            sl_logic=         data.get("sl_logic", ""),
            tp_logic=         data.get("tp_logic", ""),
            strengths=        data.get("strengths", ""),
            weaknesses=       data.get("weaknesses", ""),
            red_flags=        data.get("red_flags", ""),
            pine_viable=      bool(data.get("pine_viable", False)),
            critic_reasoning= data.get("critic_reasoning", ""),
        )

    except Exception as e:
        console.print(f"[dim]Critic error for {setup.setup_id[:8]}: {e}[/dim]")
        return None


# ─────────────────────────────────────────────────────────────
# PINE SCRIPT GENERATOR (from verdict)
# ─────────────────────────────────────────────────────────────

PINE_TEMPLATE = '''//@version=6
// ══════════════════════════════════════════════════════════════
// AUTO-GENERATED STRATEGY — SentinelFX Architect Agent
// Source:      {source}
// Author:      {author}
// Confidence:  {confidence}/100
// Generated:   {ts}
// Setup:       {title}
//
// CRITIC ANALYSIS:
// Strengths:   {strengths}
// Weaknesses:  {weaknesses}
// Red Flags:   {red_flags}
//
// Entry:  {entry_logic}
// SL:     {sl_logic}
// TP:     {tp_logic}
// ══════════════════════════════════════════════════════════════
strategy("{concept} [{tf}] — SentinelFX",
     overlay=true,
     initial_capital=100000,
     default_qty_type=strategy.fixed,
     default_qty_value=1,
     commission_type=strategy.commission.cash_per_contract,
     commission_value=2.0)

// ── Prop Firm Risk Controls ───────────────────────────────────
dd_limit     = input.float(4.5,  "Daily DD Limit %",  minval=0.5, maxval=10.0)
risk_pt      = input.float(0.5,  "Risk Per Trade %",  minval=0.1, maxval=3.0)
rr_ratio     = input.float(2.0,  "R:R Ratio",         minval=0.5, maxval=10.0)
account_size = input.float(100000,"Account Size ($)")

var float day_open_eq = strategy.initial_capital
var bool  kill        = false
if ta.change(time("D")) != 0
    day_open_eq := strategy.equity
    kill        := false
daily_dd = (day_open_eq - strategy.equity) / day_open_eq * 100
if daily_dd >= dd_limit
    kill := true
    strategy.close_all(comment="DD_KILL")

// ── {concept} Detection ───────────────────────────────────────
// AUTO-GENERATED FROM: {entry_logic}
swing_len = input.int(10, "Swing Length", minval=3, maxval=50)
ph = ta.pivothigh(high, swing_len, swing_len)
pl = ta.pivotlow(low,   swing_len, swing_len)
var float last_ph = na
var float last_pl = na
if not na(ph) then last_ph := ph
if not na(pl) then last_pl := pl

// Structure break signals
bos_bull  = not na(last_ph) and close > last_ph
bos_bear  = not na(last_pl) and close < last_pl

// FVG detection (3-candle imbalance)
bull_fvg = low > high[2]
bear_fvg = high < low[2]

// OB detection
bull_ob = close[1] < open[1] and close > open and close > high[1]
bear_ob = close[1] > open[1] and close < open and close < low[1]

// ── Entry Conditions (adapt to your specific setup) ──────────
// Concept: {concept}
// Timeframe: {tf} | Market: {market}
long_cond  = (bull_fvg or bos_bull or bull_ob) and strategy.position_size == 0 and not kill
short_cond = (bear_fvg or bos_bear or bear_ob) and strategy.position_size == 0 and not kill

// ── SL / TP from Critic: {sl_logic} / {tp_logic}
buf      = 2 * syminfo.mintick
long_sl  = not na(last_pl) ? last_pl - buf : close * 0.995
long_tp  = close + (close - long_sl) * rr_ratio
short_sl = not na(last_ph) ? last_ph + buf : close * 1.005
short_tp = close - (short_sl - close) * rr_ratio

risk_amt = account_size * (risk_pt / 100)
qty = math.max(1, math.floor(risk_amt / (math.abs(close - long_sl) / syminfo.mintick * syminfo.pointvalue)))

// ── Execute ───────────────────────────────────────────────────
if long_cond
    strategy.entry("Long",  strategy.long,  qty=qty)
    strategy.exit("Long X",  "Long",  stop=long_sl,  limit=long_tp)
if short_cond
    strategy.entry("Short", strategy.short, qty=qty)
    strategy.exit("Short X", "Short", stop=short_sl, limit=short_tp)

// ── Visuals ───────────────────────────────────────────────────
plotshape(long_cond,  "Long",  shape.triangleup,   location.belowbar, color.teal, size=size.small)
plotshape(short_cond, "Short", shape.triangledown, location.abovebar, color.red,  size=size.small)
plot(not na(last_ph) ? last_ph : na, "Last High", color.new(color.red,  60))
plot(not na(last_pl) ? last_pl : na, "Last Low",  color.new(color.teal, 60))

// ── Dashboard ─────────────────────────────────────────────────
var table t = table.new(position.top_right, 2, 5, bgcolor=color.new(color.black, 80))
if barstate.islast
    table.cell(t,0,0,"Confidence",  text_color=color.gray,  text_size=size.tiny)
    table.cell(t,1,0,"{confidence}/100",
               text_color={confidence} >= 80 ? color.green : {confidence} >= 65 ? color.yellow : color.red,
               text_size=size.tiny)
    table.cell(t,0,1,"Daily DD",    text_color=color.gray,  text_size=size.tiny)
    table.cell(t,1,1,str.tostring(daily_dd,"#.##")+"%",
               text_color=daily_dd >= dd_limit*0.8 ? color.red : color.green, text_size=size.tiny)
    table.cell(t,0,2,"Kill",        text_color=color.gray,  text_size=size.tiny)
    table.cell(t,1,2,kill?"ON 🔴":"OFF 🟢",
               text_color=kill?color.red:color.green, text_size=size.tiny)
    table.cell(t,0,3,"Net P&L",     text_color=color.gray,  text_size=size.tiny)
    table.cell(t,1,3,str.tostring(strategy.netprofit,"$#,###"),
               text_color=strategy.netprofit>=0?color.green:color.red, text_size=size.tiny)
    table.cell(t,0,4,"Win Rate",    text_color=color.gray,  text_size=size.tiny)
    table.cell(t,1,4,str.tostring(strategy.wintrades/math.max(strategy.closedtrades,1)*100,"#.#")+"%",
               text_color=color.white, text_size=size.tiny)
'''

def generate_pine(setup: dict, verdict: CriticVerdict) -> str:
    return PINE_TEMPLATE.format(
        source=        setup.get("source",""),
        author=        setup.get("author",""),
        confidence=    verdict.confidence,
        ts=            datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        title=         setup.get("title","")[:80],
        strengths=     verdict.strengths[:100],
        weaknesses=    verdict.weaknesses[:100],
        red_flags=     verdict.red_flags[:100],
        entry_logic=   verdict.entry_logic[:100],
        sl_logic=      verdict.sl_logic[:80],
        tp_logic=      verdict.tp_logic[:80],
        concept=       verdict.concept_type,
        tf=            verdict.timeframe,
        market=        verdict.market,
    )


# ─────────────────────────────────────────────────────────────
# PYTHON BACKTESTER
# ─────────────────────────────────────────────────────────────

def run_backtest(verdict: CriticVerdict, days: int = 365) -> dict:
    """
    Quick Python backtest using yfinance + pandas.
    Simulates the concept type with realistic proxy logic.
    Returns Sharpe, win rate, total return, max DD.
    """
    # Pick a proxy instrument
    MARKET_MAP = {
        "ES": "ES=F", "NQ": "NQ=F", "CL": "CL=F", "GC": "GC=F",
        "EURUSD": "EURUSD=X", "GBPUSD": "GBPUSD=X",
        "BTC": "BTC-USD", "ETH": "ETH-USD",
    }
    mkt    = verdict.market.upper().replace("/","").replace(" ","")
    symbol = next((v for k, v in MARKET_MAP.items() if k in mkt), "ES=F")

    try:
        df = yf.download(symbol, period=f"{days}d", interval="1h",
                         auto_adjust=True, progress=False)
        if df.empty or len(df) < 50:
            return {"error": "insufficient data"}

        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
        df = df[["Open","High","Low","Close","Volume"]].dropna()

        # ── Strategy Logic by concept type ────────────────────
        concept = verdict.concept_type
        rr      = 2.0   # default RR
        sl_pct  = 0.003 # 0.3% default SL

        if concept == "FVG":
            # Long when 3-candle bullish gap forms; Short on bearish
            df["bull_fvg"] = df["Low"] > df["High"].shift(2)
            df["bear_fvg"] = df["High"] < df["Low"].shift(2)
            df["signal"]   = np.where(df["bull_fvg"], 1,
                             np.where(df["bear_fvg"], -1, 0))

        elif concept == "LIQUIDITY_SWEEP":
            # Detect wick sweep of rolling 20-bar high/low with close back inside
            roll = 20
            df["roll_high"] = df["High"].rolling(roll).max().shift(1)
            df["roll_low"]  = df["Low"].rolling(roll).min().shift(1)
            df["bsl_sweep"] = (df["High"] > df["roll_high"]) & (df["Close"] < df["roll_high"])
            df["ssl_sweep"] = (df["Low"]  < df["roll_low"])  & (df["Close"] > df["roll_low"])
            df["signal"]    = np.where(df["ssl_sweep"], 1,
                              np.where(df["bsl_sweep"], -1, 0))

        elif concept == "BOS_CHOCH":
            # Close above/below 20-bar pivot
            roll = 20
            df["ph"]     = df["High"].rolling(roll).max().shift(1)
            df["pl"]     = df["Low"].rolling(roll).min().shift(1)
            df["signal"] = np.where(df["Close"] > df["ph"], 1,
                           np.where(df["Close"] < df["pl"], -1, 0))

        else:  # ORDER_BLOCK default
            # Last opposing candle before impulse
            df["bullish_candle"] = df["Close"] > df["Open"]
            df["bearish_candle"] = df["Close"] < df["Open"]
            df["impulse"]        = df["Close"].pct_change().abs() > 0.003
            df["bull_ob"] = df["bearish_candle"].shift(1) & df["impulse"]
            df["bear_ob"] = df["bullish_candle"].shift(1) & df["impulse"]
            df["signal"]  = np.where(df["bull_ob"], 1,
                            np.where(df["bear_ob"], -1, 0))

        # ── Simulate trades ────────────────────────────────────
        trades    = []
        in_trade  = False
        direction = 0
        entry_p   = 0.0
        sl_p      = 0.0
        tp_p      = 0.0

        for i in range(1, len(df)):
            row = df.iloc[i]

            if in_trade:
                if direction == 1:
                    if row["Low"] <= sl_p:
                        trades.append({"pnl": sl_p - entry_p, "win": False})
                        in_trade = False
                    elif row["High"] >= tp_p:
                        trades.append({"pnl": tp_p - entry_p, "win": True})
                        in_trade = False
                else:
                    if row["High"] >= sl_p:
                        trades.append({"pnl": entry_p - sl_p, "win": False})
                        in_trade = False
                    elif row["Low"] <= tp_p:
                        trades.append({"pnl": entry_p - tp_p, "win": True})
                        in_trade = False
                continue

            sig = df["signal"].iloc[i]
            if sig != 0:
                entry_p   = float(row["Close"])
                direction = int(sig)
                if direction == 1:
                    sl_p = entry_p * (1 - sl_pct)
                    tp_p = entry_p + (entry_p - sl_p) * rr
                else:
                    sl_p = entry_p * (1 + sl_pct)
                    tp_p = entry_p - (sl_p - entry_p) * rr
                in_trade = True

        # ── Stats ──────────────────────────────────────────────
        if len(trades) < 5:
            return {"error": "not enough trades", "trade_count": len(trades)}

        pnls     = [t["pnl"] for t in trades]
        wins     = [t for t in trades if t["win"]]
        win_rate = len(wins) / len(trades)
        avg_pnl  = np.mean(pnls)
        std_pnl  = np.std(pnls)
        sharpe   = (avg_pnl / std_pnl * np.sqrt(252)) if std_pnl > 0 else 0

        # Running equity for max DD
        equity = [100000.0]
        for t in trades:
            equity.append(equity[-1] + t["pnl"] * 10)   # $10/pt proxy
        equity    = np.array(equity)
        peak      = np.maximum.accumulate(equity)
        drawdowns = (peak - equity) / peak
        max_dd    = float(drawdowns.max())

        result = {
            "symbol":       symbol,
            "concept":      concept,
            "trade_count":  len(trades),
            "win_rate":     round(win_rate * 100, 1),
            "sharpe":       round(sharpe, 2),
            "avg_pnl":      round(avg_pnl, 4),
            "max_dd_pct":   round(max_dd * 100, 2),
            "total_return": round((equity[-1] - equity[0]) / equity[0] * 100, 2),
            "passed":       sharpe >= 1.0 and win_rate >= 0.40 and max_dd < 0.20,
        }
        return result

    except Exception as e:
        return {"error": str(e)}


# ─────────────────────────────────────────────────────────────
# ORCHESTRATOR
# ─────────────────────────────────────────────────────────────

async def run_scan(source_filter: Optional[str] = None):
    """Full pipeline: scan → critic → build → backtest."""
    console.rule("[bold cyan]Setup Hunter — Scan Started[/bold cyan]")

    all_setups: list[RawSetup] = []

    with Progress(SpinnerColumn(), TextColumn("{task.description}"),
                  console=console) as prog:

        if not source_filter or source_filter == "reddit":
            t = prog.add_task("Scanning Reddit...", total=None)
            try:
                scanner = RedditScanner()
                setups  = await scanner.scan()
                all_setups.extend(setups)
                for s in setups: db.save_setup(s)
            except Exception as e:
                console.print(f"[red]Reddit scan failed: {e}[/red]")
            prog.remove_task(t)

        if not source_filter or source_filter == "tradingview":
            t = prog.add_task("Scanning TradingView ideas...", total=None)
            try:
                scanner = TradingViewIdeasScanner()
                setups  = await scanner.scan()
                all_setups.extend(setups)
                for s in setups: db.save_setup(s)
            except Exception as e:
                console.print(f"[red]TradingView scan failed: {e}[/red]")
            prog.remove_task(t)

        if not source_filter or source_filter == "stocktwits":
            t = prog.add_task("Scanning StockTwits...", total=None)
            try:
                scanner = StockTwitsScanner()
                setups  = await scanner.scan()
                all_setups.extend(setups)
                for s in setups: db.save_setup(s)
            except Exception as e:
                console.print(f"[red]StockTwits scan failed: {e}[/red]")
            prog.remove_task(t)

        if not source_filter or source_filter == "babypips":
            t = prog.add_task("Scanning Babypips forums...", total=None)
            try:
                scanner = BabypipsScanner()
                setups  = await scanner.scan()
                all_setups.extend(setups)
                for s in setups: db.save_setup(s)
            except Exception as e:
                console.print(f"[red]Babypips scan failed: {e}[/red]")
            prog.remove_task(t)

        if not source_filter or source_filter == "twitter":
            t = prog.add_task("Scanning Twitter/X via Nitter...", total=None)
            try:
                scanner = TwitterScanner()
                setups  = await scanner.scan()
                all_setups.extend(setups)
                for s in setups: db.save_setup(s)
            except Exception as e:
                console.print(f"[red]Twitter scan failed: {e}[/red]")
            prog.remove_task(t)

    # Also include any previously scraped but unscored setups
    unscored = db.get_unscored()
    if unscored:
        all_setups.extend([
            RawSetup(**{k: v for k, v in u.items()
                        if k in RawSetup.__dataclass_fields__})
            for u in unscored
        ])

    console.print(f"\n[bold]Total setups to evaluate:[/bold] {len(all_setups)}\n")

    if not all_setups:
        console.print("[yellow]No new setups found.[/yellow]")
        return

    # ── Run critic on each setup ───────────────────────────────
    passed  = []
    failed  = []
    errors  = []

    with Progress(SpinnerColumn(), TextColumn("{task.description}"),
                  console=console) as prog:
        task = prog.add_task(f"Critic evaluating {len(all_setups)} setups...", total=None)

        for i, setup in enumerate(all_setups):
            prog.update(task, description=
                f"Critic [{i+1}/{len(all_setups)}]: {setup.title[:50]}...")

            verdict = await run_critic(setup)
            if not verdict:
                errors.append(setup.setup_id)
                continue

            db.save_verdict(verdict)

            color = "green" if verdict.verdict == "PASS" else "red"
            icon  = "★" if verdict.verdict == "PASS" else "✗"
            console.print(
                f"  [{color}]{icon}[/{color}] [{verdict.confidence:>3}/100] "
                f"{verdict.concept_type:<16} | {setup.source:<12} | "
                f"{setup.title[:50]}"
            )

            if verdict.verdict == "PASS" and verdict.pine_viable:
                passed.append((setup, verdict))
            else:
                failed.append(setup.setup_id)

        prog.remove_task(task)

    console.print(f"\n[green]PASSED[/green]: {len(passed)}  "
                  f"[red]FAILED[/red]: {len(failed)}  "
                  f"[dim]ERRORS: {len(errors)}[/dim]\n")

    # ── Build Pine + Backtest for passing setups ───────────────
    PINE_OUT.mkdir(exist_ok=True)
    BACKTEST_OUT.mkdir(exist_ok=True)

    for setup, verdict in passed:
        # Generate Pine Script
        pine_code = generate_pine(vars(setup) if hasattr(setup, '__dict__') else setup.__dict__,
                                  verdict)
        slug  = f"{verdict.concept_type.lower()}_{verdict.confidence}_{setup.setup_id[:8]}"
        ppath = PINE_OUT / f"{slug}.pine"
        ppath.write_text(pine_code, encoding="utf-8")
        db.mark_pine_built(setup.setup_id)
        console.print(f"  [teal]Pine Script:[/teal] {ppath.name}")

        # Run Python backtest
        console.print(f"  [yellow]Backtesting...[/yellow]", end=" ")
        result = run_backtest(verdict)
        db.save_backtest(setup.setup_id,
                         result.get("sharpe", 0),
                         result.get("win_rate", 0),
                         json.dumps(result))

        if "error" in result:
            console.print(f"[red]{result['error']}[/red]")
        else:
            bt_color = "green" if result.get("passed") else "yellow"
            console.print(
                f"[{bt_color}]Sharpe {result['sharpe']} | "
                f"WR {result['win_rate']}% | "
                f"MaxDD {result['max_dd_pct']}% | "
                f"Trades {result['trade_count']}[/{bt_color}]"
            )

            # Save detailed backtest report
            rpath = BACKTEST_OUT / f"{slug}_backtest.json"
            rpath.write_text(json.dumps(result, indent=2))

    console.rule("[bold cyan]Scan Complete[/bold cyan]")


def cmd_list():
    rows = db.get_all_verdicts()
    if not rows:
        console.print("[yellow]No scored setups yet. Run --scan first.[/yellow]")
        return

    table = Table(title=f"Scored Setups ({len(rows)} total)",
                  show_header=True, header_style="bold cyan")
    table.add_column("Score", justify="right", width=6)
    table.add_column("Concept",  width=16)
    table.add_column("Source",   width=12)
    table.add_column("TF",       width=6)
    table.add_column("Market",   width=8)
    table.add_column("Pine",     width=5)
    table.add_column("BT",       width=5)
    table.add_column("Sharpe",   width=7)
    table.add_column("Title",    width=40)

    for r in rows:
        score = r["confidence"]
        color = "green" if score >= 80 else "yellow" if score >= 65 else "red"
        pine  = "✓" if r.get("pine_built") else "—"
        bt    = "✓" if r.get("backtest_run") else "—"
        sh    = f"{r['backtest_sharpe']:.2f}" if r.get("backtest_sharpe") else "—"
        table.add_row(
            f"[{color}]{score}[/{color}]",
            r.get("concept_type","?"),
            r.get("source","?"),
            r.get("timeframe","?"),
            r.get("market","?")[:8],
            pine, bt, sh,
            r.get("title","")[:40],
        )

    console.print(table)


# ─────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SentinelFX Setup Hunter")
    parser.add_argument("--scan",   action="store_true", help="Scan all sources + critique + build")
    parser.add_argument("--source", type=str, default=None,
                        choices=["reddit","tradingview","stocktwits","babypips","twitter"],
                        help="Limit scan to one source")
    parser.add_argument("--list",   action="store_true", help="Show all scored setups")
    parser.add_argument("--review", action="store_true", help="Re-run critic on unscored setups")
    args = parser.parse_args()

    if args.scan or args.source:
        asyncio.run(run_scan(source_filter=args.source))
    elif args.list:
        cmd_list()
    elif args.review:
        asyncio.run(run_scan(source_filter=None))
    else:
        console.print(Panel(
            "[cyan]SentinelFX Setup Hunter[/cyan]\n\n"
            "  [white]python setup_hunter.py --scan[/white]                  Scan all sources\n"
            "  [white]python setup_hunter.py --scan --source reddit[/white]  Reddit only\n"
            "  [white]python setup_hunter.py --scan --source tradingview[/white]\n"
            "  [white]python setup_hunter.py --scan --source youtube[/white]\n"
            "  [white]python setup_hunter.py --scan --source twitter[/white]\n"
            "  [white]python setup_hunter.py --list[/white]                  Show scored setups\n",
            title="Setup Hunter", border_style="cyan"
        ))
