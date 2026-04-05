"""
180 Trader — Paper + Live
- Paper trades $5K simulated balance on non-live instruments
- Live trades $200 real Capital.com account on GOLD only
- Only 1 active trade at a time per mode
- Flask dashboard at http://localhost:8080 with two tabs
- Data persisted to paper_trade_state.json and live_trade_state.json
"""

import json
import threading
import time
import warnings
warnings.filterwarnings("ignore")
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests
from flask import Flask, jsonify, render_template_string, request as flask_request

# ══════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════
PAGERDUTY_KEY = "5d2b91b88ebf4405c056b3a7ffd94ecf"

API_KEY      = "B4IFjaAEgrcp3fDC"
IDENTIFIER   = "mail@arunvb.com"
API_PASSWORD = "Test@2025"
USE_DEMO     = False
RESOLUTION   = "MINUTE_5"

# Tradeable instruments in priority order. GOLD is reserved for live trading.
INSTRUMENTS = [
    # Priority order — GOLD first for live, then paper instruments by backtest performance
    {"epic": "GOLD",  "offset": 0.01,   "spread_fallback": 0.30,    "price_dp": 2, "min_size": 0.01,  "size_step": 0.01},
    {"epic": "US500", "offset": 0.1,    "spread_fallback": 0.5,     "price_dp": 1, "min_size": 0.01,  "size_step": 0.01},
    {"epic": "US100", "offset": 0.1,    "spread_fallback": 1.0,     "price_dp": 1, "min_size": 0.001, "size_step": 0.001},
    {"epic": "TSLA",  "offset": 0.01,   "spread_fallback": 0.05,    "price_dp": 2, "min_size": 0.1,   "size_step": 0.1},
    {"epic": "US30",  "offset": 1.0,    "spread_fallback": 2.0,     "price_dp": 1, "min_size": 0.001, "size_step": 0.001},
    {"epic": "META",  "offset": 0.01,   "spread_fallback": 0.05,    "price_dp": 2, "min_size": 0.01,  "size_step": 0.01},
    {"epic": "MSFT",  "offset": 0.01,   "spread_fallback": 0.05,    "price_dp": 2, "min_size": 0.01,  "size_step": 0.01},
    {"epic": "NVDA",  "offset": 0.01,   "spread_fallback": 0.05,    "price_dp": 2, "min_size": 0.1,   "size_step": 0.1},
    {"epic": "GOOGL", "offset": 0.01,   "spread_fallback": 0.05,    "price_dp": 2, "min_size": 0.1,   "size_step": 0.1},
    {"epic": "AMZN",  "offset": 0.01,   "spread_fallback": 0.05,    "price_dp": 2, "min_size": 0.1,   "size_step": 0.1},
]
INST_MAP = {i["epic"]: i for i in INSTRUMENTS}

# Live trading restricted to GOLD only
LIVE_INSTRUMENTS = [i for i in INSTRUMENTS if i["epic"] == "GOLD"]
PAPER_INSTRUMENTS = [i for i in INSTRUMENTS if i["epic"] != "GOLD"]

# Paper trading
PAPER_INITIAL  = 5000.0
PAPER_RISK_PCT = 0.005
PAPER_LEVERAGE = 5

# Live trading
LIVE_INITIAL   = 200.0
LIVE_RISK_PCT  = 0.005
LIVE_MIN_SIZE  = 0.01
LIVE_SIZE_STEP = 0.01

POLL_INTERVAL = 310

# Best strategy params
MIN_BODY_PCT    = 0.55
RR_TARGET       = 3.0
USE_80PCT_ENTRY = True
NO_FOLLOW_THRU  = True
USE_TREND       = False
MAX_TRADES_DAY  = 10

PAPER_STATE_FILE    = Path("paper_trade_state.json")
LIVE_STATE_FILE     = Path("live_trade_state.json")
SETTINGS_FILE       = Path("trader_settings.json")

def load_settings():
    defaults = {"live_risk_pct": LIVE_RISK_PCT, "paper_risk_pct": PAPER_RISK_PCT}
    if SETTINGS_FILE.exists():
        try:
            saved = json.loads(SETTINGS_FILE.read_text())
            defaults.update(saved)
        except Exception:
            pass
    return defaults

def save_settings(s):
    SETTINGS_FILE.write_text(json.dumps(s, indent=2))

_settings      = load_settings()
_settings_lock = threading.Lock()


# ══════════════════════════════════════════════════════
# PAGERDUTY NOTIFICATIONS
# ══════════════════════════════════════════════════════
def notify(summary, severity="info", details=None, epic="GOLD"):
    """Fire a PagerDuty event. Runs in a background thread so it never blocks trading."""
    def _send():
        try:
            requests.post(
                "https://events.pagerduty.com/v2/enqueue",
                json={
                    "routing_key": PAGERDUTY_KEY,
                    "event_action": "trigger",
                    "payload": {
                        "summary": summary,
                        "severity": severity,
                        "source": f"180-trader-{epic}",
                        "custom_details": details or {},
                    },
                },
                timeout=10,
            )
        except Exception as e:
            print(f"[PagerDuty] Failed: {e}", flush=True)
    threading.Thread(target=_send, daemon=True).start()


# ══════════════════════════════════════════════════════
# CAPITAL.COM CLIENT
# ══════════════════════════════════════════════════════
class Client:
    def __init__(self):
        self.base = ("https://demo-api-capital.backend-capital.com" if USE_DEMO
                     else "https://api-capital.backend-capital.com")
        self.cst = self.tok = None

    def login(self):
        r = requests.post(
            f"{self.base}/api/v1/session",
            json={"identifier": IDENTIFIER, "password": API_PASSWORD,
                  "encryptedPassword": False},
            headers={"X-CAP-API-KEY": API_KEY, "Content-Type": "application/json"},
            timeout=30)
        r.raise_for_status()
        self.cst = r.headers["CST"]
        self.tok = r.headers["X-SECURITY-TOKEN"]

    def _h(self):
        return {"X-CAP-API-KEY": API_KEY, "CST": self.cst,
                "X-SECURITY-TOKEN": self.tok}

    def candles(self, epic, n=250):
        r = requests.get(f"{self.base}/api/v1/prices/{epic}",
                         params={"resolution": RESOLUTION, "max": n},
                         headers=self._h(), timeout=30)
        r.raise_for_status()
        rows = []
        for p in r.json().get("prices", []):
            def mid(key):
                bid = p.get(key, {}).get("bid")
                ask = p.get(key, {}).get("ask")
                if bid is None and ask is None: return np.nan
                if bid is None: return float(ask)
                if ask is None: return float(bid)
                return (float(bid) + float(ask)) / 2
            rows.append({
                "time":  p.get("snapshotTimeUTC") or p.get("snapshotTime"),
                "open":  mid("openPrice"),
                "high":  mid("highPrice"),
                "low":   mid("lowPrice"),
                "close": mid("closePrice"),
            })
        df = pd.DataFrame(rows)
        df["time"] = pd.to_datetime(df["time"], utc=True, errors="coerce")
        return df.dropna(subset=["time"]).sort_values("time").reset_index(drop=True)

    def get_spread(self, epic, fallback=0.0):
        """Return current bid/ask spread for epic."""
        try:
            r = requests.get(f"{self.base}/api/v1/markets/{epic}",
                             headers=self._h(), timeout=10)
            r.raise_for_status()
            snap = r.json().get("snapshot", {})
            bid   = snap.get("bid")
            offer = snap.get("offer")
            if bid and offer:
                return float(offer) - float(bid)
        except Exception:
            pass
        return fallback

    def open_position(self, epic, direction, size, stop_level, profit_level,
                      spread_fallback=0.30, price_dp=2):
        """
        Adjust SL/TP for bid/ask spread so Capital.com triggers at the
        same mid-price level our strategy intends.
        """
        spread      = self.get_spread(epic, fallback=spread_fallback)
        half_spread = spread / 2

        if direction == "BUY":
            adj_stop   = round(stop_level   + half_spread, price_dp)
            adj_profit = round(profit_level - half_spread, price_dp)
        else:
            adj_stop   = round(stop_level   - half_spread, price_dp)
            adj_profit = round(profit_level + half_spread, price_dp)

        payload = {
            "epic": epic,
            "direction": direction,
            "size": size,
            "guaranteedStop": False,
            "stopLevel": adj_stop,
            "profitLevel": adj_profit,
        }
        r = requests.post(f"{self.base}/api/v1/positions",
                          json=payload, headers=self._h(), timeout=30)
        r.raise_for_status()
        return r.json(), spread

    def get_positions(self):
        r = requests.get(f"{self.base}/api/v1/positions",
                         headers=self._h(), timeout=30)
        r.raise_for_status()
        return r.json().get("positions", [])

    def close_position(self, deal_id):
        r = requests.delete(f"{self.base}/api/v1/positions/{deal_id}",
                            headers=self._h(), timeout=30)
        r.raise_for_status()
        return r.json()

    def get_activity(self, from_iso):
        r = requests.get(f"{self.base}/api/v1/history/activity",
                         params={"from": str(from_iso)[:19].replace(" ", "T"),
                                 "detailed": "true"},
                         headers=self._h(), timeout=30)
        r.raise_for_status()
        return r.json().get("activities", [])

    def get_close_details(self, deal_id, from_iso):
        """Return (exit_price, exit_reason, actual_entry, size) from activity history."""
        try:
            activities = self.get_activity(from_iso)
            open_price = None
            close_price = close_reason = None
            close_size = None
            for act in activities:
                if act.get("dealId") != deal_id or act.get("type") != "POSITION":
                    continue
                source  = act.get("source", "")
                details = act.get("details", {})
                level   = details.get("level")
                if source == "USER" and details.get("openPrice") is None and level:
                    open_price = float(level)
                if source in ("TP", "SL") and level:
                    close_price  = float(level)
                    close_reason = "target" if source == "TP" else "stop"
                    close_size   = details.get("size")
            return close_price, close_reason, open_price, close_size
        except Exception:
            return None, None, None, None


# ══════════════════════════════════════════════════════
# STATE
# ══════════════════════════════════════════════════════
def default_paper_state():
    return {"balance": PAPER_INITIAL, "initial_balance": PAPER_INITIAL,
            "open_trade": None, "trades": [], "day_counts": {},
            "last_poll": None, "last_price": None, "last_prices": {},
            "status": "Starting", "log": []}

def default_live_state():
    return {"balance": LIVE_INITIAL, "initial_balance": LIVE_INITIAL,
            "open_trade": None, "trades": [], "day_counts": {},
            "last_poll": None, "last_price": None, "last_prices": {},
            "status": "Starting", "log": []}

def load_json(path, default_fn):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return default_fn()

def save_json(path, state):
    path.write_text(json.dumps(state, indent=2, default=str))


# ══════════════════════════════════════════════════════
# STRATEGY — shared signal detection
# ══════════════════════════════════════════════════════
def body_pct(o, h, l, c):
    rng = h - l
    return 0.0 if rng <= 0 else abs(c - o) / rng

def check_signal(df, offset, price_dp):
    if len(df) < 202:
        return None
    closes = df["close"].values
    ma20   = pd.Series(closes).rolling(20).mean().values
    ma200  = pd.Series(closes).rolling(200).mean().values
    pi, ci = len(df) - 2, len(df) - 1
    prev, curr = df.iloc[pi], df.iloc[ci]
    m20, m200 = ma20[ci], ma200[ci]
    if np.isnan(m20) or np.isnan(m200):
        return None

    trend_bull = (not USE_TREND) or (curr["close"] > m200)
    trend_bear = (not USE_TREND) or (curr["close"] < m200)
    p_bp = body_pct(prev["open"], prev["high"], prev["low"], prev["close"])
    c_bp = body_pct(curr["open"], curr["high"], curr["low"], curr["close"])
    fat_red   = (prev["close"] < prev["open"]) and p_bp >= MIN_BODY_PCT
    fat_green = (prev["close"] > prev["open"]) and p_bp >= MIN_BODY_PCT

    # Bull 180
    if fat_red and trend_bull:
        nf  = (not NO_FOLLOW_THRU) or (curr["low"] >= prev["low"])
        be  = prev["low"] + 0.80 * (prev["high"] - prev["low"])
        rev = (curr["close"] > curr["open"]) and c_bp >= MIN_BODY_PCT and curr["high"] >= be
        if nf and rev:
            stop = curr["low"] - offset
            risk = be - stop
            if risk > 0:
                entry  = round(be, price_dp)
                stop   = round(stop, price_dp)
                target = round(entry + RR_TARGET * risk, price_dp)
                return {"side": "LONG", "entry": entry, "stop": stop,
                        "target": target, "risk": round(risk, price_dp + 2),
                        "candle_time": str(curr["time"])}

    # Bear 180
    if fat_green and trend_bear:
        nf  = (not NO_FOLLOW_THRU) or (curr["high"] <= prev["high"])
        be  = prev["high"] - 0.80 * (prev["high"] - prev["low"])
        rev = (curr["close"] < curr["open"]) and c_bp >= MIN_BODY_PCT and curr["low"] <= be
        if nf and rev:
            stop = curr["high"] + offset
            risk = stop - be
            if risk > 0:
                entry  = round(be, price_dp)
                stop   = round(stop, price_dp)
                target = round(entry - RR_TARGET * risk, price_dp)
                return {"side": "SHORT", "entry": entry, "stop": stop,
                        "target": target, "risk": round(risk, price_dp + 2),
                        "candle_time": str(curr["time"])}
    return None

def check_exit(ot, high, low, pessimistic=False):
    """
    pessimistic=True (paper mode): if both SL and TP are within the candle range,
    assume stop was hit first — mirrors real-world worst-case behaviour.
    """
    if ot["side"] == "LONG":
        sl_hit = low  <= ot["stop"]
        tp_hit = high >= ot["target"]
        if pessimistic and sl_hit and tp_hit:
            return ot["stop"], "stop"
        if sl_hit:  return ot["stop"],   "stop"
        if tp_hit:  return ot["target"], "target"
    else:
        sl_hit = high >= ot["stop"]
        tp_hit = low  <= ot["target"]
        if pessimistic and sl_hit and tp_hit:
            return ot["stop"], "stop"
        if sl_hit:  return ot["stop"],   "stop"
        if tp_hit:  return ot["target"], "target"
    return None, None

def day_key(ts_str):
    try:
        ts = pd.Timestamp(ts_str)
        if ts.tzinfo is None: ts = ts.tz_localize("UTC")
        return ts.tz_convert("UTC").strftime("%Y-%m-%d")
    except Exception:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

def calc_live_size(risk_amount, stop_distance, min_size=LIVE_MIN_SIZE, size_step=LIVE_SIZE_STEP):
    if stop_distance <= 0:
        return None
    raw  = risk_amount / stop_distance
    size = max(round(round(raw / size_step) * size_step, 2), min_size)
    return size


# ══════════════════════════════════════════════════════
# SHARED STATE + LOCKS
# ══════════════════════════════════════════════════════
client      = Client()
_paper_lock = threading.Lock()
_live_lock  = threading.Lock()
_log_lock   = threading.Lock()

_paper = load_json(PAPER_STATE_FILE, default_paper_state)
_live  = load_json(LIVE_STATE_FILE,  default_live_state)


def log_paper(msg):
    ts    = datetime.now(timezone.utc).strftime("%H:%M:%S")
    entry = f"[{ts}] {msg}"
    print(f"[PAPER] {entry}", flush=True)
    with _log_lock:
        _paper["log"].insert(0, entry)
        _paper["log"] = _paper["log"][:100]

def log_live(msg):
    ts    = datetime.now(timezone.utc).strftime("%H:%M:%S")
    entry = f"[{ts}] {msg}"
    print(f"[LIVE]  {entry}", flush=True)
    with _log_lock:
        _live["log"].insert(0, entry)
        _live["log"] = _live["log"][:100]


# ══════════════════════════════════════════════════════
# PAPER TRADING LOGIC
# ══════════════════════════════════════════════════════
def paper_poll_multi(candles_by_epic, now_str):
    gold_df    = candles_by_epic.get("GOLD")
    last_price = float(gold_df["close"].iloc[-1]) if gold_df is not None else 0.0
    last_prices = {epic: float(df["close"].iloc[-1]) for epic, df in candles_by_epic.items()}

    with _settings_lock:
        paper_risk_pct = _settings["paper_risk_pct"]

    with _paper_lock:
        ot         = _paper.get("open_trade")
        balance    = _paper["balance"]
        day_counts = dict(_paper["day_counts"])

    close_update = open_update = None

    # ── Check exit for the currently open trade ───────────────────────────
    if ot:
        epic = ot.get("epic", "GOLD")
        df   = candles_by_epic.get(epic)
        if df is not None:
            curr = df.iloc[-1]
            xp, xr = check_exit(ot, curr["high"], curr["low"], pessimistic=True)
            if xr:
                risk_usd = balance * paper_risk_pct
                r_mult   = ((xp - ot["entry"]) / ot["risk"] if ot["side"] == "LONG"
                            else (ot["entry"] - xp) / ot["risk"])
                pnl      = round(risk_usd * r_mult, 2)
                new_bal  = round(balance + pnl, 2)
                closed   = {**ot, "exit_price": xp, "exit_reason": xr,
                            "exit_time": str(curr["time"]),
                            "r_multiple": round(r_mult, 3),
                            "pnl": pnl, "balance_after": new_bal}
                close_update = (closed, new_bal)

    # ── Scan instruments in priority order for a new signal ───────────────
    if not ot or close_update:
        bal_for_signal = close_update[1] if close_update else balance
        for inst in PAPER_INSTRUMENTS:
            df = candles_by_epic.get(inst["epic"])
            if df is None:
                continue
            signal = check_signal(df, inst["offset"], inst["price_dp"])
            if signal:
                dk = day_key(signal["candle_time"])
                if day_counts.get(dk, 0) < MAX_TRADES_DAY:
                    risk_usd = round(bal_for_signal * paper_risk_pct, 2)
                    trade    = {**signal,
                                "epic": inst["epic"],
                                "open_time": now_str,
                                "risk_dollars": risk_usd,
                                "effective_capital": bal_for_signal * PAPER_LEVERAGE}
                    open_update = (trade, dk)
                    break  # one trade at a time; first paper instrument wins

    # ── Write state ───────────────────────────────────────────────────────
    with _paper_lock:
        _paper["last_poll"]   = now_str
        _paper["last_price"]  = last_price
        _paper["last_prices"] = last_prices
        _paper["status"]      = "Live"
        if close_update:
            closed, new_bal = close_update
            _paper["balance"] = new_bal
            _paper["trades"].insert(0, closed)
            _paper["open_trade"] = None
        if open_update:
            trade, dk = open_update
            _paper["open_trade"] = trade
            _paper["day_counts"][dk] = _paper["day_counts"].get(dk, 0) + 1
        snap = json.loads(json.dumps(_paper, default=str))

    save_json(PAPER_STATE_FILE, snap)

    if close_update:
        c, nb = close_update
        epic  = c.get("epic", "GOLD")
        log_paper(f"CLOSED {epic} {c['side']} @ {c['exit_price']}  "
                  f"R={c['r_multiple']:.2f}  PnL=${c['pnl']:+.2f}  Bal=${nb:.2f}")
    if open_update:
        t    = open_update[0]
        epic = t["epic"]
        log_paper(f"OPENED {epic} {t['side']} @ {t['entry']}  "
                  f"SL={t['stop']}  TP={t['target']}  Risk=${t['risk_dollars']:.2f}")
    if not close_update and not open_update:
        checked = ", ".join(i["epic"] for i in PAPER_INSTRUMENTS if i["epic"] in candles_by_epic)
        log_paper(f"No action. Paper scanned: {checked}. GOLD={last_price:.2f}")


# ══════════════════════════════════════════════════════
# LIVE TRADING LOGIC
# ══════════════════════════════════════════════════════
def live_poll_multi(candles_by_epic, now_str):
    gold_df    = candles_by_epic.get("GOLD")
    last_price = float(gold_df["close"].iloc[-1]) if gold_df is not None else 0.0
    last_prices = {epic: float(df["close"].iloc[-1]) for epic, df in candles_by_epic.items()}

    with _settings_lock:
        live_risk_pct = _settings["live_risk_pct"]

    with _live_lock:
        ot         = _live.get("open_trade")
        balance    = _live["balance"]
        day_counts = dict(_live["day_counts"])

    close_update = open_update = None

    # ── Check if open trade is still live on Capital.com ─────────────────
    if ot:
        try:
            positions = client.get_positions()
            deal_ids  = {p["position"]["dealId"] for p in positions}
            if ot.get("deal_id") not in deal_ids:
                xp, xr, actual_entry, actual_size = client.get_close_details(
                    ot["deal_id"], ot["open_time"])

                if xp is None:
                    epic      = ot.get("epic", "GOLD")
                    df        = candles_by_epic.get(epic)
                    cur_price = float(df["close"].iloc[-1]) if df is not None else last_price
                    xp = ot["target"] if (
                        (ot["side"] == "LONG"  and cur_price >= ot["target"]) or
                        (ot["side"] == "SHORT" and cur_price <= ot["target"])
                    ) else ot["stop"]
                    xr = "target" if xp == ot["target"] else "stop"

                entry = actual_entry or ot.get("actual_entry") or ot["entry"]
                size  = actual_size  or ot["size"]

                if ot["side"] == "LONG":
                    pnl    = round(size * (xp - entry), 2)
                    r_mult = (xp - entry) / ot["risk"]
                else:
                    pnl    = round(size * (entry - xp), 2)
                    r_mult = (entry - xp) / ot["risk"]

                new_bal = round(balance + pnl, 2)
                closed  = {**ot,
                           "actual_entry": round(entry, 4),
                           "exit_price": xp, "exit_reason": xr,
                           "exit_time": now_str,
                           "r_multiple": round(r_mult, 3),
                           "pnl": pnl, "balance_after": new_bal}
                close_update = (closed, new_bal)
        except Exception as e:
            log_live(f"Position check error: {e}")

    # ── Scan GOLD only for live signals ──────────────────────────────────
    if not ot or close_update:
        bal_for_signal = close_update[1] if close_update else balance
        for inst in LIVE_INSTRUMENTS:
            df = candles_by_epic.get(inst["epic"])
            if df is None:
                continue
            signal = check_signal(df, inst["offset"], inst["price_dp"])
            if signal:
                dk = day_key(signal["candle_time"])
                if day_counts.get(dk, 0) < MAX_TRADES_DAY:
                    risk_usd  = round(bal_for_signal * live_risk_pct, 4)
                    size      = calc_live_size(risk_usd, signal["risk"],
                                               inst["min_size"], inst["size_step"])
                    direction = "BUY" if signal["side"] == "LONG" else "SELL"
                    try:
                        resp, spread = client.open_position(
                            inst["epic"], direction, size,
                            signal["stop"], signal["target"],
                            spread_fallback=inst["spread_fallback"],
                            price_dp=inst["price_dp"])
                        deal_id = resp.get("dealReference") or resp.get("dealId", "unknown")
                        time.sleep(1)
                        positions    = client.get_positions()
                        actual_entry = None
                        for p in positions:
                            if (p["market"]["epic"] == inst["epic"] and
                                    p["position"]["direction"] == direction):
                                deal_id      = p["position"]["dealId"]
                                actual_entry = float(p["position"]["level"])
                                break
                        trade = {**signal,
                                 "epic": inst["epic"],
                                 "open_time": now_str,
                                 "risk_dollars": risk_usd,
                                 "size": size,
                                 "deal_id": deal_id,
                                 "spread_at_entry": spread,
                                 "actual_entry": actual_entry}
                        open_update = (trade, dk)
                        break  # one trade at a time
                    except Exception as e:
                        log_live(f"Order failed ({inst['epic']}): {e}")

    # ── Write state ───────────────────────────────────────────────────────
    with _live_lock:
        _live["last_poll"]   = now_str
        _live["last_price"]  = last_price
        _live["last_prices"] = last_prices
        _live["status"]      = "Live"
        if close_update:
            closed, new_bal = close_update
            _live["balance"] = new_bal
            _live["trades"].insert(0, closed)
            _live["open_trade"] = None
        if open_update:
            trade, dk = open_update
            _live["open_trade"] = trade
            _live["day_counts"][dk] = _live["day_counts"].get(dk, 0) + 1
        snap = json.loads(json.dumps(_live, default=str))

    save_json(LIVE_STATE_FILE, snap)

    if close_update:
        c, nb = close_update
        epic  = c.get("epic", "GOLD")
        log_live(f"CLOSED {epic} {c['side']} @ {c['exit_price']}  "
                 f"R={c['r_multiple']:.2f}  PnL=${c['pnl']:+.2f}  Bal=${nb:.2f}")
        notify(
            f"[LIVE] {c['side']} {epic} {c['exit_reason'].upper()} @ {c['exit_price']}  "
            f"R={c['r_multiple']:+.2f}  PnL=${c['pnl']:+.2f}  Bal=${nb:.2f}",
            severity="info" if c["pnl"] >= 0 else "warning",
            details={"mode": "live", "epic": epic, "side": c["side"],
                     "exit_reason": c["exit_reason"],
                     "entry": c["entry"], "exit_price": c["exit_price"],
                     "r_multiple": c["r_multiple"], "pnl": c["pnl"], "balance": nb},
            epic=epic,
        )
    if open_update:
        t    = open_update[0]
        epic = t["epic"]
        log_live(f"OPENED {epic} {t['side']} @ {t['entry']}  "
                 f"SL={t['stop']}  TP={t['target']}  "
                 f"Size={t['size']}  Risk=${t['risk_dollars']:.4f}")
        notify(
            f"[LIVE] {t['side']} {epic} OPENED @ {t['entry']}  "
            f"SL={t['stop']}  TP={t['target']}  Size={t['size']}",
            severity="info",
            details={"mode": "live", "epic": epic, "side": t["side"],
                     "entry": t["entry"], "stop": t["stop"],
                     "target": t["target"], "size": t["size"],
                     "risk_dollars": t["risk_dollars"]},
            epic=epic,
        )
    if not close_update and not open_update:
        log_live(f"No action. GOLD={last_price:.2f}")


# ══════════════════════════════════════════════════════
# POLL LOOP
# ══════════════════════════════════════════════════════
def poll_loop():
    while True:
        try:
            client.login()
            candles_by_epic = {}
            for inst in INSTRUMENTS:
                try:
                    df = client.candles(inst["epic"], 250)
                    if not df.empty and len(df) >= 5:
                        candles_by_epic[inst["epic"]] = df
                except Exception as e:
                    log_paper(f"Candle fetch error ({inst['epic']}): {e}")

            if candles_by_epic:
                now_str = datetime.now(timezone.utc).isoformat()
                paper_poll_multi(candles_by_epic, now_str)
                live_poll_multi(candles_by_epic, now_str)
            else:
                log_paper("No candle data for any instrument")
        except Exception as e:
            log_paper(f"Poll error: {e}")
            log_live(f"Poll error: {e}")
        time.sleep(POLL_INTERVAL)


# ══════════════════════════════════════════════════════
# FLASK UI
# ══════════════════════════════════════════════════════
app = Flask(__name__)

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>180 Trader Dashboard</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',sans-serif;background:#0d1117;color:#e6edf3;min-height:100vh}
.header{background:#161b22;border-bottom:1px solid #30363d;padding:14px 24px;display:flex;align-items:center;gap:12px}
.header h1{font-size:1.3rem;font-weight:600}
.badge{padding:3px 9px;border-radius:12px;font-size:.72rem;font-weight:600}
.live-badge{background:#1a4a1a;color:#3fb950}
.tabs{display:flex;background:#161b22;border-bottom:1px solid #30363d;padding:0 24px}
.tab{padding:12px 20px;cursor:pointer;border-bottom:2px solid transparent;font-size:.9rem;color:#8b949e;transition:.15s}
.tab.active{border-bottom-color:#58a6ff;color:#e6edf3;font-weight:600}
.tab:hover:not(.active){color:#c9d1d9}
.panel{display:none;padding:20px 24px}.panel.active{display:block}
.container{max-width:1200px;margin:0 auto}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:14px;margin-bottom:20px}
.card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:16px}
.card .lbl{font-size:.7rem;color:#8b949e;text-transform:uppercase;letter-spacing:.05em;margin-bottom:4px}
.card .val{font-size:1.5rem;font-weight:700}
.green{color:#3fb950}.red{color:#f85149}.yellow{color:#e3b341}.blue{color:#58a6ff}.purple{color:#bc8cff}
.open-box{background:#1c2810;border:1px solid #2ea043;border-radius:8px;padding:16px;margin-bottom:20px}
.open-box.short-box{background:#2a1010;border-color:#f85149}
.open-box h3{font-size:.9rem;font-weight:600;margin-bottom:10px}
.fields{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px}
.field .lbl{font-size:.68rem;color:#8b949e;margin-bottom:2px}
.field .val{font-size:.9rem;font-weight:600}
.section{background:#161b22;border:1px solid #30363d;border-radius:8px;margin-bottom:20px}
.sec-hdr{padding:12px 16px;border-bottom:1px solid #30363d;font-size:.75rem;font-weight:600;color:#8b949e;text-transform:uppercase;letter-spacing:.05em}
table{width:100%;border-collapse:collapse;font-size:.82rem}
th{padding:9px 14px;text-align:left;color:#8b949e;font-weight:500;border-bottom:1px solid #30363d}
td{padding:9px 14px;border-bottom:1px solid #21262d}
tr:last-child td{border-bottom:none}
tr:hover td{background:#1c2128}
.log-box{max-height:180px;overflow-y:auto;padding:10px 16px;font-family:monospace;font-size:.75rem;color:#8b949e}
.log-box div{padding:2px 0;border-bottom:1px solid #21262d}
.pp{color:#3fb950;font-weight:600}.pn{color:#f85149;font-weight:600}
.info-bar{background:#161b22;border-bottom:1px solid #30363d;padding:6px 24px;font-size:.75rem;color:#8b949e;display:flex;gap:20px;flex-wrap:wrap}
.settings-bar{background:#0d1117;border-bottom:1px solid #30363d;padding:10px 24px;display:flex;align-items:center;gap:16px;flex-wrap:wrap}
.settings-bar label{font-size:.75rem;color:#8b949e}
.settings-bar input[type=number]{width:70px;background:#161b22;border:1px solid #30363d;color:#e6edf3;border-radius:4px;padding:4px 8px;font-size:.8rem}
.settings-bar input[type=number]:focus{outline:none;border-color:#58a6ff}
.btn{padding:5px 14px;border:none;border-radius:5px;font-size:.78rem;font-weight:600;cursor:pointer;transition:.15s}
.btn-save{background:#1f6feb;color:#fff}.btn-save:hover{background:#388bfd}
.btn-reset{background:#3d1212;color:#f85149;border:1px solid #f85149}.btn-reset:hover{background:#f85149;color:#fff}
.btn-reset:disabled{opacity:.4;cursor:not-allowed}
.settings-divider{width:1px;height:24px;background:#30363d}
.toast{position:fixed;bottom:24px;right:24px;background:#1f6feb;color:#fff;padding:10px 18px;border-radius:6px;font-size:.82rem;font-weight:600;display:none;z-index:999}
</style>
</head>
<body>
<div class="header">
  <h1>180 Trader &mdash; Multi-Instrument</h1>
  <span class="badge live-badge" id="status-badge">Loading...</span>
  <span style="margin-left:auto;font-size:.75rem;color:#8b949e" id="header-info">...</span>
</div>
<div class="info-bar" id="prices-bar">
  <span>Last poll: <span id="last-poll">—</span></span>
  <span>Auto-refresh: 30s</span>
</div>

<div class="settings-bar">
  <label>Live Risk %</label>
  <input type="number" id="live-risk" min="0.1" max="5" step="0.1" value="0.5">
  <label>Paper Risk %</label>
  <input type="number" id="paper-risk" min="0.1" max="5" step="0.1" value="0.5">
  <button class="btn btn-save" onclick="saveSettings()">Save Risk Settings</button>
  <div class="settings-divider"></div>
  <button class="btn btn-reset" id="btn-reset-live" onclick="resetAccount('live')">&#9888; Reset Live ($200)</button>
  <button class="btn btn-reset" id="btn-reset-paper" onclick="resetAccount('paper')">&#9888; Reset Paper ($5K)</button>
</div>

<div class="tabs">
  <div class="tab active" onclick="showTab('live')">&#128308; Live Trade ($200)</div>
  <div class="tab" onclick="showTab('paper')">&#128196; Paper Trade ($5K)</div>
</div>

<div id="panel-live" class="panel active"><div class="container" id="live-content">Loading...</div></div>
<div id="panel-paper" class="panel"><div class="container" id="paper-content">Loading...</div></div>

<script>
function showTab(name) {
  document.querySelectorAll('.tab').forEach((t,i) => t.classList.toggle('active', ['live','paper'][i]===name));
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
  document.getElementById('panel-'+name).classList.add('active');
}

function fmt(n, dec=2) { return n == null ? '—' : Number(n).toFixed(dec); }
function fmtPnl(n) {
  if (n == null) return '—';
  const cls = n>=0?'pp':'pn'; return `<span class="${cls}">${n>=0?'+':''}${fmt(n)}</span>`;
}
function fmtR(n) {
  if (n == null) return '—';
  const cls = n>=0?'pp':'pn'; return `<span class="${cls}">${n>=0?'+':''}${fmt(n,3)}</span>`;
}
function colorVal(n, suffix='') {
  const cls = n>0?'green':n<0?'red':'yellow';
  return `<span class="${cls}">${n>=0?'+':''}${fmt(n)}${suffix}</span>`;
}

function renderKpis(s, id) {
  const pnl = s.balance - s.initial_balance;
  const ret = pnl / s.initial_balance * 100;
  const wins = (s.trades||[]).filter(t=>t.r_multiple>0).length;
  const wr = s.trades&&s.trades.length ? wins/s.trades.length*100 : 0;
  const totalR = (s.trades||[]).reduce((a,t)=>a+(t.r_multiple||0),0);
  document.getElementById(id).innerHTML = `
    <div class="grid">
      <div class="card"><div class="lbl">Balance</div><div class="val ${pnl>=0?'green':'red'}">$${fmt(s.balance)}</div></div>
      <div class="card"><div class="lbl">Total P&L</div><div class="val">${colorVal(pnl,'')}</div></div>
      <div class="card"><div class="lbl">Return</div><div class="val">${colorVal(ret,'%')}</div></div>
      <div class="card"><div class="lbl">Closed Trades</div><div class="val blue">${(s.trades||[]).length}</div></div>
      <div class="card"><div class="lbl">Win Rate</div><div class="val ${wr>=50?'green':'yellow'}">${fmt(wr,1)}%</div></div>
      <div class="card"><div class="lbl">Total R</div><div class="val ${totalR>=0?'green':'red'}">${fmt(totalR,2)}R</div></div>
    </div>`;
}

function renderOpenTrade(ot, lastPrices) {
  if (!ot) return '<div style="color:#8b949e;font-size:.85rem;margin-bottom:20px">No open trade.</div>';
  const epic = ot.epic || 'GOLD';
  const lastPrice = (lastPrices && lastPrices[epic]) ? lastPrices[epic] : 0;
  const isLong = ot.side==='LONG';
  const unrR = isLong ? (lastPrice-ot.entry)/ot.risk : (ot.entry-lastPrice)/ot.risk;
  const unrPnl = ot.risk_dollars != null ? unrR*ot.risk_dollars : null;
  const boxCls = isLong ? 'open-box' : 'open-box short-box';
  const color = isLong ? 'green' : 'red';
  let extra = ot.deal_id ? `<div class="field"><div class="lbl">Deal ID</div><div class="val" style="font-size:.75rem">${ot.deal_id}</div></div>` : '';
  let sizeField = ot.size != null ? `<div class="field"><div class="lbl">Size</div><div class="val">${ot.size}</div></div>` : '';
  return `<div class="${boxCls}">
    <h3><span class="${color}">&#9679; OPEN ${ot.side} ${epic}</span></h3>
    <div class="fields">
      <div class="field"><div class="lbl">Entry</div><div class="val">${ot.entry}</div></div>
      <div class="field"><div class="lbl">Stop</div><div class="val red">${ot.stop}</div></div>
      <div class="field"><div class="lbl">Target</div><div class="val green">${ot.target}</div></div>
      <div class="field"><div class="lbl">Risk $</div><div class="val">$${fmt(ot.risk_dollars,4)}</div></div>
      ${sizeField}
      <div class="field"><div class="lbl">Opened</div><div class="val" style="font-size:.78rem">${(ot.open_time||'').substring(0,16)}</div></div>
      ${extra}
    </div>
    <div style="margin-top:10px;font-size:.83rem">
      Unrealised at ${fmt(lastPrice)}: ${fmtR(unrR)} ${unrPnl!=null?'($'+fmt(unrPnl,2)+')':''}
    </div>
  </div>`;
}

function renderTrades(trades) {
  if (!trades||!trades.length) return '<div style="padding:20px;color:#8b949e;text-align:center">No closed trades yet.</div>';
  let rows = trades.map((t,i)=>`<tr>
    <td style="color:#8b949e">${trades.length-i}</td>
    <td class="purple">${t.epic||'GOLD'}</td>
    <td class="${t.side==='LONG'?'green':'red'}">${t.side}</td>
    <td style="color:#8b949e;font-size:.75rem">${(t.open_time||'').substring(0,16)}</td>
    <td>${t.entry}</td>
    <td class="red">${t.stop}</td>
    <td class="green">${t.target}</td>
    <td style="color:#8b949e;font-size:.75rem">${(t.exit_time||'').substring(0,16)}</td>
    <td>${t.exit_price}</td>
    <td class="${t.exit_reason==='target'?'green':t.exit_reason==='stop'?'red':'yellow'}">${t.exit_reason}</td>
    <td>${fmtR(t.r_multiple)}</td>
    <td>${fmtPnl(t.pnl)}</td>
    <td>$${fmt(t.balance_after)}</td>
  </tr>`).join('');
  return `<table><thead><tr>
    <th>#</th><th>Instr</th><th>Side</th><th>Opened</th><th>Entry</th><th>Stop</th><th>Target</th>
    <th>Closed</th><th>Exit</th><th>Reason</th><th>R</th><th>P&L</th><th>Balance</th>
  </tr></thead><tbody>${rows}</tbody></table>`;
}

function renderLog(log) {
  return (log||[]).map(l=>`<div>${l}</div>`).join('');
}

function renderPanel(s, containerId) {
  const lp = s.last_prices || {};
  renderKpis(s, containerId);
  const c = document.getElementById(containerId);
  c.innerHTML += renderOpenTrade(s.open_trade, lp);
  c.innerHTML += `<div class="section"><div class="sec-hdr">Trade History</div>${renderTrades(s.trades)}</div>`;
  c.innerHTML += `<div class="section"><div class="sec-hdr">Activity Log</div><div class="log-box">${renderLog(s.log)}</div></div>`;
}

async function refresh() {
  try {
    const [liveRes, paperRes] = await Promise.all([
      fetch('/api/live').then(r=>r.json()),
      fetch('/api/paper').then(r=>r.json())
    ]);
    document.getElementById('status-badge').textContent = liveRes.status||'Live';
    document.getElementById('last-poll').textContent = (liveRes.last_poll||'').substring(0,19)+' UTC';
    // Show last price for each instrument
    const lp = liveRes.last_prices || {};
    const priceStr = Object.entries(lp).map(([k,v])=>`${k}: ${Number(v).toFixed(k==='GOLD'?2:k.includes('JPY')?3:5)}`).join(' &nbsp;|&nbsp; ');
    document.getElementById('prices-bar').innerHTML =
      `<span>Last poll: <span id="last-poll">${(liveRes.last_poll||'').substring(0,19)} UTC</span></span>` +
      (priceStr ? `<span>${priceStr}</span>` : '') +
      `<span>Auto-refresh: 30s</span>`;
    document.getElementById('live-content').innerHTML='';
    document.getElementById('paper-content').innerHTML='';
    renderPanel(liveRes, 'live-content');
    renderPanel(paperRes, 'paper-content');
  } catch(e) { console.error(e); }
}

// ── Settings ──────────────────────────────────────────────────────────────
async function loadSettings() {
  try {
    const s = await fetch('/api/settings').then(r=>r.json());
    document.getElementById('live-risk').value  = (s.live_risk_pct  * 100).toFixed(1);
    document.getElementById('paper-risk').value = (s.paper_risk_pct * 100).toFixed(1);
  } catch(e) {}
}

async function saveSettings() {
  const lr = parseFloat(document.getElementById('live-risk').value)  / 100;
  const pr = parseFloat(document.getElementById('paper-risk').value) / 100;
  if (isNaN(lr)||isNaN(pr)||lr<=0||pr<=0) { showToast('Invalid values','#f85149'); return; }
  try {
    await fetch('/api/settings', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({live_risk_pct: lr, paper_risk_pct: pr})});
    showToast('Risk settings saved!');
  } catch(e) { showToast('Save failed','#f85149'); }
}

// ── Reset ─────────────────────────────────────────────────────────────────
async function resetAccount(mode) {
  const label = mode==='live' ? 'Live ($200)' : 'Paper ($5K)';
  if (!confirm('Reset ' + label + ' account?\\nThis will clear ALL trade history and restore the starting balance.')) return;
  const btn = document.getElementById('btn-reset-'+mode);
  btn.disabled = true;
  try {
    await fetch('/api/reset/'+mode, {method:'POST'});
    showToast(label + ' account reset!');
    await refresh();
  } catch(e) { showToast('Reset failed','#f85149'); }
  finally { btn.disabled = false; }
}

function showToast(msg, color='#1f6feb') {
  const t = document.getElementById('toast');
  t.textContent = msg; t.style.background = color; t.style.display = 'block';
  setTimeout(()=>{ t.style.display='none'; }, 3000);
}

refresh();
loadSettings();
setInterval(refresh, 30000);
</script>
<div class="toast" id="toast"></div>
</body>
</html>"""


@app.route("/")
def index():
    return render_template_string(DASHBOARD_HTML)

@app.route("/api/paper")
def api_paper():
    with _paper_lock:
        s = json.loads(json.dumps(_paper, default=str))
    return jsonify(s)

@app.route("/api/live")
def api_live():
    with _live_lock:
        s = json.loads(json.dumps(_live, default=str))
    return jsonify(s)

@app.route("/api/reset/paper", methods=["POST"])
def reset_paper():
    global _paper
    with _paper_lock:
        _paper = default_paper_state()
        save_json(PAPER_STATE_FILE, _paper)
    log_paper("State RESET by user — balance restored to $5,000")
    return jsonify({"ok": True})

@app.route("/api/reset/live", methods=["POST"])
def reset_live():
    global _live
    with _live_lock:
        _live = default_live_state()
        save_json(LIVE_STATE_FILE, _live)
    log_live("State RESET by user — balance restored to $200")
    return jsonify({"ok": True})

@app.route("/api/settings", methods=["GET"])
def get_settings():
    with _settings_lock:
        return jsonify(_settings)

@app.route("/api/settings", methods=["POST"])
def update_settings():
    global _settings
    data = flask_request.get_json()
    with _settings_lock:
        if "live_risk_pct" in data:
            val = float(data["live_risk_pct"])
            if 0.001 <= val <= 0.05:
                _settings["live_risk_pct"] = round(val, 4)
        if "paper_risk_pct" in data:
            val = float(data["paper_risk_pct"])
            if 0.001 <= val <= 0.05:
                _settings["paper_risk_pct"] = round(val, 4)
        save_settings(_settings)
        snap = dict(_settings)
    log_live(f"Settings updated: live_risk={snap['live_risk_pct']*100:.2f}%  paper_risk={snap['paper_risk_pct']*100:.2f}%")
    return jsonify({"ok": True, "settings": snap})


# ══════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════
if __name__ == "__main__":
    paper_epics = [i["epic"] for i in PAPER_INSTRUMENTS]
    live_epics = [i["epic"] for i in LIVE_INSTRUMENTS]
    log_paper(f"Starting — paper instruments: {paper_epics}  (priority order, 1 trade at a time)")
    log_live(f"Starting — live instruments: {live_epics}")
    log_paper(f"Paper balance: ${PAPER_INITIAL:,.0f}  |  Live balance: ${LIVE_INITIAL:,.0f}")
    log_paper(f"Strategy: body={MIN_BODY_PCT} RR={RR_TARGET} 80pct={USE_80PCT_ENTRY} "
              f"no_follow={NO_FOLLOW_THRU} trend={USE_TREND} max/day={MAX_TRADES_DAY}")

    t = threading.Thread(target=poll_loop, daemon=True)
    t.start()

    app.run(host="0.0.0.0", port=8080, debug=False, use_reloader=False, threaded=True)
