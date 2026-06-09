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
    "CEG","ANET","VOO","VST","NOW","IONQ","SMCI","HPE","RGTI","QBTS","HGRAF"
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
})

def fmt(v, prefix=""):
    """Format a number, handle nested Yahoo dicts."""
    if v is None: return "—"
    if isinstance(v, dict): v = v.get("raw") or v.get("fmt")
    if v is None: return "—"
    try:
        f = float(v)
        if f != f: return "—"
        if abs(f) >= 1e12: return f"{prefix}{f/1e12:.2f}T"
        if abs(f) >= 1e9:  return f"{prefix}{f/1e9:.2f}B"
        if abs(f) >= 1e6:  return f"{prefix}{f/1e6:.2f}M"
        return f"{prefix}{f:,.2f}"
    except:
        return str(v) if v else "—"

def flt(v):
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
        gain  = delta.clip(lower=0)
        loss  = -delta.clip(upper=0)
        ag = gain.ewm(com=period-1, min_periods=period).mean()
        al = loss.ewm(com=period-1, min_periods=period).mean()
        rs  = ag / al
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
                items = r.json()[:6]
                if items:
                    return [{"headline":i.get("headline",""),"source":i.get("source",""),
                             "date":datetime.fromtimestamp(i.get("datetime",0)).strftime("%b %d, %Y") if i.get("datetime") else "",
                             "url":i.get("url",""),"sentiment":"neu"} for i in items]
        except: pass
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

        # ── 2-day chart: price, prev close, and all meta fields ──────
        r2 = SESSION.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}",
            params={"interval":"1d","range":"2d","includePrePost":"false",
                    "modules":"defaultKeyStatistics,financialData,summaryDetail"},
            timeout=10)
        r2.raise_for_status()
        d2     = r2.json()["chart"]["result"][0]
        meta   = d2["meta"]
        c2     = [c for c in d2.get("indicators",{}).get("quote",[{}])[0].get("close",[]) if c is not None]

        # Price + daily change
        price = meta.get("regularMarketPrice")
        prev  = meta.get("regularMarketPreviousClose") or meta.get("chartPreviousClose")
        if prev is None and len(c2) >= 2: prev = c2[-2]
        change     = round(float(price) - float(prev), 2) if price and prev else 0
        change_pct = round((change / float(prev)) * 100, 2) if prev and float(prev) != 0 else 0

        # OHLC from meta
        open_p  = fmt(meta.get("regularMarketOpen"),  "$")
        high_p  = fmt(meta.get("regularMarketDayHigh"),"$")
        low_p   = fmt(meta.get("regularMarketDayLow"), "$")
        prev_p  = fmt(prev, "$") if prev else "—"
        vol_p   = fmt(meta.get("regularMarketVolume"))
        name    = meta.get("longName") or meta.get("shortName") or sym

        # ── Fundamentals via Yahoo query2 API ───────────────────────
        pe=fwdpe=eps_p=w52h=w52l=beta_p=mktcap=target_p = "—"
        divy_p = "None"

        def gv(d, *keys):
            for k in keys:
                v = d.get(k)
                if v is not None:
                    if isinstance(v, dict): v = v.get("raw")
                    if v is not None:
                        try: return float(v)
                        except: pass
            return None

        try:
            r_q2 = SESSION.get(
                f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{sym}",
                params={"modules":"summaryDetail,defaultKeyStatistics,financialData,price"},
                timeout=8
            )
            if r_q2.ok:
                res  = r_q2.json()["quoteSummary"]["result"][0]
                sd   = res.get("summaryDetail",{})
                ks   = res.get("defaultKeyStatistics",{})
                fd   = res.get("financialData",{})
                pr   = res.get("price",{})
                pe_v    = gv(sd,"trailingPE") or gv(pr,"trailingPE")
                fwdpe_v = gv(sd,"forwardPE")  or gv(pr,"forwardPE")
                eps_v   = gv(ks,"trailingEps")
                w52h_v  = gv(sd,"fiftyTwoWeekHigh")
                w52l_v  = gv(sd,"fiftyTwoWeekLow")
                beta_v  = gv(sd,"beta")
                mc_v    = gv(pr,"marketCap") or gv(sd,"marketCap")
                dy_v    = gv(sd,"dividendYield") or gv(sd,"trailingAnnualDividendYield")
                tgt_v   = gv(fd,"targetMeanPrice")
                vol_v   = gv(pr,"regularMarketVolume")
                nm      = pr.get("longName") or pr.get("shortName")
                if isinstance(nm, dict): nm = nm.get("raw","")
                if nm and nm != sym: name = str(nm)
                if pe_v:     pe       = f"{pe_v:.2f}x"
                if fwdpe_v:  fwdpe    = f"{fwdpe_v:.2f}x"
                if eps_v:    eps_p    = f"${eps_v:.2f}"
                if w52h_v:   w52h     = f"${w52h_v:,.2f}"
                if w52l_v:   w52l     = f"${w52l_v:,.2f}"
                if beta_v:   beta_p   = f"{beta_v:.2f}"
                if mc_v:     mktcap   = fmt(mc_v,"$")
                if dy_v:     divy_p   = f"{round(dy_v*100,2)}%"
                if tgt_v:    target_p = f"${tgt_v:,.2f}"
                if vol_v and vol_p == "—": vol_p = fmt(vol_v)
        except:
            pass
        # Fallback to v8 meta for 52W range
        if w52h == "—": w52h = fmt(meta.get("fiftyTwoWeekHigh"),"$")
        if w52l == "—": w52l = fmt(meta.get("fiftyTwoWeekLow"), "$")

        # ── 1-year chart: MA + RSI ───────────────────────────────────
        closes = c2
        ma50 = ma200 = None
        try:
            r1y = SESSION.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}",
                params={"interval":"1d","range":"1y","includePrePost":"false"},timeout=10)
            if r1y.ok:
                c1y = [c for c in r1y.json()["chart"]["result"][0].get("indicators",{}).get("quote",[{}])[0].get("close",[]) if c is not None]
                if c1y: closes = c1y
        except: pass
        if len(closes) >= 50:  ma50  = round(float(pd.Series(closes).tail(50).mean()),  2)
        if len(closes) >= 200: ma200 = round(float(pd.Series(closes).tail(200).mean()), 2)
        rsi = calc_rsi(closes)
        qtr = round(((closes[-1]-closes[-63])/closes[-63])*100,2) if len(closes)>=63 else None

        return {
            "ticker":sym, "companyName":name,
            "price":round(float(price),2) if price else None,
            "change":change, "changePct":change_pct, "positive":change>=0,
            "open":open_p, "high":high_p, "low":low_p, "prevClose":prev_p,
            "volume":vol_p, "marketCap":mktcap,
            "peRatio":pe, "fwdPE":fwdpe, "eps":eps_p,
            "week52High":w52h, "week52Low":w52l,
            "beta":beta_p, "dividendYield":divy_p,
            "analystTarget":target_p,
            "ma50":ma50, "ma200":ma200, "rsi":rsi,
            "news":get_news(sym),
            "dayChgPct":change_pct,
            "qtrChgPct":qtr,
        }
    except Exception as e:
        return {"ticker":ticker.upper(),"error":str(e)}

def fetch_index(name, sym):
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
async def get_quote(ticker: str):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(executor, fetch_one, ticker.upper())

@app.get("/quotes")
async def get_all_quotes():
    loop = asyncio.get_event_loop()
    sf = [loop.run_in_executor(executor, fetch_one, t) for t in TICKERS]
    ix = [loop.run_in_executor(executor, fetch_index, n, s) for n,s in INDEX_TICKERS.items()]
    sr = await asyncio.gather(*sf, return_exceptions=True)
    ir = await asyncio.gather(*ix, return_exceptions=True)
    results = {}
    for t, r in zip(TICKERS, sr):
        results[t] = r if not isinstance(r, Exception) else {"ticker":t,"error":str(r)}
    results["_markets"] = [r for r in ir if not isinstance(r, Exception)]
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
        pe = mktcap = None
        try:
            rq = SESSION.get("https://query2.finance.yahoo.com/v10/finance/quoteSummary/AAPL",
                params={"modules":"summaryDetail,price"},timeout=5)
            if rq.ok:
                res = rq.json()["quoteSummary"]["result"][0]
                sd  = res.get("summaryDetail",{})
                pr  = res.get("price",{})
                pe_r  = sd.get("trailingPE");  pe     = pe_r.get("raw")  if isinstance(pe_r,dict)  else pe_r
                mc_r  = pr.get("marketCap");   mktcap = mc_r.get("raw")  if isinstance(mc_r,dict)  else mc_r
        except: pass
    except Exception as e:
        return {"status":"ok","yahoo_ok":False,"finnhub":bool(FINNHUB_KEY),"error":str(e)}
    return {"status":"ok","yahoo_ok":r.ok,"finnhub":bool(FINNHUB_KEY),
            "aapl_price":price,"aapl_chg_pct":chg,
            "aapl_pe":pe,"aapl_mktcap":mktcap,"workers":20}
