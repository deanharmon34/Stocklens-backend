# StockLens Backend

FastAPI + yfinance backend for real-time stock data.

## Deploy to Railway (free)

1. **Create account** at [railway.app](https://railway.app) — sign in with GitHub
2. **New Project → Deploy from GitHub repo**
3. Push this folder to a GitHub repo first:
   ```bash
   git init
   git add .
   git commit -m "StockLens backend"
   # create repo on github.com, then:
   git remote add origin https://github.com/YOUR_USERNAME/stocklens-backend.git
   git push -u origin main
   ```
4. In Railway: **New Project → GitHub Repo → select stocklens-backend**
5. Railway auto-detects Python, installs requirements, deploys
6. Click **Settings → Networking → Generate Domain**
7. Your API URL will be: `https://stocklens-backend-production.up.railway.app`

## Endpoints

- `GET /quote/NVDA` — single ticker
- `GET /quotes` — all 20 tickers at once
- `GET /health` — health check

## Data refresh
- yfinance pulls **live data** from Yahoo Finance on every request
- Railway free tier = 500 hours/month (plenty for personal use)
- Data is as fresh as Yahoo Finance (15-min delay for most stocks)

## Local test
```bash
pip install -r requirements.txt
uvicorn main:app --reload
# visit http://localhost:8000/quote/NVDA
```
