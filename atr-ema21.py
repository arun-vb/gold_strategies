"""
backtest_atr_ema21.py  –  Backtest the ATR + EMA21 filtered Logic B strategy on GOLD 5-min.

Strategy logic:
    - Looks for a fat-body candle (body >= 55% of range) followed by a reversal candle.
    - LONG  : fat red candle -> no new low + bullish reversal body above 80% of prior range
    - SHORT : fat green candle -> no new high + bearish reversal body below 80% of prior range
    - ATR filter  : reversal candle body must be >= 60% of ATR14  (drops weak moves)
    - EMA21 filter: LONG only if close > EMA21  |  SHORT only if close < EMA21  (trend alignment)
    - RR 1:3, stop below/above reversal candle

Usage:
    python backtest_atr_ema21.py                        # last 365 days
    python backtest_atr_ema21.py --days 180
    python backtest_atr_ema21.py --days 60 --out my_trades.csv
"""

import sys, argparse, requests, pandas as pd, numpy as np
from datetime import datetime, timezone, timedelta
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# ── Config ────────────────────────────────────────────────────────────────────
API_KEY    = "B4IFjaAEgrcp3fDC"
IDENTIFIER = "mail@arunvb.com"
PASSWORD   = "Test@2025"
BASE_URL   = "https://api-capital.backend-capital.com"
EPIC       = "GOLD"
RESOLUTION = "MINUTE_5"
MAX_BARS   = 1000

RR         = 3.0
MIN_BODY   = 0.55     # min body-to-range ratio for signal candles
ATR_MIN    = 0.60     # reversal body must be >= ATR_MIN * ATR14
OFFSET     = 0.01     # added to stop distance
ATR_LEN        = 14
EMA_LEN        = 21
TIME_EXIT_BARS = 4       # 4 x 5-min bars = 20-minute time exit
DEFAULT_DAYS   = 365

STRATEGY = "ATR+EMA21 Logic B"


# ── API client ────────────────────────────────────────────────────────────────

class Client:
    def __init__(self):
        self.s = requests.Session()
        self.cst = self.tok = None

    def login(self):
        r = self.s.post(
            f"{BASE_URL}/api/v1/session",
            json={"identifier": IDENTIFIER, "password": PASSWORD},
            headers={"X-CAP-API-KEY": API_KEY, "Content-Type": "application/json"},
            timeout=30,
        )
        r.raise_for_status()
        self.cst = r.headers["CST"]
        self.tok = r.headers["X-SECURITY-TOKEN"]

    def _headers(self):
        return {"X-CAP-API-KEY": API_KEY, "CST": self.cst, "X-SECURITY-TOKEN": self.tok}

    def candles_range(self, from_dt, to_dt):
        r = self.s.get(
            f"{BASE_URL}/api/v1/prices/{EPIC}",
            params={
                "resolution": RESOLUTION, "max": MAX_BARS,
                "from": from_dt.strftime("%Y-%m-%dT%H:%M:%S"),
                "to":   to_dt.strftime("%Y-%m-%dT%H:%M:%S"),
            },
            headers=self._headers(),
            timeout=30,
        )
        if r.status_code == 404:
            return pd.DataFrame()
        r.raise_for_status()
        rows = []
        for p in r.json().get("prices", []):
            def mid(k):
                b = p.get(k, {}).get("bid")
                a = p.get(k, {}).get("ask")
                if b is None and a is None: return np.nan
                if b is None: return float(a)
                if a is None: return float(b)
                return (float(b) + float(a)) / 2
            rows.append({
                "time":  p.get("snapshotTimeUTC") or p.get("snapshotTime"),
                "open":  mid("openPrice"), "high": mid("highPrice"),
                "low":   mid("lowPrice"),  "close": mid("closePrice"),
            })
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows)
        df["time"] = pd.to_datetime(df["time"], utc=True, errors="coerce")
        return (df.dropna(subset=["time", "open", "high", "low", "close"])
                  .sort_values("time").drop_duplicates("time").reset_index(drop=True))


def fetch_data(client, days):
    end   = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    frames, cur = [], start
    while cur < end:
        nxt = min(cur + timedelta(days=3), end)
        try:
            chunk = client.candles_range(cur, nxt)
            if not chunk.empty:
                frames.append(chunk)
        except Exception as e:
            print(f"  Batch error: {e}", flush=True)
        cur = nxt
    df = (pd.concat(frames)
            .drop_duplicates("time")
            .sort_values("time")
            .reset_index(drop=True))
    return df[df["time"] >= pd.Timestamp(start)]


# ── Indicators ────────────────────────────────────────────────────────────────

def add_indicators(df):
    df = df.copy()
    c = df["close"]; h = df["high"]; l = df["low"]
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    df["atr14"] = tr.ewm(span=ATR_LEN, adjust=False).mean()
    df["ema21"] = c.ewm(span=EMA_LEN, adjust=False).mean()
    df["date"]  = df["time"].dt.tz_convert("UTC").dt.date
    return df


def body_pct(o, h, l, c):
    rng = h - l
    return 0.0 if rng <= 0 else abs(c - o) / rng


# ── Backtest ──────────────────────────────────────────────────────────────────

def backtest(df):
    trades = []
    open_trade = None

    times = df["time"].values
    dates = df["date"].values
    o   = df["open"].values;   h   = df["high"].values
    l   = df["low"].values;    c   = df["close"].values
    atr = df["atr14"].values;  ema = df["ema21"].values

    for i in range(1, len(df) - 1):

        # ── manage open trade ──────────────────────────────────────────────
        if open_trade:
            side, entry, sl, tp, entry_time, signal_time, signal_date, bars_held = open_trade
            bars_held += 1

            if side == "LONG":
                sl_hit = l[i] <= sl
                tp_hit = h[i] >= tp
            else:
                sl_hit = h[i] >= sl
                tp_hit = l[i] <= tp

            time_exit = bars_held >= TIME_EXIT_BARS and not sl_hit and not tp_hit

            if sl_hit or tp_hit or time_exit:
                if time_exit:
                    exit_p = c[i]
                    reason = "time_exit"
                elif sl_hit:
                    exit_p = sl
                    reason = "stop"
                else:
                    exit_p = tp
                    reason = "target"

                risk  = abs(entry - sl)
                pnl_r = ((exit_p - entry) / risk) if side == "LONG" else ((entry - exit_p) / risk)
                pnl_r = round(pnl_r, 4) if risk > 0 else 0.0

                trades.append({
                    "strategy":    STRATEGY,
                    "date":        str(signal_date),
                    "direction":   side,
                    "signal_time": pd.Timestamp(signal_time),
                    "entry_time":  pd.Timestamp(entry_time),
                    "exit_time":   pd.Timestamp(times[i]),
                    "bars_held":   bars_held,
                    "entry":       round(entry,  4),
                    "stop":        round(sl,     4),
                    "target":      round(tp,     4),
                    "exit_price":  round(exit_p, 4),
                    "risk":        round(risk,   4),
                    "pnl_r":       round(pnl_r,  2),
                    "outcome":     "win" if pnl_r > 0 else "loss",
                    "reason":      reason,
                    "atr14":       round(float(atr[i]), 4),
                    "ema21":       round(float(ema[i]), 4),
                })
                open_trade = None
            else:
                open_trade = (side, entry, sl, tp, entry_time, signal_time, signal_date, bars_held)
            continue

        # ── look for new setup ─────────────────────────────────────────────
        pi = i - 1
        if np.isnan(atr[i]) or np.isnan(ema[i]):
            continue

        p_bp = body_pct(o[pi], h[pi], l[pi], c[pi])
        c_bp = body_pct(o[i],  h[i],  l[i],  c[i])

        # LONG: fat red -> no new low + bullish reversal
        fat_red = (c[pi] < o[pi]) and p_bp >= MIN_BODY
        if fat_red:
            nf         = l[i] >= l[pi]
            bull_entry = l[pi] + 0.80 * (h[pi] - l[pi])
            rev        = (c[i] > o[i]) and c_bp >= MIN_BODY and h[i] >= bull_entry
            if nf and rev:
                if abs(c[i] - o[i]) < ATR_MIN * atr[i]: continue   # ATR filter
                if c[i] < ema[i]:                        continue   # EMA21 filter
                sl   = l[i] - OFFSET
                risk = bull_entry - sl
                if risk > 0:
                    open_trade = ("LONG", bull_entry, sl, bull_entry + RR * risk,
                                  times[i], times[pi], dates[i], 0)
                    continue

        # SHORT: fat green -> no new high + bearish reversal
        fat_grn = (c[pi] > o[pi]) and p_bp >= MIN_BODY
        if fat_grn:
            nf         = h[i] <= h[pi]
            bear_entry = h[pi] - 0.80 * (h[pi] - l[pi])
            rev        = (c[i] < o[i]) and c_bp >= MIN_BODY and l[i] <= bear_entry
            if nf and rev:
                if abs(c[i] - o[i]) < ATR_MIN * atr[i]: continue   # ATR filter
                if c[i] > ema[i]:                        continue   # EMA21 filter
                sl   = h[i] + OFFSET
                risk = sl - bear_entry
                if risk > 0:
                    open_trade = ("SHORT", bear_entry, sl, bear_entry - RR * risk,
                                  times[i], times[pi], dates[i], 0)

    return pd.DataFrame(trades)


# ── Reporting ─────────────────────────────────────────────────────────────────

def report(trades):
    total  = len(trades)
    wins   = int((trades["pnl_r"] > 0).sum())
    losses = total - wins
    wr     = wins / total * 100 if total else 0
    net_r  = trades["pnl_r"].sum()
    avg_r  = trades["pnl_r"].mean()

    equity = trades["pnl_r"].cumsum()
    peak   = equity.cummax()
    dd     = (peak - equity).max()

    gross_w = trades.loc[trades["pnl_r"] > 0, "pnl_r"].sum()
    gross_l = abs(trades.loc[trades["pnl_r"] <= 0, "pnl_r"].sum())
    pf      = gross_w / gross_l if gross_l > 0 else float("inf")

    long_t  = trades[trades["direction"] == "LONG"]
    short_t = trades[trades["direction"] == "SHORT"]

    print()
    print("=" * 44)
    print(f"  BACKTEST RESULTS  |  {STRATEGY}")
    print("=" * 44)
    print(f"  Trades        : {total}")
    print(f"  Wins          : {wins}  ({wr:.1f}%)")
    print(f"  Losses        : {losses}")
    print(f"  Net R         : {net_r:+.1f}R")
    print(f"  Avg R / trade : {avg_r:+.3f}R")
    print(f"  Profit factor : {pf:.2f}")
    print(f"  Max drawdown  : {dd:.1f}R")
    print("-" * 44)
    print(f"  LONG  trades  : {len(long_t)}  "
          f"({int((long_t['pnl_r']>0).sum())}W / {int((long_t['pnl_r']<=0).sum())}L)")
    print(f"  SHORT trades  : {len(short_t)}  "
          f"({int((short_t['pnl_r']>0).sum())}W / {int((short_t['pnl_r']<=0).sum())}L)")
    print("-" * 44)
    target_hits = int((trades["reason"] == "target").sum())
    stop_hits   = int((trades["reason"] == "stop").sum())
    time_exits  = int((trades["reason"] == "time_exit").sum())
    te_win  = int(((trades["reason"] == "time_exit") & (trades["pnl_r"] > 0)).sum())
    te_loss = time_exits - te_win
    print(f"  Target hits   : {target_hits}")
    print(f"  Stop hits     : {stop_hits}")
    print(f"  Time exits    : {time_exits}  ({te_win}W / {te_loss}L  @ 20 min)")
    print("=" * 44)
    print()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Backtest ATR+EMA21 Logic B strategy on GOLD 5-min"
    )
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS,
                        help="Lookback in days (default: %(default)s)")
    parser.add_argument("--out", default="backtest_atr_ema21.csv",
                        help="Output CSV (default: %(default)s)")
    args = parser.parse_args()

    print("Logging in...", flush=True)
    client = Client()
    client.login()

    print(f"Fetching {args.days} days of GOLD 5-min data...", flush=True)
    df = fetch_data(client, args.days)
    df = add_indicators(df)
    print(f"Candles: {len(df):,}  "
          f"({df['time'].min().strftime('%Y-%m-%d')} to "
          f"{df['time'].max().strftime('%Y-%m-%d')})", flush=True)

    print("Running backtest...", flush=True)
    trades = backtest(df)

    if trades.empty:
        print("No trades found.")
        return

    out_path = Path(args.out)
    trades.to_csv(out_path, index=False)
    print(f"Saved {len(trades)} trades to: {out_path}")

    report(trades)


if __name__ == "__main__":
    main()
