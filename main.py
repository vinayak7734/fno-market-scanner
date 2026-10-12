
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from datetime import datetime
from zoneinfo import ZoneInfo

app = FastAPI(title="F&O Market Scanner")

@app.get("/health")
def health():
    return {
        "status": "ok",
        "time": datetime.now(
            ZoneInfo("Asia/Kolkata")
        ).isoformat()
    }

@app.get("/", response_class=HTMLResponse)
def dashboard():
    return """
    <!DOCTYPE html>
    <html>
    <head>
      <meta name="viewport"
            content="width=device-width, initial-scale=1">
      <title>F&O Market Scanner</title>
      <style>
        body {
          font-family: Arial, sans-serif;
          background: #101827;
          color: white;
          padding: 24px;
        }
        h1 { color: #38bdf8; }
        .card {
          background: #1e293b;
          padding: 20px;
          margin: 12px 0;
          border-radius: 12px;
        }
      </style>
    </head>
    <body>
      <h1>F&O Market Scanner</h1>
      <p>Cloud Dashboard - Initial Setup</p>
      <div class="card">Previous Market +/- 5%</div>
      <div class="card">Pre-Market +/- 3%</div>
      <div class="card">Live Market +/- 2%</div>
      <div class="card">Common 3 Stage</div>
      <div class="card">Sector Performance</div>
      <p>Data connection: Not configured yet</p>
    </body>
    </html>
    """
