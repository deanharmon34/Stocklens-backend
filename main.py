from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import requests
import pandas as pd
from datetime import datetime, timedelta
import os
import asyncio
from concurrent.futures import ThreadPoolExecutor
import time
import json

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
    "CEG","ANET","VOO","VST","NOW","IONQ","SMCI","HPE","RGTI","QBTS","HGRAF","APH"
]
INDEX_TICKERS = {"S&P 500":"^GSPC","Nasdaq":"^IXIC","Dow":"^DJI","Russell":"^RUT"}
FINNHUB_KEY   = os.getenv("FINNHUB_KEY", "")
executor      = ThreadPoolExecutor(max_workers=20)

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://finance.yahoo.com",
    "Origin":  "https://finance.yahoo.com",
})

# ── Yahoo Finance direct API ───────────────────────────────────────────
def yahoo_quote(symbol):
    """Fetch quote using Yahoo Finance v8 chart + v10 quoteSummary directly."""
    sym = symbol.upper()

    # 2-day fetch — gives exactly [yesterday_close, today_close]
    r5 = SESSION.get(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}",
        params={"interval": "1d", "range": "2d", "includePrePost": "false"},
        timeout=10
    )
    r5.raise_for_status()
    d5     = r5.json()["chart"]["result"][0]
    meta   = d5["meta"]
    c5     = d5.get("indicators", {}).get("quote", [{}])[0].get("close", [])
    c5     = [c for c in c5 if c is not None]

    # 1-year fetch for MA and RSI calculations
    closes = c5  # fallback
    try:
        r1y = SESSION.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}",
            params={"interval": "1d", "range": "1y", "includePrePost": "false"},
            timeout=10
        )
        if r1y.ok:
            c1y    = r1y.json()["chart"]["result"][0].get("indicators", {}).get("quote", [{}])[0].get("close", [])
            closes = [c for c in c1y if c is not None]
    except:
        pass

    price = meta.get("regularMarketPrice")

    # Get previous close — try meta fields first, then 5d history
    prev = meta.get("regularMarketPreviousClose")
    if prev is None: prev = meta.get("chartPreviousClose")
    if prev is None: prev = meta.get("previousClose")
    # Last resort: use second-to-last from 5d history (yesterday's close)
    if prev is None and len(c5) >= 2:
        prev = c5[-2]

    # Calculate daily change from price vs prev close
    # Never use Yahoo's change fields — they are unreliable
    if price is not None and prev is not None and prev != 0:
        change     = round(float(price) - float(prev), 2)
        change_pct = round((change / float(prev)) * 100, 2)
    else:
        change     = 0
        change_pct = 0

    # MA + RSI from history
    ma50  = round(float(pd.Series(closes).tail(50).mean()),  2) if len(closes) >= 50  else None
    ma200 = round(float(pd.Series(closes).tail(200).mean()), 2) if len(closes) >= 200 else None
    rsi   = calc_rsi(closes)

    # v10 quoteSummary — fundamentals
    qs_url = f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{sym}"
    qs_r = SESSION.get(qs_url, params={
        "modules": "summaryDetail,defaultKeyStatistics,financialData,price"
    }, timeout=10)

    info = {}
    if qs_r.ok:
        try:
            result = qs_r.json()["quoteSummary"]["result"][0]
            sd  = result.get("summaryDetail", {})
            ks  = result.get("defaultKeyStatistics", {})
            fd  = result.get("financialData", {})
            pr  = result.get("price", {})
            info = {
                "companyName":   pr.get("longName", {}).get("raw") or pr.get("shortName", {}).get("raw") or sym,
                "marketCap":     pr.get("marketCap", {}).get("fmt","—"),
                "volume":        pr.get("regularMarketVolume", {}).get("fmt","—"),
                "open":          pr.get("regularMarketOpen", {}).get("fmt","—"),
                "high":          pr.get("regularMarketDayHigh", {}).get("fmt","—"),
                "low":           pr.get("regularMarketDayLow", {}).get("fmt","—"),
                "prevClose":     pr.get("regularMarketPreviousClose", {}).get("fmt","—"),
                "peRatio":       sd.get("trailingPE", {}).get("fmt","—"),
                "fwdPE":         sd.get("forwardPE",  {}).get("fmt","—"),
                "eps":           ks.get("trailingEps",{}).get("fmt","—"),
                "week52High":    sd.get("fiftyTwoWeekHigh",{}).get("fmt","—"),
                "week52Low":     sd.get("fiftyTwoWeekLow", {}).get("fmt","—"),
                "beta":          sd.get("beta",{}).get("fmt","—"),
                "dividendYield": sd.get("dividendYield",{}).get("fmt","None"),
                "analystTarget": fd.get("targetMeanPrice",{}).get("fmt","—"),
            }
        except:
            pass

    return {
        "ticker":        sym,
        "companyName":   info.get("companyName", sym),
        "price":         round(price, 2) if price else None,
        "change":        change,
        "changePct":     change_pct,
        "positive":      change >= 0,
        "open":          info.get("open","—"),
        "high":          info.get("high","—"),
        "low":           info.get("low","—"),
        "prevClose":     info.get("prevClose","—"),
        "volume":        info.get("volume","—"),
        "marketCap":     info.get("marketCap","—"),
        "peRatio":       info.get("peRatio","—"),
        "fwdPE":         info.get("fwdPE","—"),
        "eps":           info.get("eps","—"),
        "week52High":    info.get("week52High","—"),
        "week52Low":     info.get("week52Low","—"),
        "beta":          info.get("beta","—"),
        "dividendYield": info.get("dividendYield","None"),
        "analystTarget": info.get("analystTarget","—"),
        "ma50":          ma50,
        "ma200":         ma200,
        "rsi":           rsi,
        "news":          get_news(sym),
        "dayChgPct":     change_pct,
        "qtrChgPct":     calc_qtr_change(closes),
    }


def yahoo_index(name, sym):
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
        r   = SESSION.get(url, params={"interval":"1d","range":"5d"}, timeout=8)
        r.raise_for_status()
        meta = r.json()["chart"]["result"][0]["meta"]
        p    = meta.get("regularMarketPrice")
        prev = meta.get("chartPreviousClose") or meta.get("previousClose")
        chg  = round(((p-prev)/prev)*100, 2) if p and prev else 0
        return {"name": name, "val": f"{p:,.2f}" if p else "—",
                "chg": f"{'+' if chg>=0 else ''}{chg}%", "pos": chg >= 0}
    except Exception as e:
        return {"name": name, "val": "—", "chg": "—", "pos": True}


# ── Helpers ───────────────────────────────────────────────────────────
def calc_rsi(closes, period=14):
    try:
        s        = pd.Series(closes)
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


def get_news(ticker):
    # Finnhub first
    if FINNHUB_KEY:
        try:
            today    = datetime.now().strftime("%Y-%m-%d")
            week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
            r = requests.get(
                "https://finnhub.io/api/v1/company-news",
                params={"symbol": ticker, "from": week_ago, "to": today, "token": FINNHUB_KEY},
                timeout=5
            )
            if r.ok:
                articles = []
                for item in r.json()[:6]:
                    articles.append({
                        "headline": item.get("headline",""),
                        "source":   item.get("source",""),
                        "date":     datetime.fromtimestamp(item.get("datetime",0)).strftime("%b %d, %Y")
                                    if item.get("datetime") else "",
                        "url":      item.get("url",""),
                        "sentiment":"neu"
                    })
                if articles:
                    return articles
        except:
            pass

    # Yahoo Finance news fallback
    try:
        r = SESSION.get(
            f"https://query1.finance.yahoo.com/v1/finance/search",
            params={"q": ticker, "newsCount": 6, "quotesCount": 0},
            timeout=5
        )
        if r.ok:
            articles = []
            for item in r.json().get("news", [])[:6]:
                articles.append({
                    "headline": item.get("title",""),
                    "source":   item.get("publisher",""),
                    "date":     datetime.fromtimestamp(item.get("providerPublishTime",0)).strftime("%b %d, %Y")
                                if item.get("providerPublishTime") else "",
                    "url":      item.get("link",""),
                    "sentiment":"neu"
                })
            return articles
    except:
        pass

    return []


def calc_qtr_change(closes):
    """Calculate % change over last ~63 trading days (1 quarter)."""
    try:
        if len(closes) < 63:
            return None
        start = closes[-63]
        end   = closes[-1]
        return round(((end - start) / start) * 100, 2) if start else None
    except:
        return None


def fetch_one(ticker):
    try:
        return yahoo_quote(ticker.upper())
    except Exception as e:
        return {"ticker": ticker.upper(), "error": str(e)}


def fetch_index(name, sym):
    return yahoo_index(name, sym)


# ── Routes ────────────────────────────────────────────────────────────
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
    try:
        r = SESSION.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/AAPL",
            params={"interval":"1d","range":"2d"},
            timeout=5
        )
        yahoo_ok = r.ok
        data     = r.json()["chart"]["result"][0]
        meta     = data["meta"]
        closes_2d = [c for c in data.get("indicators",{}).get("quote",[{}])[0].get("close",[]) if c is not None]
        price    = meta.get("regularMarketPrice")
        prev     = meta.get("regularMarketPreviousClose") or meta.get("chartPreviousClose") or meta.get("previousClose")
        chg      = meta.get("regularMarketChange")
        chg_pct  = meta.get("regularMarketChangePercent")
        # Show which fields are available for debugging
        available = [k for k in ["regularMarketPreviousClose","chartPreviousClose",
                                  "regularMarketChange","regularMarketChangePercent"]
                     if meta.get(k) is not None]
    except Exception as e:
        yahoo_ok  = False
        price     = None
        prev      = None
        chg       = None
        chg_pct   = None
        available = []

    return {
        "status":       "ok",
        "finnhub":      bool(FINNHUB_KEY),
        "yahoo_ok":     yahoo_ok,
        "aapl_price":   price,
        "aapl_prev":    prev,
        "aapl_chg":     chg,
        "aapl_chg_pct": chg_pct,
        "closes_2d":    closes_2d if 'closes_2d' in dir() else [],
        "available_fields": available,
        "workers":      20
    }
