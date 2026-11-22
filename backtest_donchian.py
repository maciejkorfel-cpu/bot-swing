#!/usr/bin/env python3
"""
Backtest bot-swing: Donchian breakout 4H + trailing stop (ATR), z realnymi kosztami.

Uruchomienie (np. w GitHub Codespaces):
    pip install pandas numpy requests
    python backtest_donchian.py
    python backtest_donchian.py --days 365 --short --fee 0.0005 --slip 0.0005

Dane: publiczne API OKX (bez klucza). Wyniki: tabela per para + podsumowanie
+ werdykt wg kryteriów oceny + plik trades.csv.
"""
import argparse
import time

import numpy as np
import pandas as pd
import requests

TOP10 = ["BTC-USDT", "ETH-USDT", "SOL-USDT", "XRP-USDT", "BNB-USDT",
         "DOGE-USDT", "ADA-USDT", "AVAX-USDT", "LINK-USDT", "TRX-USDT"]

# Kryteria oceny (czy strategia nadaje się do dalszego testu na demo / live)
CRITERIA = {
    "min_trades": 30,        # mniej = wynik statystycznie bez znaczenia
    "min_profit_factor": 1.3,
    "max_drawdown_pct": 25.0,
}


# ---------------------------------------------------------------- dane
def fetch_candles(inst, bar="4H", days=365):
    """Pobiera świece z OKX (history-candles, stronicowanie wstecz)."""
    url = "https://www.okx.com/api/v5/market/history-candles"
    end_needed = int((time.time() - days * 86400) * 1000)
    rows, after = [], None
    while True:
        params = {"instId": inst, "bar": bar, "limit": 100}
        if after:
            params["after"] = after
        r = requests.get(url, params=params, timeout=20)
        r.raise_for_status()
        data = r.json().get("data", [])
        if not data:
            break
        rows.extend(data)
        oldest = int(data[-1][0])
        if oldest <= end_needed:
            break
        after = oldest
        time.sleep(0.12)  # limit API: 20 zapytań / 2 s
    df = pd.DataFrame(rows, columns=["ts", "o", "h", "l", "c", "vol",
                                     "volCcy", "volQuote", "confirm"])
    df = df[df["confirm"] == "1"]  # tylko zamknięte świece
    df["ts"] = pd.to_datetime(df["ts"].astype("int64"), unit="ms", utc=True)
    for col in ["o", "h", "l", "c"]:
        df[col] = df[col].astype(float)
    df = df.sort_values("ts").drop_duplicates("ts").reset_index(drop=True)
    return df[df["ts"] >= pd.Timestamp(end_needed, unit="ms", tz="UTC")]


# ---------------------------------------------------------------- wskaźniki
def add_indicators(df, don_n, atr_n):
    df = df.copy()
    df["don_hi"] = df["h"].rolling(don_n).max().shift(1)  # bez podglądania
    df["don_lo"] = df["l"].rolling(don_n).min().shift(1)
    prev_c = df["c"].shift(1)
    tr = pd.concat([df["h"] - df["l"],
                    (df["h"] - prev_c).abs(),
                    (df["l"] - prev_c).abs()], axis=1).max(axis=1)
    df["atr"] = tr.ewm(alpha=1 / atr_n, adjust=False).mean()
    return df


# ---------------------------------------------------------------- symulacja
def backtest(df, inst, a):
    """
    Wejście: zamknięcie świecy powyżej kanału -> wejście na OTWARCIU następnej
    (realistycznie, tak jak działa bot odpalany co 5 min).
    Wyjście: trailing stop = max(close od wejścia) - ATR * mult (dla longa).
    Stop sprawdzany na low/high świecy; gap przez stop -> wyjście po open.
    Koszty: prowizja + poślizg doliczane przy KAŻDYM wejściu i wyjściu.
    """
    cost = a.fee + a.slip
    trades, pos = [], None
    equity, eq_curve = 1.0, []
    risk_frac = a.risk  # część kapitału ryzykowana na trade

    for i in range(1, len(df)):
        bar, prev = df.iloc[i], df.iloc[i - 1]

        # --- zarządzanie otwartą pozycją
        if pos:
            if pos["side"] == 1:
                stop = pos["stop"]
                if bar["l"] <= stop:
                    px = min(bar["o"], stop)
                    pos["exit"] = px * (1 - cost)
                else:
                    pos["peak"] = max(pos["peak"], bar["c"])
                    pos["stop"] = max(stop, pos["peak"] - a.mult * bar["atr"])
            else:
                stop = pos["stop"]
                if bar["h"] >= stop:
                    px = max(bar["o"], stop)
                    pos["exit"] = px * (1 + cost)
                else:
                    pos["peak"] = min(pos["peak"], bar["c"])
                    pos["stop"] = min(stop, pos["peak"] + a.mult * bar["atr"])

            if "exit" in pos:
                ret = pos["side"] * (pos["exit"] / pos["entry"] - 1)
                pnl = ret * pos["size"]
                equity *= (1 + pnl)
                trades.append({"inst": inst, "side": "LONG" if pos["side"] == 1 else "SHORT",
                               "open": pos["t"], "close": bar["ts"],
                               "entry": pos["entry"], "exit": pos["exit"],
                               "ret_pct": ret * 100, "pnl_pct_equity": pnl * 100})
                pos = None

        # --- sygnał na zamknięciu poprzedniej świecy, wejście na open bieżącej
        if pos is None and not np.isnan(prev["don_hi"]) and not np.isnan(prev["atr"]):
            side = 0
            if prev["c"] > prev["don_hi"]:
                side = 1
            elif a.short and prev["c"] < prev["don_lo"]:
                side = -1
            if side:
                entry = bar["o"] * (1 + side * cost)
                stop = entry - side * a.mult * prev["atr"]
                stop_dist = abs(entry - stop) / entry
                # wielkość pozycji tak, by strata na stopie = risk_frac kapitału
                size = min(risk_frac / stop_dist, a.max_lev) if stop_dist > 0 else 0
                pos = {"side": side, "entry": entry, "stop": stop, "peak": bar["o"],
                       "size": size, "t": bar["ts"]}
        eq_curve.append(equity)

    return trades, pd.Series(eq_curve)


def stats(trades, eq):
    if not trades:
        return {"trades": 0, "win%": 0, "PF": 0, "ret%": 0, "maxDD%": 0}
    t = pd.DataFrame(trades)
    gains = t.loc[t.pnl_pct_equity > 0, "pnl_pct_equity"].sum()
    losses = -t.loc[t.pnl_pct_equity < 0, "pnl_pct_equity"].sum()
    dd = (eq / eq.cummax() - 1).min() * 100
    return {"trades": len(t),
            "win%": round((t.pnl_pct_equity > 0).mean() * 100, 1),
            "PF": round(gains / losses, 2) if losses else float("inf"),
            "ret%": round((eq.iloc[-1] - 1) * 100, 1),
            "maxDD%": round(dd, 1)}


def run(a, cost_mult=1.0, data=None, quiet=False):
    a2 = argparse.Namespace(**vars(a))
    a2.fee, a2.slip = a.fee * cost_mult, a.slip * cost_mult
    all_trades, rows, curves = [], [], []
    for inst in a.pairs:
        df = data[inst]
        df = add_indicators(df, a.don, a.atr)
        tr, eq = backtest(df, inst, a2)
        all_trades += tr
        curves.append(eq.reset_index(drop=True))
        rows.append({"para": inst, **stats(tr, eq)})
    table = pd.DataFrame(rows)
    # portfel: równy podział kapitału między pary
    n = min(len(c) for c in curves)
    port = sum(c.iloc[-n:].reset_index(drop=True) for c in curves) / len(curves)
    total = stats(all_trades, port)
    if not quiet:
        print(table.to_string(index=False))
    return table, total, all_trades


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--pairs", nargs="+", default=TOP10)
    p.add_argument("--days", type=int, default=365)
    p.add_argument("--don", type=int, default=20, help="okres kanału Donchiana")
    p.add_argument("--atr", type=int, default=14)
    p.add_argument("--mult", type=float, default=3.0, help="trailing stop = ATR * mult")
    p.add_argument("--risk", type=float, default=0.02, help="ryzyko na trade (0.02 = 2%%)")
    p.add_argument("--max-lev", dest="max_lev", type=float, default=1.0,
                   help="maks. wielkość pozycji vs kapitał (1 = spot bez dźwigni)")
    p.add_argument("--short", action="store_true", help="włącz shorty (futures)")
    p.add_argument("--fee", type=float, default=0.001,
                   help="prowizja na stronę (spot OKX taker ~0.1%%, futures ~0.05%%)")
    p.add_argument("--slip", type=float, default=0.0005, help="poślizg na stronę")
    a = p.parse_args()

    print(f"Pobieram {a.days} dni świec 4H z OKX dla {len(a.pairs)} par...")
    data = {}
    for inst in a.pairs:
        data[inst] = fetch_candles(inst, "4H", a.days)
        print(f"  {inst}: {len(data[inst])} świec")

    print(f"\n=== KOSZTY REALNE (prowizja {a.fee*100:.2f}% + poślizg {a.slip*100:.2f}% na stronę) ===")
    _, total, trades = run(a, 1.0, data)
    print("PORTFEL:", total)

    print("\n=== TEST BEZ KOSZTÓW (dla porównania) ===")
    _, t0, _ = run(a, 0.0, data, quiet=True)
    print("PORTFEL:", t0)

    print("\n=== STRESS TEST: KOSZTY x2 ===")
    _, t2, _ = run(a, 2.0, data, quiet=True)
    print("PORTFEL:", t2)

    pd.DataFrame(trades).to_csv("trades.csv", index=False)

    c = CRITERIA
    ok = (total["trades"] >= c["min_trades"] and total["PF"] >= c["min_profit_factor"]
          and abs(total["maxDD%"]) <= c["max_drawdown_pct"] and t2["PF"] > 1.0)
    print("\n=== WERDYKT ===")
    print(f"Kryteria: >= {c['min_trades']} transakcji, PF >= {c['min_profit_factor']}, "
          f"maxDD <= {c['max_drawdown_pct']}%, PF przy kosztach x2 > 1.0")
    print("✅ ZDAJE – zostaw na demo i porównuj demo z backtestem"
          if ok else "❌ NIE ZDAJE – nie przechodź na live, zmień parametry/strategię")
    print("Transakcje zapisane w trades.csv")


if __name__ == "__main__":
    main()
