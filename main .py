from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import yfinance as yf
import pandas as pd
import requests
from datetime import datetime, timedelta
import os
import asyncio
from concurrent.futures import ThreadPoolExecutor

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

TICKERS = [
    "MU","AVGO","MRVL","AMD","NVDA","ASML","DELL","TSM","CRWD","VRT",
    "ARM","GEV","ALAB","QQQ","MSFT","AAPL","AMZN","PLTR","ORCL","ETN",
    "CEG","ANET","VOO","VST","NOW","IONQ","SMCI","HPE","RGTI","QBTS"
]

INDEX_TICKERS = {"S&P 500":"^GSPC","Nasdaq":"^IXIC","Dow":"^DJI","Russell":"^RUT"}
FINNHUB_KEY   = os.getenv("FINNHUB_KEY", "")
executor      = ThreadPoolExecutor(max_workers=20)

# Fix Yahoo Finance rate limiting — spoof browser headers
YF_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}

def make_ticker(sym):
    t = yf.Ticker(sym)
    # Inject headers into the yfinance session
    t.session = requests.Session()
    t.session.headers.update(YF_HEADERS)
    return t

# ── Helpers ───────────────────────────────────────────────────────────
def fmt(v, prefix=""):
    if v is None or (isinstance(v, float) and v != v): return "—"
    if isinstance(v, (int, float)):
        if abs(v) >= 1e12: return f"{prefix}{v/1e12:.2f}T"
        if abs(v) >= 1e9:  return f"{prefix}{v/1e9:.2f}B"
        if abs(v) >= 1e6:  return f"{prefix}{v/1e6:.2f}M"
        return f"{prefix}{v:,.2f}"
    return str(v)

def calc_rsi(closes, period=14):
    try:
        s = pd.Series(closes)
        delta    = s.diff()
        gain     = delta.clip(lower=0)
        loss     = -delta.clip(upper=0)
        avg_gain = gain.ewm(com=period-1, min_periods=period).mean()
        avg_loss = loss.ewm(com=period-1, min_periods=period).mean()
        rs  = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        val = round(float(rsi.iloc[-1]), 1)
        return None if pd.isna(rsi.iloc[-1]) else val
    except:
        return None

def get_news_finnhub(ticker):
    if not FINNHUB_KEY:
        return []
    try:
        today    = datetime.now().strftime("%Y-%m-%d")
        week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        r = requests.get(
            "https://finnhub.io/api/v1/company-news",
            params={"symbol": ticker, "from": week_ago, "to": today, "token": FINNHUB_KEY},
            timeout=5
        )
        if not r.ok: return []
        articles = []
        for item in r.json()[:6]:
            articles.append({
                "headline": item.get("headline", ""),
                "source":   item.get("source", ""),
                "date":     datetime.fromtimestamp(item.get("datetime", 0)).strftime("%b %d, %Y")
                            if item.get("datetime") else "",
                "url":      item.get("url", ""),
                "sentiment": "neu"
            })
        return articles
    except:
        return []

def get_news_yfinance(ticker):
    try:
        t = make_ticker(ticker)
        articles = []
        for item in (t.news or [])[:6]:
            articles.append({
                "headline": item.get("title", ""),
                "source":   item.get("publisher", ""),
                "date":     datetime.fromtimestamp(item.get("providerPublishTime", 0)).strftime("%b %d, %Y")
                            if item.get("providerPublishTime") else "",
                "url":      item.get("link", ""),
                "sentiment": "neu"
            })
        return articles
    except:
        return []

# ── Single ticker fetch ───────────────────────────────────────────────
def fetch_one(ticker):
    try:
        sym  = ticker.upper()
        t    = make_ticker(sym)
        info = t.info

        if not info or len(info) < 5:
            raise ValueError("Empty info returned — Yahoo may be rate limiting")

        price = info.get("currentPrice") or info.get("regularMarketPrice")
        prev  = info.get("previousClose") or info.get("regularMarketPreviousClose")
        change     = round(price - prev, 2)           if price and prev else 0
        change_pct = round((change / prev) * 100, 2)  if prev           else 0

        hist   = t.history(period="1y")
        closes = hist["Close"].tolist() if not hist.empty else []
        ma50   = round(float(pd.Series(closes).tail(50).mean()),  2) if len(closes) >= 50  else None
        ma200  = round(float(pd.Series(closes).tail(200).mean()), 2) if len(closes) >= 200 else None
        rsi    = calc_rsi(closes)

        news = get_news_finnhub(sym) or get_news_yfinance(sym)

        return {
            "ticker":        sym,
            "companyName":   info.get("longName") or info.get("shortName", sym),
            "price":         round(price, 2) if price else None,
            "change":        change,
            "changePct":     change_pct,
            "positive":      change >= 0,
            "open":          info.get("open") or info.get("regularMarketOpen"),
            "high":          info.get("dayHigh") or info.get("regularMarketDayHigh"),
            "low":           info.get("dayLow")  or info.get("regularMarketDayLow"),
            "prevClose":     prev,
            "volume":        fmt(info.get("volume") or info.get("regularMarketVolume")),
            "marketCap":     fmt(info.get("marketCap"), "$"),
            "peRatio":       round(info.get("trailingPE"), 2) if info.get("trailingPE") else "—",
            "fwdPE":         round(info.get("forwardPE"),  2) if info.get("forwardPE")  else "—",
            "eps":           fmt(info.get("trailingEps"), "$"),
            "week52High":    fmt(info.get("fiftyTwoWeekHigh"), "$"),
            "week52Low":     fmt(info.get("fiftyTwoWeekLow"),  "$"),
            "beta":          round(info.get("beta"), 2) if info.get("beta") else "—",
            "dividendYield": f"{round(info.get('dividendYield',0)*100,2)}%" if info.get("dividendYield") else "None",
            "analystTarget": fmt(info.get("targetMeanPrice"), "$"),
            "ma50":          ma50,
            "ma200":         ma200,
            "rsi":           rsi,
            "news":          news,
        }
    except Exception as e:
        return {"ticker": ticker.upper(), "error": str(e)}


def fetch_index(name, sym):
    try:
        t    = make_ticker(sym)
        info = t.info
        p    = info.get("regularMarketPrice") or info.get("currentPrice")
        prev = info.get("previousClose") or info.get("regularMarketPreviousClose")
        chg  = round(((p - prev) / prev) * 100, 2) if p and prev else 0
        return {"name": name, "val": f"{p:,.2f}" if p else "—",
                "chg": f"{'+' if chg>=0 else ''}{chg}%", "pos": chg >= 0}
    except:
        return {"name": name, "val": "—", "chg": "—", "pos": True}


# ── Routes ─────────────────────────────────────────────────────────────
@app.get("/quote/{ticker}")
async def get_quote(ticker: str):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(executor, fetch_one, ticker.upper())


@app.get("/quotes")
async def get_all_quotes():
    loop = asyncio.get_event_loop()

    stock_futures = [loop.run_in_executor(executor, fetch_one, t) for t in TICKERS]
    index_futures = [loop.run_in_executor(executor, fetch_index, name, sym)
                     for name, sym in INDEX_TICKERS.items()]

    stock_results = await asyncio.gather(*stock_futures, return_exceptions=True)
    index_results = await asyncio.gather(*index_futures, return_exceptions=True)

    results = {}
    for t, r in zip(TICKERS, stock_results):
        results[t] = r if not isinstance(r, Exception) else {"ticker": t, "error": str(r)}

    results["_markets"] = [r for r in index_results if not isinstance(r, Exception)]
    return results


@app.get("/health")
def health():
    return {"status": "ok", "finnhub": bool(FINNHUB_KEY), "workers": 20}
