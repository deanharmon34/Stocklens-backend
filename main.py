from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import yfinance as yf
import json

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

TICKERS = [
    "MU", "AVGO", "MRVL", "NVDA", "DELL", "TSM", "CRWD", "VRT",
    "ARM", "GEV", "ALAB", "QQQ", "MSFT", "PLTR", "ETN", "ANET",
    "VOO", "NOW", "IONQ", "SMCI"
]

def fmt(v, prefix=""):
    if v is None or v != v:  # NaN check
        return "—"
    if isinstance(v, float) or isinstance(v, int):
        if abs(v) >= 1e12: return f"{prefix}{v/1e12:.2f}T"
        if abs(v) >= 1e9:  return f"{prefix}{v/1e9:.2f}B"
        if abs(v) >= 1e6:  return f"{prefix}{v/1e6:.2f}M"
        return f"{prefix}{v:,.2f}"
    return str(v)

@app.get("/quote/{ticker}")
def get_quote(ticker: str):
    try:
        t = yf.Ticker(ticker.upper())
        info = t.info
        hist = t.history(period="1d")
        
        price = info.get("currentPrice") or info.get("regularMarketPrice") or (hist["Close"].iloc[-1] if not hist.empty else None)
        prev = info.get("previousClose") or info.get("regularMarketPreviousClose")
        
        change = round(price - prev, 2) if price and prev else 0
        change_pct = round((change / prev) * 100, 2) if prev else 0
        
        # Moving averages from history
        hist_1y = t.history(period="1y")
        ma50  = round(hist_1y["Close"].tail(50).mean(), 2)  if len(hist_1y) >= 50  else None
        ma200 = round(hist_1y["Close"].tail(200).mean(), 2) if len(hist_1y) >= 200 else None

        return {
            "ticker": ticker.upper(),
            "companyName": info.get("longName") or info.get("shortName", ticker),
            "price": round(price, 2) if price else None,
            "change": change,
            "changePct": change_pct,
            "positive": change >= 0,
            "open": info.get("open") or info.get("regularMarketOpen"),
            "high": info.get("dayHigh") or info.get("regularMarketDayHigh"),
            "low":  info.get("dayLow")  or info.get("regularMarketDayLow"),
            "prevClose": prev,
            "volume": fmt(info.get("volume") or info.get("regularMarketVolume")),
            "marketCap": fmt(info.get("marketCap"), "$"),
            "peRatio": round(info.get("trailingPE"), 2) if info.get("trailingPE") else "—",
            "fwdPE":   round(info.get("forwardPE"),  2) if info.get("forwardPE")  else "—",
            "eps": fmt(info.get("trailingEps"), "$"),
            "week52High": fmt(info.get("fiftyTwoWeekHigh"), "$"),
            "week52Low":  fmt(info.get("fiftyTwoWeekLow"),  "$"),
            "beta": round(info.get("beta"), 2) if info.get("beta") else "—",
            "dividendYield": f"{round(info.get('dividendYield',0)*100,2)}%" if info.get("dividendYield") else "None",
            "analystTarget": fmt(info.get("targetMeanPrice"), "$"),
            "ma50":  ma50,
            "ma200": ma200,
            "fiftyDayAverage":     info.get("fiftyDayAverage"),
            "twoHundredDayAverage": info.get("twoHundredDayAverage"),
        }
    except Exception as e:
        return {"ticker": ticker, "error": str(e)}


@app.get("/quotes")
def get_all_quotes():
    results = {}
    for t in TICKERS:
        results[t] = get_quote(t)
    return results


@app.get("/health")
def health():
    return {"status": "ok"}
