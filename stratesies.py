"""
Full comparison table: all variants x all periods — Win Rate + Net R
GOLD 5-min, RR 1:3, Logic B base with filter combinations
"""
import sys, requests, pandas as pd, numpy as np
from datetime import datetime, timezone, timedelta

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

API_KEY    = "B4IFjaAEgrcp3fDC"
IDENTIFIER = "mail@arunvb.com"
PASSWORD   = "Test@2025"
BASE_URL   = "https://api-capital.backend-capital.com"
EPIC       = "GOLD"
RESOLUTION = "MINUTE_5"
MAX_BARS   = 1000
OFFSET     = 0.01
RR         = 3.0
MIN_BODY   = 0.55
RSI_LEN    = 14; ATR_LEN = 14; EMA_LEN = 21
LONDON_START = 7;  LONDON_END = 10
NY_START     = 13; NY_END     = 17

VARIANTS = [
    ("Base (Logic B)",   False, False, False, False),
    ("+Session only",    True,  False, False, False),
    ("+ATR only",        False, True,  False, False),
    ("+RSI only",        False, False, True,  False),
    ("+EMA21 only",      False, False, False, True ),
    ("+Session+ATR",     True,  True,  False, False),
    ("+Session+RSI",     True,  False, True,  False),
    ("+Session+EMA21",   True,  False, False, True ),
    ("+ATR+RSI",         False, True,  True,  False),
    ("+ATR+EMA21",       False, True,  False, True ),
    ("+RSI+EMA21",       False, False, True,  True ),
    ("All 4 filters",    True,  True,  True,  True ),
]

PERIODS = [
    ("10 Days",  10),
    ("30 Days",  30),
    ("60 Days",  60),
    ("6 Months", 180),
    ("1 Year",   365),
]


class Client:
    def __init__(self):
        self.s = requests.Session(); self.cst = self.tok = None
    def login(self):
        r = self.s.post(f"{BASE_URL}/api/v1/session",
            json={"identifier": IDENTIFIER, "password": PASSWORD},
            headers={"X-CAP-API-KEY": API_KEY, "Content-Type": "application/json"}, timeout=30)
        r.raise_for_status()
        self.cst = r.headers["CST"]; self.tok = r.headers["X-SECURITY-TOKEN"]
    def _h(self): return {"X-CAP-API-KEY": API_KEY, "CST": self.cst, "X-SECURITY-TOKEN": self.tok}
    def candles_range(self, from_dt, to_dt):
        r = self.s.get(f"{BASE_URL}/api/v1/prices/{EPIC}",
            params={"resolution": RESOLUTION, "max": MAX_BARS,
                    "from": from_dt.strftime("%Y-%m-%dT%H:%M:%S"),
                    "to":   to_dt.strftime("%Y-%m-%dT%H:%M:%S")},
            headers=self._h(), timeout=30)
        r.raise_for_status()
        rows = []
        for p in r.json().get("prices", []):
            def mid(k):
                b = p.get(k, {}).get("bid"); a = p.get(k, {}).get("ask")
                if b is None and a is None: return np.nan
                if b is None: return float(a)
                if a is None: return float(b)
                return (float(b)+float(a))/2
            rows.append({"time": p.get("snapshotTimeUTC") or p.get("snapshotTime"),
                         "open": mid("openPrice"), "high": mid("highPrice"),
                         "low":  mid("lowPrice"),  "close": mid("closePrice")})
        if not rows: return pd.DataFrame()
        df = pd.DataFrame(rows)
        df["time"] = pd.to_datetime(df["time"], utc=True, errors="coerce")
        return df.dropna(subset=["time","open","high","low","close"]).sort_values("time").drop_duplicates("time").reset_index(drop=True)


def fetch_all(client):
    end = datetime.now(timezone.utc); start = end - timedelta(days=365)
    frames, cur = [], start
    while cur < end:
        nxt = min(cur + timedelta(days=3), end)
        try:
            chunk = client.candles_range(cur, nxt)
            if not chunk.empty: frames.append(chunk)
        except Exception as e:
            print(f"  Batch error: {e}", flush=True)
        cur = nxt
    df = pd.concat(frames).drop_duplicates("time").sort_values("time").reset_index(drop=True)
    return df[df["time"] >= pd.Timestamp(start)]


def add_indicators(df):
    df = df.copy(); c = df["close"]; h = df["high"]; l = df["low"]
    pc = c.shift(1)
    tr = pd.concat([h-l, (h-pc).abs(), (l-pc).abs()], axis=1).max(axis=1)
    df["atr"]  = tr.ewm(span=ATR_LEN, adjust=False).mean()
    df["ema21"] = c.ewm(span=EMA_LEN, adjust=False).mean()
    delta = c.diff()
    gain  = delta.clip(lower=0).ewm(span=RSI_LEN, adjust=False).mean()
    loss  = (-delta.clip(upper=0)).ewm(span=RSI_LEN, adjust=False).mean()
    rs    = gain / loss.replace(0, np.nan)
    df["rsi"]  = 100 - 100 / (1 + rs)
    df["hour"] = df["time"].dt.hour
    return df


def body_pct(o, h, l, c):
    rng = h - l; return 0.0 if rng <= 0 else abs(c - o) / rng


def run(df, use_session=False, use_atr=False, use_rsi=False, use_ema=False):
    trades = []; open_trade = None
    o = df["open"].values; h = df["high"].values
    l = df["low"].values;  c = df["close"].values
    atr = df["atr"].values; ema = df["ema21"].values
    rsi = df["rsi"].values; hr  = df["hour"].values
    for i in range(1, len(df)-1):
        if open_trade:
            side, entry, sl, tp = open_trade
            if side == "LONG":
                sl_hit = l[i] <= sl; tp_hit = h[i] >= tp
                if   sl_hit and tp_hit: trades.append((side,entry,sl,tp,sl,"stop"));   open_trade=None
                elif sl_hit:            trades.append((side,entry,sl,tp,sl,"stop"));   open_trade=None
                elif tp_hit:            trades.append((side,entry,sl,tp,tp,"target")); open_trade=None
                else: continue
            else:
                sl_hit = h[i] >= sl; tp_hit = l[i] <= tp
                if   sl_hit and tp_hit: trades.append((side,entry,sl,tp,sl,"stop"));   open_trade=None
                elif sl_hit:            trades.append((side,entry,sl,tp,sl,"stop"));   open_trade=None
                elif tp_hit:            trades.append((side,entry,sl,tp,tp,"target")); open_trade=None
                else: continue
        pi = i - 1
        if np.isnan(atr[i]) or np.isnan(ema[i]) or np.isnan(rsi[i]): continue
        in_session = (LONDON_START <= hr[i] < LONDON_END) or (NY_START <= hr[i] < NY_END)
        if use_session and not in_session: continue
        p_bp = body_pct(o[pi],h[pi],l[pi],c[pi]); c_bp = body_pct(o[i],h[i],l[i],c[i])
        fat_red = (c[pi] < o[pi]) and p_bp >= MIN_BODY
        if fat_red:
            nf = l[i] >= l[pi]; bull_entry = l[pi] + 0.80*(h[pi]-l[pi])
            rev = (c[i] > o[i]) and c_bp >= MIN_BODY and h[i] >= bull_entry
            if nf and rev:
                if use_atr and abs(c[i]-o[i]) < 0.6*atr[i]: continue
                if use_rsi and rsi[i] >= 55: continue
                if use_ema and c[i] < ema[i]: continue
                sl = l[i]-OFFSET; risk = bull_entry-sl
                if risk > 0: open_trade = ("LONG", bull_entry, sl, bull_entry+RR*risk); continue
        fat_grn = (c[pi] > o[pi]) and p_bp >= MIN_BODY
        if fat_grn:
            nf = h[i] <= h[pi]; bear_entry = h[pi] - 0.80*(h[pi]-l[pi])
            rev = (c[i] < o[i]) and c_bp >= MIN_BODY and l[i] <= bear_entry
            if nf and rev:
                if use_atr and abs(c[i]-o[i]) < 0.6*atr[i]: continue
                if use_rsi and rsi[i] <= 45: continue
                if use_ema and c[i] > ema[i]: continue
                sl = h[i]+OFFSET; risk = sl-bear_entry
                if risk > 0: open_trade = ("SHORT", bear_entry, sl, bear_entry-RR*risk)
    return trades


def score(trades):
    if not trades: return {"t": 0, "w": 0, "l": 0, "wr": 0.0, "nr": 0.0, "pf": 0.0, "dd": 0.0}
    wins = losses = 0; net_r = eq = peak = max_dd = 0.0
    for side, entry, sl, tp, exit_p, reason in trades:
        risk = (entry-sl) if side == "LONG" else (sl-entry)
        r    = ((exit_p-entry)/risk) if side == "LONG" else ((entry-exit_p)/risk)
        r    = r if risk > 0 else 0
        if r > 0: wins += 1
        else:     losses += 1
        net_r += r; eq += r; peak = max(peak, eq); max_dd = max(max_dd, peak-eq)
    total = wins+losses; wr = wins/total*100 if total else 0
    pf = (wins*RR)/losses if losses > 0 else 0
    return {"t": total, "w": wins, "l": losses, "wr": wr, "nr": net_r, "pf": pf, "dd": max_dd}


def sep(w_name, cols, col_w):
    return "  " + "-"*w_name + "-+-" + ("-"*col_w + "-+-") * (cols-1) + "-"*col_w


def box_table(title, all_res, vlabels, plabels, fmt_fn, metric, best_reverse=False):
    NW = 24   # name column width
    CW = 13   # data column width

    def hline():
        return "+" + ("-"*NW) + ("+" + "-"*CW) * len(plabels) + "+"

    print()
    print(f"  {title}")
    print("  " + hline())
    # Header
    hdr = "|" + f" {'Variant':<{NW-1}}"  + "|"
    for pl in plabels:
        hdr += f" {pl:^{CW-1}}" + "|"
    print("  " + hdr)
    print("  " + hline())
    # Data rows
    for vl in vlabels:
        row = "|" + f" {vl:<{NW-1}}" + "|"
        for pl in plabels:
            r = all_res[pl][vl]
            val = "-" if r["t"] == 0 else fmt_fn(r)
            row += f" {val:^{CW-1}}" + "|"
        print("  " + row)
    # Best row
    print("  " + hline())
    best_row = "|" + f" {'** BEST':<{NW-1}}" + "|"
    for pl in plabels:
        cands = [(all_res[pl][vl][metric], vl) for vl in vlabels if all_res[pl][vl]["t"] > 0]
        bv = (min if best_reverse else max)(cands, key=lambda x: x[0])
        best_row += f" {fmt_fn(all_res[pl][bv[1]]):^{CW-1}}" + "|"
    print("  " + best_row)
    print("  " + hline())


def main():
    print("Logging in...", flush=True)
    client = Client(); client.login()
    print("Fetching GOLD 5-min — 1 year data...", flush=True)
    df_full = fetch_all(client)
    df_full = add_indicators(df_full)
    print(f"Candles: {len(df_full):,}  "
          f"({df_full['time'].min().strftime('%Y-%m-%d')} to "
          f"{df_full['time'].max().strftime('%Y-%m-%d')})\n", flush=True)

    # Compute all results
    all_res = {}
    vlabels = [v[0] for v in VARIANTS]
    plabels = [p[0] for p in PERIODS]

    for pl, days in PERIODS:
        cutoff = df_full["time"].max() - pd.Timedelta(days=days)
        df = df_full[df_full["time"] >= cutoff].reset_index(drop=True)
        all_res[pl] = {}
        for label, sess, atr, rsi, ema in VARIANTS:
            t = run(df, use_session=sess, use_atr=atr, use_rsi=rsi, use_ema=ema)
            all_res[pl][label] = score(t)
        print(f"  Done: {pl}", flush=True)

    # ── TABLE 1: WIN RATE ──────────────────────────────
    box_table(
        "TABLE 1 — WIN RATE %  |  GOLD 5-min  |  RR 1:3  (higher = better)",
        all_res, vlabels, plabels,
        lambda r: f"{r['wr']:.1f}%",
        "wr"
    )

    # ── TABLE 2: NET R ────────────────────────────────
    box_table(
        "TABLE 2 — NET R  |  GOLD 5-min  |  RR 1:3  (higher = better)",
        all_res, vlabels, plabels,
        lambda r: f"{r['nr']:+.0f}R",
        "nr"
    )

    # ── TABLE 3: WIN RATE % / NET R combined ──────────
    box_table(
        "TABLE 3 — WIN RATE % / NET R  |  GOLD 5-min  |  RR 1:3",
        all_res, vlabels, plabels,
        lambda r: f"{r['wr']:.1f}%/{r['nr']:+.0f}R",
        "wr"
    )

    # ── TABLE 4: TRADES / MAX DD ──────────────────────
    box_table(
        "TABLE 4 — TRADES / MAX DRAWDOWN  |  GOLD 5-min  |  RR 1:3  (DD lower = better)",
        all_res, vlabels, plabels,
        lambda r: f"{r['t']}tr/{r['dd']:.0f}R DD",
        "dd",
        best_reverse=True
    )

    # ── SUMMARY ───────────────────────────────────────
    NW = 12; CW2 = 26
    def hline2(): return "+" + ("-"*NW) + ("+" + "-"*CW2) * 2 + "+"

    print()
    print("  SUMMARY -- BEST VARIANT PER PERIOD")
    print("  " + hline2())
    print("  |" + f" {'Period':<{NW-1}}" + "|" +
          f" {'Best WIN RATE variant':<{CW2-1}}" + "|" +
          f" {'Best NET R variant':<{CW2-1}}" + "|")
    print("  " + hline2())
    for pl in plabels:
        scores = {vl: all_res[pl][vl] for vl in vlabels if all_res[pl][vl]["t"] > 0}
        bwr = max(scores, key=lambda v: scores[v]["wr"])
        bnr = max(scores, key=lambda v: scores[v]["nr"])
        wr_cell = bwr + "  " + f"{scores[bwr]['wr']:.1f}%"
        nr_cell = bnr + "  " + f"{scores[bnr]['nr']:+.0f}R"
        print("  |" + f" {pl:<{NW-1}}" + "|" +
              f" {wr_cell:<{CW2-1}}" + "|" +
              f" {nr_cell:<{CW2-1}}" + "|")
    print("  " + hline2())
    print()


if __name__ == "__main__":
    main()
