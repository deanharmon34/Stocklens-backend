from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
import requests, json, pathlib, os, asyncio, math
import pandas as pd
from datetime import datetime, timedelta
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
SCORES_FILE   = pathlib.Path("/tmp/scores.json")
PORT_FILE     = pathlib.Path("/tmp/portfolios.json")

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept":"*/*","Referer":"https://finance.yahoo.com",
})

# ── HELPERS ────────────────────────────────────────────────────────────
def flt(v):
    if v is None: return None
    if isinstance(v, dict): v = v.get("raw") or v.get("fmt")
    try:
        f = float(v)
        return None if f != f else f
    except: return None

def fmt(v, prefix=""):
    f = flt(v)
    if f is None: return "—"
    if abs(f) >= 1e12: return f"{prefix}{f/1e12:.2f}T"
    if abs(f) >= 1e9:  return f"{prefix}{f/1e9:.2f}B"
    if abs(f) >= 1e6:  return f"{prefix}{f/1e6:.2f}M"
    return f"{prefix}{f:,.2f}"

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
    except: return None

# ── SCORING ENGINE ──────────────────────────────────────────────────────
def compute_score(data: dict) -> dict:
    """
    Pure math scoring — no LLM needed.
    Returns score 1-10, verdict, and component breakdown.
    All inputs from live Finnhub + Yahoo data.
    """
    score = 5.0  # baseline
    components = {}

    price  = flt(data.get("price")) or 0
    rsi    = flt(data.get("rsi"))
    ma50   = flt(data.get("ma50"))
    ma200  = flt(data.get("ma200"))
    target = flt(data.get("analystTargetRaw")) or 0
    beta   = flt(data.get("betaRaw"))   or 1.0
    pe     = flt(data.get("peRaw"))
    fwdpe  = flt(data.get("fwdPERaw"))
    eps_g  = flt(data.get("epsGrowth"))       # YoY EPS growth
    rev_g  = flt(data.get("revenueGrowth"))   # YoY revenue growth
    marg   = flt(data.get("grossMargin"))      # gross margin %
    debt   = flt(data.get("debtToEquity"))
    qtr_chg= flt(data.get("qtrChgPct")) or 0

    # ── 1. RSI (momentum signal) — weight 15% ──────────────────────────
    if rsi is not None:
        if rsi < 30:    rsi_s = 8.5   # oversold = buying opportunity
        elif rsi < 45:  rsi_s = 7.0
        elif rsi < 60:  rsi_s = 6.5   # neutral zone
        elif rsi < 70:  rsi_s = 6.0
        elif rsi < 80:  rsi_s = 4.5   # overbought = caution
        else:           rsi_s = 3.0   # very overbought
        components["rsi"] = round(rsi_s, 1)
        score += (rsi_s - 5.0) * 0.15
    else:
        components["rsi"] = None

    # ── 2. Moving average position — weight 20% ────────────────────────
    ma_s = 5.0
    if price and ma50 and ma200:
        above50  = price >= ma50
        above200 = price >= ma200
        pct_above50  = ((price - ma50)  / ma50)  * 100 if ma50  else 0
        pct_above200 = ((price - ma200) / ma200) * 100 if ma200 else 0
        if above50 and above200:
            ma_s = min(9.0, 6.5 + min(pct_above50, 20)/20 * 2.5)
        elif above200 and not above50:
            ma_s = 5.5   # uptrend but short-term weak
        elif above50 and not above200:
            ma_s = 4.5   # recovering but below long-term trend
        else:
            ma_s = max(2.0, 4.0 - min(abs(pct_above200), 20)/20 * 2.0)
    elif price and ma200:
        ma_s = 6.0 if price >= ma200 else 4.0
    components["ma"] = round(ma_s, 1)
    score += (ma_s - 5.0) * 0.20

    # ── 3. Analyst target upside — weight 25% ──────────────────────────
    if target and price and target > 0:
        upside = ((target - price) / price) * 100
        if upside > 40:    tgt_s = 9.5
        elif upside > 25:  tgt_s = 8.5
        elif upside > 15:  tgt_s = 7.5
        elif upside > 5:   tgt_s = 6.0
        elif upside > -5:  tgt_s = 4.5
        elif upside > -15: tgt_s = 3.0
        else:              tgt_s = 1.5
        components["upside"] = round(tgt_s, 1)
        score += (tgt_s - 5.0) * 0.25
    else:
        components["upside"] = None

    # ── 4. Revenue & earnings growth — weight 20% ──────────────────────
    growth_s = 5.0
    if rev_g is not None:
        rev_pct = rev_g * 100 if abs(rev_g) < 10 else rev_g  # handle decimal vs percent
        if rev_pct > 50:   growth_s = 9.5
        elif rev_pct > 30: growth_s = 8.5
        elif rev_pct > 15: growth_s = 7.5
        elif rev_pct > 5:  growth_s = 6.0
        elif rev_pct > 0:  growth_s = 5.0
        elif rev_pct > -10:growth_s = 3.5
        else:              growth_s = 2.0
    elif eps_g is not None:
        eps_pct = eps_g * 100 if abs(eps_g) < 10 else eps_g
        if eps_pct > 50:   growth_s = 9.0
        elif eps_pct > 25: growth_s = 8.0
        elif eps_pct > 10: growth_s = 7.0
        elif eps_pct > 0:  growth_s = 5.5
        else:              growth_s = 3.5
    elif qtr_chg:
        # Fall back to quarterly price change as proxy
        if qtr_chg > 50:   growth_s = 8.0
        elif qtr_chg > 25: growth_s = 7.0
        elif qtr_chg > 10: growth_s = 6.0
        elif qtr_chg > 0:  growth_s = 5.5
        else:              growth_s = 4.0
    components["growth"] = round(growth_s, 1)
    score += (growth_s - 5.0) * 0.20

    # ── 5. Valuation (PE vs sector norm) — weight 20% ──────────────────
    val_s = 5.0
    pe_use = fwdpe or pe
    if pe_use and pe_use > 0:
        if pe_use < 15:    val_s = 8.5   # cheap
        elif pe_use < 25:  val_s = 7.0   # reasonable
        elif pe_use < 40:  val_s = 5.5   # fair for growth
        elif pe_use < 60:  val_s = 4.0   # expensive
        elif pe_use < 100: val_s = 3.0   # very expensive
        else:              val_s = 2.0   # extremely expensive
        # Adjust: high growth justifies high PE
        if growth_s > 7 and pe_use < 80:
            val_s = min(val_s + 1.0, 8.0)
    components["valuation"] = round(val_s, 1)
    score += (val_s - 5.0) * 0.20

    # ── Clamp and round ────────────────────────────────────────────────
    score = round(max(1.0, min(10.0, score)), 1)

    # ── Verdict from score ─────────────────────────────────────────────
    if score >= 8.5:   verdict = "STRONG BUY"
    elif score >= 7.0: verdict = "BUY"
    elif score >= 5.5: verdict = "HOLD"
    elif score >= 4.0: verdict = "CAUTION"
    else:              verdict = "SELL"

    return {
        "score": score,
        "verdict": verdict,
        "components": components,
        "computedAt": datetime.now().isoformat(),
        "inputs": {
            "rsi": rsi, "ma50": ma50, "ma200": ma200,
            "price": price, "target": target,
            "upside": round(((target-price)/price*100),1) if target and price else None,
            "revGrowth": rev_g, "pe": pe_use, "beta": beta,
        }
    }

# ── DATA FETCH ──────────────────────────────────────────────────────────
def get_finnhub(ticker):
    """Fetch fundamentals from Finnhub."""
    if not FINNHUB_KEY: return {}
    out = {}
    try:
        r = requests.get("https://finnhub.io/api/v1/stock/metric",
            params={"symbol":ticker,"metric":"all","token":FINNHUB_KEY},timeout=6)
        if r.ok:
            m = r.json().get("metric",{})
            out["peRaw"]          = m.get("peBasicExclExtraTTM") or m.get("peNormalizedAnnual")
            out["fwdPERaw"]       = m.get("forwardPE")
            out["betaRaw"]        = m.get("beta")
            out["revenueGrowth"]  = m.get("revenueGrowthTTMYoy") or m.get("revenueGrowth5Y")
            out["epsGrowth"]      = m.get("epsGrowthTTMYoy")     or m.get("epsGrowth5Y")
            out["grossMargin"]    = m.get("grossMarginTTM")
            out["debtToEquity"]   = m.get("totalDebt/totalEquityAnnual")
    except: pass
    try:
        r2 = requests.get("https://finnhub.io/api/v1/stock/price-target",
            params={"symbol":ticker,"token":FINNHUB_KEY},timeout=5)
        if r2.ok:
            d = r2.json()
            out["analystTargetRaw"] = d.get("targetMean") or d.get("targetMedian")
    except: pass
    return out

def get_news(ticker):
    if FINNHUB_KEY:
        try:
            today    = datetime.now().strftime("%Y-%m-%d")
            week_ago = (datetime.now()-timedelta(days=7)).strftime("%Y-%m-%d")
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

        # 2-day chart: price + prev close
        r2 = SESSION.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}",
            params={"interval":"1d","range":"2d","includePrePost":"false"},timeout=10)
        r2.raise_for_status()
        d2   = r2.json()["chart"]["result"][0]
        meta = d2["meta"]
        c2   = [c for c in d2.get("indicators",{}).get("quote",[{}])[0].get("close",[]) if c is not None]

        price = meta.get("regularMarketPrice")
        prev  = meta.get("regularMarketPreviousClose") or meta.get("chartPreviousClose") or (c2[-2] if len(c2)>=2 else None)
        change     = round(float(price)-float(prev),2) if price and prev else 0
        change_pct = round((change/float(prev))*100,2)  if prev and float(prev)!=0 else 0

        # OHLC from meta
        open_p = fmt(meta.get("regularMarketOpen"),  "$")
        high_p = fmt(meta.get("regularMarketDayHigh"),"$")
        low_p  = fmt(meta.get("regularMarketDayLow"), "$")
        prev_p = fmt(prev,"$") if prev else "—"
        vol_p  = fmt(meta.get("regularMarketVolume"))
        name   = meta.get("longName") or meta.get("shortName") or sym

        # 52W from meta (always available)
        w52h = fmt(meta.get("fiftyTwoWeekHigh"),"$")
        w52l = fmt(meta.get("fiftyTwoWeekLow"), "$")

        # 1-year chart: MA + RSI + quarterly change
        closes = c2
        ma50 = ma200 = None
        try:
            r1y = SESSION.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}",
                params={"interval":"1d","range":"1y","includePrePost":"false"},timeout=10)
            if r1y.ok:
                c1y = [c for c in r1y.json()["chart"]["result"][0].get("indicators",{}).get("quote",[{}])[0].get("close",[]) if c is not None]
                if c1y: closes = c1y
        except: pass

        if len(closes)>=50:  ma50  = round(float(pd.Series(closes).tail(50).mean()), 2)
        if len(closes)>=200: ma200 = round(float(pd.Series(closes).tail(200).mean()),2)
        rsi_val  = calc_rsi(closes)
        qtr_chg  = round(((closes[-1]-closes[-63])/closes[-63])*100,2) if len(closes)>=63 else None

        # Finnhub fundamentals
        fh = get_finnhub(sym)

        # Format for display
        pe_val   = flt(fh.get("peRaw"));    pe_fmt  = f"{pe_val:.2f}x"    if pe_val else "—"
        fpe_val  = flt(fh.get("fwdPERaw")); fpe_fmt = f"{fpe_val:.2f}x"   if fpe_val else "—"
        beta_val = flt(fh.get("betaRaw"));  beta_p  = f"{beta_val:.2f}"   if beta_val else "—"
        tgt_val  = flt(fh.get("analystTargetRaw")); tgt_p = f"${tgt_val:,.2f}" if tgt_val else "—"
        mktcap   = fmt(meta.get("marketCap"),"$")
        divy_v   = flt(meta.get("dividendYield") or meta.get("trailingAnnualDividendYield"))
        divy_p   = f"{round(divy_v*100,2)}%" if divy_v else "None"

        # Compute score
        score_data = {
            "price": price, "rsi": rsi_val, "ma50": ma50, "ma200": ma200,
            "analystTargetRaw": flt(fh.get("analystTargetRaw")),
            "betaRaw": beta_val, "peRaw": pe_val, "fwdPERaw": fpe_val,
            "revenueGrowth": flt(fh.get("revenueGrowth")),
            "epsGrowth": flt(fh.get("epsGrowth")),
            "grossMargin": flt(fh.get("grossMargin")),
            "qtrChgPct": qtr_chg,
        }
        computed = compute_score(score_data)

        return {
            "ticker":sym, "companyName":name,
            "price":round(float(price),2) if price else None,
            "change":change, "changePct":change_pct, "positive":change>=0,
            "open":open_p, "high":high_p, "low":low_p, "prevClose":prev_p,
            "volume":vol_p, "marketCap":mktcap,
            "peRatio":pe_fmt, "fwdPE":fpe_fmt, "eps":"—",
            "week52High":w52h, "week52Low":w52l,
            "beta":beta_p, "dividendYield":divy_p, "analystTarget":tgt_p,
            "ma50":ma50, "ma200":ma200, "rsi":rsi_val,
            "news":get_news(sym),
            "dayChgPct":change_pct,
            "qtrChgPct":qtr_chg,
            # Computed score — overrides static frontend data
            "computedScore":   computed["score"],
            "computedVerdict": computed["verdict"],
            "scoreComponents": computed["components"],
            "scoreInputs":     computed["inputs"],
            "scoreComputedAt": computed["computedAt"],
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

# ── PORTFOLIO STORAGE ───────────────────────────────────────────────────
def load_portfolios():
    try:
        if PORT_FILE.exists(): return json.loads(PORT_FILE.read_text())
    except: pass
    return {}

def save_portfolios(data):
    try: PORT_FILE.write_text(json.dumps(data)); return True
    except: return False

# ── SCORES STORAGE ──────────────────────────────────────────────────────
def load_scores():
    try:
        if SCORES_FILE.exists(): return json.loads(SCORES_FILE.read_text())
    except: pass
    return {}

def save_scores(data):
    try: SCORES_FILE.write_text(json.dumps(data)); return True
    except: return False

# ── ROUTES ──────────────────────────────────────────────────────────────
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
        results[t] = r if not isinstance(r,Exception) else {"ticker":t,"error":str(r)}
    results["_markets"] = [r for r in ir if not isinstance(r,Exception)]
    return results

@app.get("/scores")
async def get_scores():
    """Return latest computed scores for all tickers."""
    return load_scores()

@app.post("/scores/refresh")
async def refresh_scores():
    """Recompute scores for all tickers. Call on Monday or on demand."""
    loop = asyncio.get_event_loop()
    sf   = [loop.run_in_executor(executor, fetch_one, t) for t in TICKERS]
    results = await asyncio.gather(*sf, return_exceptions=True)
    scores = {}
    for t, r in zip(TICKERS, results):
        if not isinstance(r, Exception) and not r.get("error"):
            scores[t] = {
                "score":          r.get("computedScore"),
                "verdict":        r.get("computedVerdict"),
                "components":     r.get("scoreComponents"),
                "inputs":         r.get("scoreInputs"),
                "updatedAt":      r.get("scoreComputedAt"),
                "price":          r.get("price"),
                "analystTarget":  r.get("analystTarget"),
                "rsi":            r.get("rsi"),
                "ma50":           r.get("ma50"),
                "ma200":          r.get("ma200"),
            }
    save_scores(scores)
    return {"status":"ok","count":len(scores),"updatedAt":datetime.now().isoformat()}

# Portfolio endpoints
@app.get("/portfolios")
def get_portfolios(): return load_portfolios()

@app.get("/portfolios/{name}")
def get_portfolio(name: str): return load_portfolios().get(name, {})

@app.put("/portfolios/{name}")
async def save_portfolio(name: str, request: Request):
    try:
        body = await request.json()
        all_p = load_portfolios()
        all_p[name] = body
        save_portfolios(all_p)
        return {"status":"ok","name":name}
    except Exception as e:
        return {"status":"error","message":str(e)}

@app.delete("/portfolios/{name}")
def delete_portfolio(name: str):
    all_p = load_portfolios()
    if name in all_p:
        del all_p[name]
        save_portfolios(all_p)
    return {"status":"ok"}

@app.get("/health")
def health():
    try:
        r    = SESSION.get("https://query1.finance.yahoo.com/v8/finance/chart/AAPL",
                params={"interval":"1d","range":"2d"},timeout=5)
        meta = r.json()["chart"]["result"][0]["meta"]
        price= meta.get("regularMarketPrice")
        prev = meta.get("regularMarketPreviousClose") or meta.get("chartPreviousClose")
        chg  = round(((price-prev)/prev)*100,2) if price and prev else 0
        # Quick score test
        test = compute_score({"price":price,"rsi":55,"ma50":price*0.95,"ma200":price*0.90,
                               "analystTargetRaw":price*1.15,"peRaw":28,"revenueGrowth":0.15})
        fh_ok = bool(FINNHUB_KEY)
    except Exception as e:
        return {"status":"ok","yahoo_ok":False,"finnhub":bool(FINNHUB_KEY),"error":str(e)}
    scores = load_scores()
    return {
        "status":"ok","yahoo_ok":r.ok,"finnhub":fh_ok,
        "aapl_price":price,"aapl_chg_pct":chg,
        "scoring_engine":"ok","test_score":test["score"],
        "scores_cached":len(scores),
        "workers":20
    }
