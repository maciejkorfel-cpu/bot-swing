#!/usr/bin/env python3
"""
Przeszukiwanie strategii dla bot-swing (OKX), z walidacją walk-forward.

- dane: świece 4H z OKX (publiczne API), 1D liczone z 4H
- strategie: Donchian (+filtr EMA200, wyjście trailing ATR lub kanał Turtle),
  przecięcia EMA, mean reversion RSI(2), mean reversion Bollinger
- tryby: SPOT (tylko long, prowizja 0.10%) i FUT (long+short, prowizja 0.05%),
  poślizg 0.05% na stronę
- wybór: ranking na pierwszych 60% danych (IS), weryfikacja na ostatnich 40% (OOS),
  stress test kosztów x2

Wynik: tabela w GITHUB_STEP_SUMMARY (jeśli jest) + results.csv
Użycie: python strategy_search.py [--days 1095] [--pairs BTC-USDT ETH-USDT ...]
"""
import argparse
import itertools
import os
import sys
import time

import numpy as np
import pandas as pd

TOP10 = ["BTC-USDT", "ETH-USDT", "SOL-USDT", "XRP-USDT", "BNB-USDT",
         "DOGE-USDT", "ADA-USDT", "AVAX-USDT", "LINK-USDT", "TRX-USDT"]
SLIP = 0.0005
FEES = {"SPOT": 0.001, "FUT": 0.0005}
IS_FRAC = 0.6
PASS = {"min_is_pf": 1.2, "min_oos_pf": 1.2, "min_oos_trades": 30, "min_stress_pf": 1.0}


# ------------------------------------------------------------------ dane
def fetch_candles(inst, days):
    import requests
    url = "https://www.okx.com/api/v5/market/history-candles"
    end_needed = int((time.time() - days * 86400) * 1000)
    rows, after = [], None
    for _ in range(2000):
        params = {"instId": inst, "bar": "4H", "limit": 100}
        if after:
            params["after"] = after
        for attempt in range(5):
            try:
                r = requests.get(url, params=params, timeout=20)
                r.raise_for_status()
                data = r.json().get("data", [])
                break
            except Exception:
                time.sleep(1 + attempt)
        else:
            raise RuntimeError(f"OKX nie odpowiada dla {inst}")
        if not data:
            break
        rows.extend(data)
        oldest = int(data[-1][0])
        if oldest <= end_needed:
            break
        after = oldest
        time.sleep(0.12)
    df = pd.DataFrame(rows, columns=["ts", "o", "h", "l", "c", "v", "vc", "vq", "confirm"])
    df = df[df["confirm"] == "1"]
    df["ts"] = pd.to_datetime(df["ts"].astype("int64"), unit="ms", utc=True)
    for col in "ohlc":
        df[col] = df[col].astype(float)
    df = df.sort_values("ts").drop_duplicates("ts")
    df = df[df["ts"] >= pd.Timestamp(end_needed, unit="ms", tz="UTC")]
    return df[["ts", "o", "h", "l", "c"]].reset_index(drop=True)


def to_daily(df):
    d = df.set_index("ts").resample("1D").agg({"o": "first", "h": "max", "l": "min", "c": "last"})
    return d.dropna().reset_index()


# ------------------------------------------------------------------ wskaźniki
def ema(x, n):
    return pd.Series(x).ewm(span=n, adjust=False).mean().values


def atr(h, l, c, n=14):
    pc = np.r_[c[0], c[:-1]]
    tr = np.maximum.reduce([h - l, np.abs(h - pc), np.abs(l - pc)])
    return pd.Series(tr).ewm(alpha=1 / n, adjust=False).mean().values


def rsi(c, n):
    d = np.diff(c, prepend=c[0])
    up = pd.Series(np.clip(d, 0, None)).ewm(alpha=1 / n, adjust=False).mean()
    dn = pd.Series(np.clip(-d, 0, None)).ewm(alpha=1 / n, adjust=False).mean()
    return (100 - 100 / (1 + up / dn.replace(0, np.nan))).fillna(50).values


def roll_max(x, n):
    return pd.Series(x).rolling(n).max().shift(1).values


def roll_min(x, n):
    return pd.Series(x).rolling(n).min().shift(1).values


# ------------------------------------------------------------------ strategie
# Każda zwraca: long_in, short_in, long_out, short_out (bool na zamknięciu świecy),
# oraz stop: ("trail"|"fixed", mnożnik ATR)
def strat_donchian(d, n, mult, trend, exit_kind):
    c, h, l = d["c"], d["h"], d["l"]
    hi, lo = roll_max(h, n), roll_min(l, n)
    e200 = ema(c, 200)
    li = c > hi
    si = c < lo
    if trend:
        li &= c > e200
        si &= c < e200
    if exit_kind == "turtle":
        m = max(n // 2, 5)
        lo_x, hi_x = roll_min(l, m), roll_max(h, m)
        return li, si, c < lo_x, c > hi_x, ("fixed", mult)
    f = np.zeros(len(c), bool)
    return li, si, f, f, ("trail", mult)


def strat_ema(d, fast, slow, trend):
    c = d["c"]
    ef, es, e200 = ema(c, fast), ema(c, slow), ema(c, 200)
    up = ef > es
    prev = np.r_[up[0], up[:-1]]
    li = up & ~prev
    si = ~up & prev
    if trend:
        li &= c > e200
        si &= c < e200
    return li, si, ~up, up, ("fixed", 3.0)


def strat_rsi_mr(d, thr, mult):
    c = d["c"]
    r = rsi(c, 2)
    e200 = ema(c, 200)
    sma5 = pd.Series(c).rolling(5).mean().values
    li = (r < thr) & (c > e200)
    si = (r > 100 - thr) & (c < e200)
    return li, si, c > sma5, c < sma5, ("fixed", mult)


def strat_bb_mr(d, mult):
    c = d["c"]
    s = pd.Series(c)
    mid = s.rolling(20).mean().values
    sd = s.rolling(20).std().values
    e200 = ema(c, 200)
    li = (c < mid - 2 * sd) & (c > e200)
    si = (c > mid + 2 * sd) & (c < e200)
    return li, si, c > mid, c < mid, ("fixed", mult)


def build_configs():
    cfg = []
    for n, mult, trend in itertools.product([20, 40, 55], [2.0, 3.0, 4.0], [False, True]):
        cfg.append((f"donch{n}_trail{mult:g}" + ("_ema200" if trend else ""),
                    lambda d, n=n, m=mult, t=trend: strat_donchian(d, n, m, t, "trail")))
    for n, trend in itertools.product([20, 40, 55], [False, True]):
        cfg.append((f"turtle{n}" + ("_ema200" if trend else ""),
                    lambda d, n=n, t=trend: strat_donchian(d, n, 3.0, t, "turtle")))
    for (f, s), trend in itertools.product([(9, 21), (20, 50), (50, 200)], [False, True]):
        if s == 200 and trend:
            continue
        cfg.append((f"ema{f}x{s}" + ("_ema200" if trend else ""),
                    lambda d, f=f, s=s, t=trend: strat_ema(d, f, s, t)))
    for thr, mult in itertools.product([5, 10], [2.0, 3.0]):
        cfg.append((f"rsi2mr{thr}_sl{mult:g}", lambda d, th=thr, m=mult: strat_rsi_mr(d, th, m)))
    for mult in [2.0, 3.0]:
        cfg.append((f"bbmr_sl{mult:g}", lambda d, m=mult: strat_bb_mr(d, m)))
    return cfg


# ------------------------------------------------------------------ silnik
def simulate(d, sig, allow_short, cost):
    """Sygnał na zamknięciu świecy i -> wejście/wyjście na otwarciu i+1.
    Zwraca listę transakcji (entry_idx, exit_idx, ret)."""
    o, h, l, c, a = d["o"], d["h"], d["l"], d["c"], d["atr"]
    li, si, lo_, so_, (stop_kind, mult) = sig
    n = len(c)
    trades = []
    side = 0
    pend_in = 0
    pend_out = False
    entry = stop = peak = 0.0
    ei = 0
    warm = 200
    for i in range(warm, n):
        if pend_in:
            side = pend_in
            pend_in = 0
            entry = o[i] * (1 + side * cost)
            stop = o[i] - side * mult * a[i - 1]
            peak = o[i]
            ei = i
        elif pend_out and side:
            px = o[i] * (1 - side * cost)
            trades.append((ei, i, side * (px / entry - 1)))
            side = 0
            pend_out = False
        if side == 1 and l[i] <= stop:
            px = min(o[i], stop) * (1 - cost)
            trades.append((ei, i, px / entry - 1))
            side = 0
        elif side == -1 and h[i] >= stop:
            px = max(o[i], stop) * (1 + cost)
            trades.append((ei, i, -(px / entry - 1)))
            side = 0
        if side == 1:
            if lo_[i]:
                pend_out = True
            if stop_kind == "trail":
                peak = max(peak, c[i])
                stop = max(stop, peak - mult * a[i])
        elif side == -1:
            if so_[i]:
                pend_out = True
            if stop_kind == "trail":
                peak = min(peak, c[i])
                stop = min(stop, peak + mult * a[i])
        if side == 0 and not pend_in and i < n - 1:
            pend_out = False
            if li[i]:
                pend_in = 1
            elif allow_short and si[i]:
                pend_in = -1
    return trades


def stats(pair_trades, lo_idx, hi_idx, nbars):
    """pair_trades: {pair: [(ei, xi, r)]}; liczy dla transakcji z ei w [lo,hi)."""
    rets, curves = [], []
    for p, tr in pair_trades.items():
        mult = np.ones(nbars[p])
        for ei, xi, r in tr:
            if lo_idx[p] <= ei < hi_idx[p]:
                rets.append(r)
                mult[xi] *= (1 + r)
        eq = np.cumprod(mult[lo_idx[p]:hi_idx[p]])
        curves.append(eq)
    if not rets:
        return dict(trades=0, win=0, pf=0, ret=0, dd=0)
    m = min(len(x) for x in curves)
    port = np.mean([x[-m:] for x in curves], axis=0)
    rets = np.array(rets)
    g, ls = rets[rets > 0].sum(), -rets[rets < 0].sum()
    return dict(trades=len(rets), win=round((rets > 0).mean() * 100, 1),
                pf=round(g / ls, 2) if ls > 0 else 99.0,
                ret=round((port[-1] - 1) * 100, 1),
                dd=round((port / np.maximum.accumulate(port) - 1).min() * 100, 1))


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1095)
    ap.add_argument("--pairs", nargs="+", default=TOP10)
    ap.add_argument("--synthetic", action="store_true", help="test lokalny bez sieci")
    a = ap.parse_args()

    raw = {}
    for p in a.pairs:
        if a.synthetic:
            rng = np.random.default_rng(abs(hash(p)) % 2**32)
            n = a.days * 6
            cc = 100 * np.exp(np.cumsum(rng.normal(0.0002, 0.02, n)))
            oo = np.r_[cc[0], cc[:-1]]
            raw[p] = pd.DataFrame({"ts": pd.date_range("2023-01-01", periods=n, freq="4h", tz="UTC"),
                                   "o": oo, "h": np.maximum(oo, cc) * 1.004,
                                   "l": np.minimum(oo, cc) * 0.996, "c": cc})
        else:
            raw[p] = fetch_candles(p, a.days)
        print(f"{p}: {len(raw[p])} świec 4H, od {raw[p]['ts'].iloc[0].date()}", flush=True)

    frames = {"4H": raw, "1D": {p: to_daily(df) for p, df in raw.items()}}
    configs = build_configs()
    rows = []
    t0 = time.time()
    for tf, data in frames.items():
        prepared = {}
        for p, df in data.items():
            d = {k: df[k].values.astype(float) for k in "ohlc"}
            d["atr"] = atr(d["h"], d["l"], d["c"])
            prepared[p] = d
        nb = {p: len(d["c"]) for p, d in prepared.items()}
        split = {p: int(n * IS_FRAC) for p, n in nb.items()}
        start = {p: 0 for p in nb}
        for name, fn in configs:
            sigs = {p: fn(d) for p, d in prepared.items()}
            for mode, fee in FEES.items():
                res = {}
                for cm in (1, 2):
                    cost = (fee + SLIP) * cm
                    res[cm] = {p: simulate(prepared[p], sigs[p], mode == "FUT", cost)
                               for p in prepared}
                is_ = stats(res[1], start, split, nb)
                oos = stats(res[1], split, nb, nb)
                st = stats(res[2], split, nb, nb)
                rows.append(dict(tf=tf, mode=mode, strategy=name,
                                 is_trades=is_["trades"], is_pf=is_["pf"], is_ret=is_["ret"], is_dd=is_["dd"],
                                 oos_trades=oos["trades"], oos_win=oos["win"], oos_pf=oos["pf"],
                                 oos_ret=oos["ret"], oos_dd=oos["dd"], oos_pf_cost2x=st["pf"]))
        print(f"{tf}: gotowe ({time.time()-t0:.0f}s)", flush=True)

    df = pd.DataFrame(rows)
    df["passed"] = ((df.is_pf >= PASS["min_is_pf"]) & (df.oos_pf >= PASS["min_oos_pf"]) &
                    (df.oos_trades >= PASS["min_oos_trades"]) & (df.oos_pf_cost2x >= PASS["min_stress_pf"]))
    df.to_csv("results.csv", index=False)

    # ranking: wybór TYLKO wg IS (uczciwie), potem pokazujemy jak wypadły OOS
    top_is = df[df.is_trades >= 30].sort_values("is_pf", ascending=False).head(15)
    passed = df[df.passed].sort_values("oos_pf", ascending=False)
    cols = ["tf", "mode", "strategy", "is_trades", "is_pf", "is_ret", "oos_trades",
            "oos_win", "oos_pf", "oos_ret", "oos_dd", "oos_pf_cost2x"]

    split_date = {tf: frames[tf][a.pairs[0]]["ts"].iloc[int(len(frames[tf][a.pairs[0]]) * IS_FRAC)].date()
                  for tf in frames}
    out = []
    out.append(f"# Przeszukiwanie strategii — {len(df)} wariantów, {len(a.pairs)} par, {a.days} dni\n")
    out.append(f"IS = do {split_date['4H']}, OOS = od {split_date['4H']} (dane, których wybór nie widział)\n")
    out.append(f"Kryteria: IS PF≥{PASS['min_is_pf']}, OOS PF≥{PASS['min_oos_pf']}, "
               f"OOS transakcji≥{PASS['min_oos_trades']}, OOS PF przy kosztach×2≥{PASS['min_stress_pf']}\n")
    out.append(f"\n## ✅ Przeszły walidację: {len(passed)}\n")
    out.append(passed[cols].head(20).to_markdown(index=False) if len(passed) else "_brak_")
    out.append("\n\n## Top 15 wg IS (jak wypadły na OOS)\n")
    out.append(top_is[cols].to_markdown(index=False))
    best_fam = (df.assign(fam=df.strategy.str.extract(r"^([a-z]+)")[0])
                .groupby(["tf", "mode", "fam"])[["is_pf", "oos_pf"]].median().round(2)
                .sort_values("oos_pf", ascending=False).head(12))
    out.append("\n\n## Mediana PF wg rodziny strategii\n")
    out.append(best_fam.to_markdown())
    text = "\n".join(out)
    print(text)
    summ = os.environ.get("GITHUB_STEP_SUMMARY")
    if summ:
        with open(summ, "a") as f:
            f.write(text + "\n")


if __name__ == "__main__":
    main()
