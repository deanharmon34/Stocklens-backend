from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import requests
import pandas as pd
from datetime import datetime, timedelta
import os
import asyncio
from concurrent.futures import ThreadPoolExecutor

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

TICKERS = [
    "MU","AVGO","MRVL","AMD","NVDA","ASML","DELL","TSM","APH","CRWD","VRT",
    "ARM","GEV","ALAB","QQQ","MSFT","AAPL","AMZN","PLTR","ORCL","ETN",
    "CEG","ANET","VOO","VST","NOW","IONQ","SMCI","HPE","RGTI","QBTS","HGRAF","APH"
]
INDEX_TICKERS = {"S&P 500":"^GSPC","Nasdaq":"^IXIC","Dow":"^DJI","Russell":"^RUT"}
FINNHUB_KEY   = os.getenv("FINNHUB_KEY","")
executor      = ThreadPoolExecutor(max_workers=20)

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept":"*/*",
    "Accept-Language":"en-US,en;q=0.9",
    "Referer":"https://finance.yahoo.com",
    "Origin":"https://finance.yahoo.com",
})

def safe(v, prefix="", suffix="", decimals=2):
    """Safely format a value, handling None, NaN, nested dicts."""
    if v is None: return "—"
    if isinstance(v, dict):
        # Yahoo sometimes returns {"raw":123, "fmt":"123"}
        v = v.get("raw") or v.get("fmt")
        if v is None: return "—"
    try:
        f = float(v)
        if f != f: return "—"  # NaN check
        if abs(f) >= 1e12: return f"{prefix}{f/1e12:.2f}T{suffix}"
        if abs(f) >= 1e9:  return f"{prefix}{f/1e9:.2f}B{suffix}"
        if abs(f) >= 1e6:  return f"{prefix}{f/1e6:.2f}M{suffix}"
        return f"{prefix}{f:,.{decimals}f}{suffix}"
    except:
        return str(v) if v else "—"

def safe_float(v):
    if v is None: return None
    if isinstance(v, dict): v = v.get("raw") or v.get("fmt")
    try:
        f = float(v)
        return None if f != f else f
    except:
        return None

def calc_rsi(closes, period=14):
    try:
        s = pd.Series(closes)
        delta = s.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.ewm(com=period-1, min_periods=period).mean()
        avg_loss = loss.ewm(com=period-1, min_periods=period).mean()
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        val = round(float(rsi.iloc[-1]), 1)
        return None if pd.isna(rsi.iloc[-1]) else val
    except:
        return None

def get_news(ticker):
    if FINNHUB_KEY:
        try:
            today    = datetime.now().strftime("%Y-%m-%d")
            week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
            r = requests.get("https://finnhub.io/api/v1/company-news",
                params={"symbol":ticker,"from":week_ago,"to":today,"token":FINNHUB_KEY},timeout=5)
            if r.ok:
                articles = []
                for item in r.json()[:6]:
                    articles.append({
                        "headline": item.get("headline",""),
                        "source":   item.get("source",""),
                        "date":     datetime.fromtimestamp(item.get("datetime",0)).strftime("%b %d, %Y") if item.get("datetime") else "",
                        "url":      item.get("url",""),
                        "sentiment":"neu"
                    })
                if articles: return articles
        except: pass
    # Yahoo news fallback
    try:
        r = SESSION.get("https://query1.finance.yahoo.com/v1/finance/search",
            params={"q":ticker,"newsCount":6,"quotesCount":0},timeout=5)
        if r.ok:
            return [{"headline":i.get("title",""),"source":i.get("publisher",""),
                     "date":datetime.fromtimestamp(i.get("providerPublishTime",0)).strftime("%b %d, %Y") if i.get("providerPublishTime") else "",
                     "url":i.get("link",""),"sentiment":"neu"}
                    for i in r.json().get("news",[])[:6]]
    except: pass
    return []

def fetch_one(ticker):
    try:
        sym = ticker.upper()

        # ── 2-day chart for price + prev close ──────────────────────
        r2 = SESSION.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}",
            params={"interval":"1d","range":"2d","includePrePost":"false"},timeout=10)
        r2.raise_for_status()
        d2   = r2.json()["chart"]["result"][0]
        meta = d2["meta"]
        c2   = [c for c in d2.get("indicators",{}).get("quote",[{}])[0].get("close",[]) if c is not None]

        price = meta.get("regularMarketPrice")
        prev  = meta.get("regularMarketPreviousClose") or meta.get("chartPreviousClose") or (c2[-2] if len(c2)>=2 else None)
        change     = round(float(price)-float(prev),2)      if price and prev else 0
        change_pct = round((change/float(prev))*100,2)      if prev and float(prev)!=0 else 0

        # ── 1-year chart for MA + RSI ────────────────────────────────
        closes = c2
        ma50 = ma200 = None
        try:
            r1y = SESSION.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}",
                params={"interval":"1d","range":"1y","includePrePost":"false"},timeout=10)
            if r1y.ok:
                c1y = [c for c in r1y.json()["chart"]["result"][0].get("indicators",{}).get("quote",[{}])[0].get("close",[]) if c is not None]
                if c1y: closes = c1y
        except: pass
        if len(closes)>=50:  ma50  = round(float(pd.Series(closes).tail(50).mean()),2)
        if len(closes)>=200: ma200 = round(float(pd.Series(closes).tail(200).mean()),2)
        rsi = calc_rsi(closes)

        # ── v10 quoteSummary for fundamentals ────────────────────────
        pe=fwdpe=eps=w52h=w52l=beta=divy=target=mktcap=vol=op=hi=lo=pc=name=None
        try:
            qs = SESSION.get(f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{sym}",
                params={"modules":"summaryDetail,defaultKeyStatistics,financialData,price"},timeout=10)
            if qs.ok:
                res = qs.json()["quoteSummary"]["result"][0]
                sd  = res.get("summaryDetail",{})
                ks  = res.get("defaultKeyStatistics",{})
                fd  = res.get("financialData",{})
                pr  = res.get("price",{})
                # Company name — plain string in price module
                name   = pr.get("longName") or pr.get("shortName") or sym
                # All these come back as {"raw":X,"fmt":"Y"} — use safe_float/safe
                pe     = safe(sd.get("trailingPE"))
                fwdpe  = safe(sd.get("forwardPE"))
                eps    = safe(ks.get("trailingEps"), prefix="$")
                w52h   = safe(sd.get("fiftyTwoWeekHigh"), prefix="$")
                w52l   = safe(sd.get("fiftyTwoWeekLow"),  prefix="$")
                beta   = safe(sd.get("beta"))
                divy_r = safe_float(sd.get("dividendYield"))
                divy   = f"{round(divy_r*100,2)}%" if divy_r else "None"
                target = safe(fd.get("targetMeanPrice"), prefix="$")
                mktcap = safe(pr.get("marketCap"), prefix="$")
                vol    = safe(pr.get("regularMarketVolume"))
                op     = safe(pr.get("regularMarketOpen"),              prefix="$")
                hi     = safe(pr.get("regularMarketDayHigh"),           prefix="$")
                lo     = safe(pr.get("regularMarketDayLow"),            prefix="$")
                pc     = safe(pr.get("regularMarketPreviousClose"),     prefix="$")
        except Exception as e:
            pass  # fall through to meta fallbacks

        # ── Fallback to chart meta for OHLC ─────────────────────────
        if op   == "—" or op   is None: op  = safe(meta.get("regularMarketOpen"),  prefix="$")
        if hi   == "—" or hi   is None: hi  = safe(meta.get("regularMarketDayHigh"),prefix="$")
        if lo   == "—" or lo   is None: lo  = safe(meta.get("regularMarketDayLow"), prefix="$")
        if pc   == "—" or pc   is None: pc  = safe(prev, prefix="$") if prev else "—"
        if name is None: name = meta.get("shortName") or meta.get("symbol") or sym

        return {
            "ticker":sym, "companyName":name,
            "price":round(float(price),2) if price else None,
            "change":change, "changePct":change_pct, "positive":change>=0,
            "open":op, "high":hi, "low":lo, "prevClose":pc,
            "volume":vol or "—", "marketCap":mktcap or "—",
            "peRatio":pe or "—", "fwdPE":fwdpe or "—", "eps":eps or "—",
            "week52High":w52h or "—", "week52Low":w52l or "—",
            "beta":beta or "—", "dividendYield":divy or "None",
            "analystTarget":target or "—",
            "ma50":ma50, "ma200":ma200, "rsi":rsi,
            "news":get_news(sym),
            "dayChgPct":change_pct,
            "qtrChgPct":round(((closes[-1]-closes[-63])/closes[-63])*100,2) if len(closes)>=63 else None,
        }
    except Exception as e:
        return {"ticker":ticker.upper(),"error":str(e)}

def fetch_index(name,sym):
    try:
        r = SESSION.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}",
            params={"interval":"1d","range":"2d"},timeout=8)
        r.raise_for_status()
        meta = r.json()["chart"]["result"][0]["meta"]
        p    = meta.get("regularMarketPrice")
        prev = meta.get("regularMarketPreviousClose") or meta.get("chartPreviousClose")
        chg  = round(((p-prev)/prev)*100,2) if p and prev else 0
        return {"name":name,"val":f"{p:,.2f}" if p else "—",
                "chg":f"{'+' if chg>=0 else ''}{chg}%","pos":chg>=0}
    except:
        return {"name":name,"val":"—","chg":"—","pos":True}

@app.get("/quote/{ticker}")
async def get_quote(ticker:str):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(executor, fetch_one, ticker.upper())

@app.get("/quotes")
async def get_all_quotes():
    loop = asyncio.get_event_loop()
    sf = [loop.run_in_executor(executor,fetch_one,t) for t in TICKERS]
    ix = [loop.run_in_executor(executor,fetch_index,n,s) for n,s in INDEX_TICKERS.items()]
    sr = await asyncio.gather(*sf, return_exceptions=True)
    ir = await asyncio.gather(*ix, return_exceptions=True)
    results = {}
    for t,r in zip(TICKERS,sr):
        results[t] = r if not isinstance(r,Exception) else {"ticker":t,"error":str(r)}
    results["_markets"] = [r for r in ir if not isinstance(r,Exception)]
    return results

@app.get("/health")
def health():
    try:
        r = SESSION.get("https://query1.finance.yahoo.com/v8/finance/chart/AAPL",
            params={"interval":"1d","range":"2d"},timeout=5)
        meta  = r.json()["chart"]["result"][0]["meta"]
        price = meta.get("regularMarketPrice")
        prev  = meta.get("regularMarketPreviousClose") or meta.get("chartPreviousClose")
        chg   = round(((price-prev)/prev)*100,2) if price and prev else 0
    except Exception as e:
        return {"status":"ok","yahoo_ok":False,"finnhub":bool(FINNHUB_KEY),"error":str(e)}
    return {"status":"ok","yahoo_ok":r.ok,"finnhub":bool(FINNHUB_KEY),
            "aapl_price":price,"aapl_chg_pct":chg,"workers":20}
