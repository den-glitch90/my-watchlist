#!/usr/bin/env python3
"""
watchlist_monitor.py

Two jobs in one script, meant to run on a schedule (every 6 hours):

  1. ALERTS — compares current numbers against the last run and emails you
     only when something material changed (price move beyond a threshold,
     or a fundamental changed after an earnings report).

  2. LIVE DASHBOARD — rebuilds docs/index.html with the current numbers and
     simple, rule-based commentary, so you can open one bookmarked link on
     your phone and always see the latest data (via GitHub Pages).

Honest limitation: the rule-based commentary below is pattern-matching
against thresholds (margin healthy/thin, debt net-cash/net-debt, etc).
It is NOT the same as a written, reasoned analysis — for that, come back
and ask directly; a script can't replace that judgment.

SETUP (one-time)
-----------------
1. Install dependencies:
       pip install yfinance --break-system-packages

2. For email alerts, set these environment variables (or set them as
   GitHub Actions secrets — see watchlist.yml):
       SENDER_EMAIL, SENDER_APP_PASSWORD, RECIPIENT_EMAIL, SMTP_SERVER

3. Run once locally to check it works:
       python3 watchlist_monitor.py
   This creates watchlist_state.json (for change-detection) and
   docs/index.html (the live dashboard page) next to this script.

4. To view the dashboard on your phone with live updates:
   - Put this script in a GitHub repo (see watchlist.yml for the full
     schedule setup).
   - Turn on GitHub Pages: repo Settings -> Pages -> Source: "Deploy from
     a branch" -> Branch: main, folder: /docs -> Save.
   - GitHub gives you a URL like https://yourusername.github.io/reponame/
   - Open that URL on your phone once and add it to your home screen
     (Share -> Add to Home Screen on iOS, or the browser menu on Android)
     so it behaves like an app icon. Every 6 hours the page content
     refreshes itself on GitHub's end — just reopen it to see current data.
"""

import json
import os
import smtplib
import sys
from datetime import datetime
from email.mime.text import MIMEText

try:
    import yfinance as yf
except ImportError:
    sys.exit(
        "Missing dependency. Run:\n"
        "    pip install yfinance --break-system-packages\n"
        "then try again."
    )

# ---------------------------------------------------------------------------
# EMAIL SETTINGS (see docstring above)
# ---------------------------------------------------------------------------
EMAIL_ENABLED = True
SMTP_SERVER = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SENDER_EMAIL = os.environ.get("SENDER_EMAIL", "your_email@gmail.com")
SENDER_APP_PASSWORD = os.environ.get("SENDER_APP_PASSWORD", "your_app_password_here")
RECIPIENT_EMAIL = os.environ.get("RECIPIENT_EMAIL", "your_email@gmail.com")

PRICE_CHANGE_THRESHOLD = 0.05
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "watchlist_state.json")
DOCS_DIR = os.path.join(BASE_DIR, "docs")
DASHBOARD_FILE = os.path.join(DOCS_DIR, "index.html")

# ---------------------------------------------------------------------------
# WATCHLIST — edit tickers, names, tags, and category here
# ---------------------------------------------------------------------------
COMPANY_META = {
    "NVDA": {"name": "NVIDIA", "tag": "AI accelerator chips", "cat": "mega", "catLabel": "Mega-Cap", "type": "stock"},
    "AMD":  {"name": "Advanced Micro Devices", "tag": "NVIDIA's closest data-center-GPU competitor", "cat": "mega", "catLabel": "Mega-Cap", "type": "stock"},
    "INTC": {"name": "Intel", "tag": "Legacy chip giant mid-turnaround", "cat": "mega", "catLabel": "Mega-Cap", "type": "stock"},
    "VOO":  {"name": "Vanguard S&P 500 ETF", "tag": "Owns all 500 — the market's default core", "cat": "etf", "catLabel": "ETF", "type": "etf"},
    "QQQ":  {"name": "Invesco QQQ Trust", "tag": "The Nasdaq-100 — concentrated growth and tech", "cat": "etf", "catLabel": "ETF", "type": "etf"},
    "SMH":  {"name": "VanEck Semiconductor ETF", "tag": "25 chip stocks in one ticker", "cat": "etf", "catLabel": "ETF", "type": "etf"},
    "CRDO": {"name": "Credo Technology", "tag": "High-speed connectivity chips for AI data centers", "cat": "small", "catLabel": "Small-Cap Growth", "type": "stock"},
    "ASTS": {"name": "AST SpaceMobile", "tag": "Satellites that connect directly to ordinary phones", "cat": "small", "catLabel": "Small-Cap Growth", "type": "stock"},
    "SOUN": {"name": "SoundHound AI", "tag": "Voice and agentic AI", "cat": "small", "catLabel": "Small-Cap Growth", "type": "stock"},
}
WATCHLIST = list(COMPANY_META.keys())


# ---------------------------------------------------------------------------
# FETCHING
# ---------------------------------------------------------------------------
def fetch_snapshot(ticker: str) -> dict:
    meta = COMPANY_META[ticker]
    t = yf.Ticker(ticker)
    info = t.info

    def g(key, default=None):
        return info.get(key, default)

    price = g("currentPrice") or g("regularMarketPrice") or g("previousClose")

    # 1-year return, computed from price history (more reliable across
    # both stocks and ETFs than yfinance's sparse "info" fields)
    one_yr_return = None
    try:
        hist = t.history(period="1y")
        if len(hist) > 5:
            start_price = hist["Close"].iloc[0]
            end_price = hist["Close"].iloc[-1]
            if start_price:
                one_yr_return = (end_price - start_price) / start_price
    except Exception:
        pass

    snapshot = {
        "type": meta["type"],
        "price": price,
        "one_yr_return": one_yr_return,
        "fetched_at": datetime.now().isoformat(timespec="minutes"),
    }

    if meta["type"] == "stock":
        snapshot.update({
            "revenue_growth_pct": g("revenueGrowth"),
            "profit_margin_pct": g("profitMargins"),
            "pe_ratio": g("trailingPE"),
            "free_cash_flow": g("freeCashflow"),
            "total_debt": g("totalDebt"),
            "total_cash": g("totalCash"),
            "total_revenue": g("totalRevenue"),
        })
    else:  # etf
        snapshot.update({
            "expense_ratio": g("annualReportExpenseRatio") or g("netExpenseRatio"),
            "dividend_yield": g("yield"),
            "total_assets": g("totalAssets"),
        })

    return snapshot


# ---------------------------------------------------------------------------
# FORMATTING HELPERS
# ---------------------------------------------------------------------------
def fmt_money(n):
    if n is None:
        return "n/a"
    for unit, div in [("T", 1e12), ("B", 1e9), ("M", 1e6)]:
        if abs(n) >= div:
            return f"${n/div:,.2f}{unit}"
    return f"${n:,.0f}"


def fmt_pct(n):
    return "n/a" if n is None else f"{n*100:.1f}%"


def fmt_price(n):
    return "n/a" if n is None else f"${n:,.2f}"


# ---------------------------------------------------------------------------
# STATE
# ---------------------------------------------------------------------------
def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {}


def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def compare(old: dict, new: dict) -> list:
    changes = []
    if old.get("price") and new.get("price"):
        pct_move = (new["price"] - old["price"]) / old["price"]
        if abs(pct_move) >= PRICE_CHANGE_THRESHOLD:
            direction = "up" if pct_move > 0 else "down"
            changes.append(
                f"Price moved {direction} {abs(pct_move)*100:.1f}% "
                f"(${old['price']:.2f} -> ${new['price']:.2f})"
            )

    if new.get("type") == "stock":
        fields = [
            ("revenue_growth_pct", "Revenue growth", fmt_pct),
            ("profit_margin_pct", "Profit margin", fmt_pct),
            ("pe_ratio", "P/E ratio", lambda v: "n/a" if v is None else f"{v:.1f}x"),
            ("free_cash_flow", "Free cash flow", fmt_money),
            ("total_debt", "Total debt", fmt_money),
            ("total_cash", "Total cash", fmt_money),
        ]
        for field, label, fmt in fields:
            old_val, new_val = old.get(field), new.get(field)
            if old_val != new_val and new_val is not None:
                changes.append(f"{label} changed: {fmt(old_val)} -> {fmt(new_val)}")

    return changes


# ---------------------------------------------------------------------------
# RULE-BASED COMMENTARY (clearly not a substitute for written analysis)
# ---------------------------------------------------------------------------
def classify_stock(snap: dict) -> dict:
    lines = []
    badge, badge_label = "watch", "Mixed Signals"

    rg = snap.get("revenue_growth_pct")
    if rg is not None:
        if rg > 0.5:
            lines.append(f"Revenue growing explosively ({fmt_pct(rg)} YoY).")
        elif rg > 0.15:
            lines.append(f"Revenue growing at a healthy clip ({fmt_pct(rg)} YoY).")
        elif rg >= 0:
            lines.append(f"Revenue growth is modest ({fmt_pct(rg)} YoY).")
        else:
            lines.append(f"Revenue is shrinking ({fmt_pct(rg)} YoY).")

    pm = snap.get("profit_margin_pct")
    if pm is not None:
        if pm > 0.25:
            lines.append(f"Profit margin is strong at {fmt_pct(pm)}.")
        elif pm > 0.10:
            lines.append(f"Profit margin is reasonable at {fmt_pct(pm)}.")
        elif pm >= 0:
            lines.append(f"Profit margin is thin at {fmt_pct(pm)}.")
        else:
            lines.append(f"Currently unprofitable (margin {fmt_pct(pm)}).")
            badge, badge_label = "risk", "Unprofitable"

    debt, cash = snap.get("total_debt"), snap.get("total_cash")
    if debt is not None and cash is not None:
        if cash >= debt:
            lines.append(f"Balance sheet is net cash ({fmt_money(cash)} cash vs {fmt_money(debt)} debt) — a real cushion.")
        else:
            lines.append(f"Balance sheet carries net debt ({fmt_money(debt)} debt vs {fmt_money(cash)} cash) — more exposed to a downturn.")
            if badge != "risk":
                badge, badge_label = "watch", "Carries Net Debt"

    pe = snap.get("pe_ratio")
    if pe is None:
        lines.append("P/E isn't meaningful right now (no positive earnings to divide by).")
    elif pe < 15:
        lines.append(f"P/E of {pe:.1f}x is low relative to the market — either cheap or the market expects trouble.")
    elif pe < 40:
        lines.append(f"P/E of {pe:.1f}x is a fairly normal growth-stock valuation.")
    else:
        lines.append(f"P/E of {pe:.1f}x means a lot of future growth is already priced in.")

    fcf = snap.get("free_cash_flow")
    if fcf is not None:
        if fcf > 0:
            lines.append(f"Generating positive free cash flow ({fmt_money(fcf)}).")
        else:
            lines.append(f"Currently burning cash ({fmt_money(fcf)} free cash flow).")
            if badge == "watch" and badge_label == "Mixed Signals":
                badge, badge_label = "watch", "Cash-Burning"

    if pm is not None and pm > 0.20 and (cash or 0) >= (debt or 0) and rg is not None and rg > 0.15:
        badge, badge_label = "buy", "Healthy On All Fronts"

    return {"badge": badge, "badge_label": badge_label, "lines": lines}


def classify_etf(snap: dict) -> dict:
    lines = []
    badge, badge_label = "watch", "—"

    er = snap.get("expense_ratio")
    if er is not None:
        lines.append(f"Expense ratio of {fmt_pct(er)} — {'low' if er < 0.001*10 else 'moderate' if er < 0.002*10 else 'on the higher side'} for a fund like this.")
    yr = snap.get("one_yr_return")
    if yr is not None:
        lines.append(f"Up {fmt_pct(yr)} over the trailing year." if yr >= 0 else f"Down {fmt_pct(abs(yr))} over the trailing year.")
        badge, badge_label = ("buy", "Strong Trailing Year") if yr > 0.15 else ("watch", "Flat-to-Down Trailing Year") if yr < 0 else ("watch", "Modest Trailing Year")
    dy = snap.get("dividend_yield")
    if dy is not None:
        lines.append(f"Dividend yield around {fmt_pct(dy)}.")

    return {"badge": badge, "badge_label": badge_label, "lines": lines}


def classify(ticker: str, snap: dict) -> dict:
    return classify_stock(snap) if snap.get("type") == "stock" else classify_etf(snap)


# ---------------------------------------------------------------------------
# EMAIL
# ---------------------------------------------------------------------------
def send_email(subject: str, body: str):
    if not EMAIL_ENABLED:
        print(f"\n[EMAIL_ENABLED is False — printing instead]\n{subject}\n{body}")
        return
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = SENDER_EMAIL
    msg["To"] = RECIPIENT_EMAIL
    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
        server.starttls()
        server.login(SENDER_EMAIL, SENDER_APP_PASSWORD)
        server.send_message(msg)


# ---------------------------------------------------------------------------
# DASHBOARD HTML
# ---------------------------------------------------------------------------
PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>The Ledger — Live</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Source+Serif+4:ital,opsz,wght@0,8..60,400;0,8..60,600;0,8..60,700;1,8..60,400&family=IBM+Plex+Mono:wght@400;500;600&display=swap');
:root{{--paper:#EDEAE0;--paper-raised:#F5F3EA;--ink:#1C2321;--ink-soft:#4A5450;--green:#1F4B3F;--green-soft:#DCE6E1;--gold:#8F6A1E;--gold-bg:#EFE5CB;--brick:#8B3226;--brick-bg:#F1DDD7;--line:#C9C2AF;--line-soft:#DAD5C6;}}
*{{box-sizing:border-box;}}
body{{margin:0;background:var(--paper);color:var(--ink);font-family:'Source Serif 4',Georgia,serif;line-height:1.5;-webkit-font-smoothing:antialiased;}}
.mono{{font-family:'IBM Plex Mono',monospace;}}
.wrap{{max-width:860px;margin:0 auto;padding:0 20px 60px;}}
header{{padding:30px 20px 20px;max-width:860px;margin:0 auto;border-bottom:1px solid var(--line);}}
header .kicker{{font-family:'IBM Plex Mono',monospace;font-size:11.5px;color:var(--green);margin-bottom:8px;}}
header h1{{font-size:30px;margin:0 0 6px;}}
header p{{color:var(--ink-soft);font-size:14.5px;margin:0;}}
.row{{border-bottom:1px solid var(--line);padding:16px 4px;}}
.row-top{{display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap;}}
.row-top .tk{{font-family:'IBM Plex Mono',monospace;font-weight:600;font-size:17px;}}
.row-top .nm{{font-size:14.5px;color:var(--ink-soft);}}
.row-top .px{{font-family:'IBM Plex Mono',monospace;font-size:17px;font-weight:600;}}
.badge{{font-family:'IBM Plex Mono',monospace;font-size:10.5px;padding:3px 9px;border-radius:2px;white-space:nowrap;}}
.badge.buy{{background:var(--green-soft);color:var(--green);}}
.badge.watch{{background:var(--gold-bg);color:var(--gold);}}
.badge.risk{{background:var(--brick-bg);color:var(--brick);}}
.commentary{{margin-top:10px;font-size:14px;}}
.commentary li{{margin-bottom:4px;}}
.commentary{{padding-left:18px;}}
.stale{{font-size:11px;color:var(--ink-soft);margin-top:6px;}}
.notice{{background:var(--paper-raised);border:1px solid var(--line);padding:14px 16px;font-size:13.5px;color:var(--ink-soft);margin:20px 0;}}
footer{{margin-top:30px;padding-top:16px;border-top:1px solid var(--line);font-size:12.5px;color:var(--ink-soft);}}
</style>
</head>
<body>
<header>
  <div class="kicker">LIVE WATCHLIST · AUTO-REFRESHED EVERY 6 HOURS · LAST UPDATE {last_update}</div>
  <h1>The Ledger — Live</h1>
  <p>Current numbers and rule-based commentary. Not the same as a reasoned, written analysis — ask directly for that when you want it.</p>
</header>
<div class="wrap">
  <div class="notice">Commentary below is generated from fixed thresholds (margin healthy/thin, debt net-cash/net-debt, etc), not judgment. Treat it as a fast read, not a recommendation.</div>
  {rows}
  <footer>Not financial advice. Data via Yahoo Finance (unofficial), refreshed every 6 hours by an automated job. Verify before acting on anything here.</footer>
</div>
</body>
</html>
"""

ROW_TEMPLATE = """
<div class="row">
  <div class="row-top">
    <div><span class="tk">{ticker}</span> &nbsp; <span class="nm">{name} · {tag}</span></div>
    <div style="display:flex;align-items:center;gap:10px;">
      <span class="px">{price}</span>
      <span class="badge {badge}">{badge_label}</span>
    </div>
  </div>
  <ul class="commentary">{commentary_items}</ul>
  <div class="stale">Fetched {fetched_at}</div>
</div>
"""


def build_dashboard(state: dict):
    rows_html = []
    for ticker in WATCHLIST:
        snap = state.get(ticker)
        meta = COMPANY_META[ticker]
        if not snap:
            continue
        result = classify(ticker, snap)
        items = "".join(f"<li>{line}</li>" for line in result["lines"])
        rows_html.append(ROW_TEMPLATE.format(
            ticker=ticker,
            name=meta["name"],
            tag=meta["tag"],
            price=fmt_price(snap.get("price")),
            badge=result["badge"],
            badge_label=result["badge_label"],
            commentary_items=items,
            fetched_at=snap.get("fetched_at", "n/a"),
        ))

    html = PAGE_TEMPLATE.format(
        last_update=datetime.now().strftime("%Y-%m-%d %H:%M"),
        rows="".join(rows_html),
    )

    os.makedirs(DOCS_DIR, exist_ok=True)
    with open(DASHBOARD_FILE, "w") as f:
        f.write(html)


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def main():
    old_state = load_state()
    new_state = {}
    all_changes = {}

    for ticker in WATCHLIST:
        try:
            snapshot = fetch_snapshot(ticker)
        except Exception as e:
            print(f"[{ticker}] fetch failed: {e}")
            continue

        new_state[ticker] = snapshot
        old_snapshot = old_state.get(ticker)
        if old_snapshot:
            changes = compare(old_snapshot, snapshot)
            if changes:
                all_changes[ticker] = changes

    save_state(new_state)
    build_dashboard(new_state)
    print(f"Dashboard written to {DASHBOARD_FILE}")

    if not old_state:
        print("First run complete — baseline saved. Alerts will start from the next run.")
        return

    if all_changes:
        lines = [f"Watchlist check — {datetime.now().strftime('%Y-%m-%d %H:%M')}", ""]
        for ticker, changes in all_changes.items():
            lines.append(f"{ticker}:")
            for c in changes:
                lines.append(f"  - {c}")
            lines.append("")
        body = "\n".join(lines)
        send_email(f"Watchlist alert: {', '.join(all_changes.keys())} moved", body)
        print(body)
    else:
        print(f"{datetime.now().strftime('%Y-%m-%d %H:%M')} — checked {len(WATCHLIST)} tickers, nothing crossed the threshold.")


if __name__ == "__main__":
    main()
