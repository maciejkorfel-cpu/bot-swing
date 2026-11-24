"""S2 — reguły 1:1 z research/finalists.py (zamrożone 2026-11-22) + wejście limit z research_edge (część 2).

LONG BTC i ETH, świece 4H (UTC):
  sygnał na zamknięciu świecy i:  close > max(high z 55 poprzednich świec)  i  close > EMA(300 świec 4H ≈ 50 dni)
  wejście: na otwarciu świecy i+1 (w backteście rynkowo; w demo limit po cenie open ważny 1 h, potem rynek)
  SL = open(i+1) − 3·ATR14(i), TP = open(i+1) + 4·ATR14(i) (limit), trailing: max zamknięcie od wejścia − 4·ATR14(bieżący),
  przesuwany po zamknięciu świecy (działa od następnej); SL przed TP w tej samej świecy (pesymistycznie).
  Wielkość: 0,5% kapitału / (3·ATR), nominał ≤ 3× kapitału.
Wskaźniki identyczne z research/engine.py (EMA: ewm span, adjust=False; ATR: Wilder ewm alpha=1/14 od TR[0]).
Moduł bez zależności od research/ (działa w GitHub Actions bez danych badawczych).
"""
import math

N_BREAK = 55
EMA_N = 300
ATR_N = 14
SL_ATR, TP_ATR, TRAIL_ATR = 3.0, 4.0, 4.0
RISK = 0.005
MAX_LEV = 3.0
BAR_MS = 4 * 3600_000
COSTS = {"taker": 0.0005, "maker": 0.0002, "slip": 0.0003}


def ema(x, n):
    a = 2.0 / (n + 1)
    out, e = [], None
    for v in x:
        e = v if e is None else a * v + (1 - a) * e
        out.append(e)
    return out


def atr(h, l, c, n=ATR_N):
    out, a = [], None
    for i in range(len(c)):
        pc = c[i - 1] if i > 0 else c[0]
        tr = max(h[i] - l[i], abs(h[i] - pc), abs(l[i] - pc))
        a = tr if a is None else a + (tr - a) / n
        out.append(a)
    return out


def indicators(bars):
    """bars: lista [ts, o, h, l, c] rosnąco (tylko zamknięte). Zwraca dict list."""
    o = [b[1] for b in bars]; h = [b[2] for b in bars]; l = [b[3] for b in bars]; c = [b[4] for b in bars]
    e = ema(c, EMA_N)
    a = atr(h, l, c)
    hh = [None] * len(c)
    for i in range(N_BREAK, len(c)):
        hh[i] = max(h[i - N_BREAK:i])
    sig = [hh[i] is not None and c[i] > hh[i] and c[i] > e[i] for i in range(len(c))]
    return dict(ts=[b[0] for b in bars], o=o, h=h, l=l, c=c, ema=e, atr=a, hh=hh, sig=sig)


def signal_at(ind, i):
    """Czy świeca i (zamknięta) daje sygnał; zwraca dict z poziomami względem ceny odniesienia (open i+1 ≈ close i)."""
    if not ind["sig"][i]:
        return None
    return dict(bar_ts=ind["ts"][i], close=ind["c"][i], atr=ind["atr"][i], hh=ind["hh"][i], ema=ind["ema"][i],
                sl_d=SL_ATR * ind["atr"][i], tp_d=TP_ATR * ind["atr"][i])


def size(equity, ref_px, sl_d, risk=RISK, max_lev=MAX_LEV):
    """ilość coina: ryzyko/odległość SL, nominał ≤ max_lev × kapitał."""
    if sl_d <= 0 or equity <= 0:
        return 0.0
    q = risk * equity / sl_d
    if q * ref_px > max_lev * equity:
        q = max_lev * equity / ref_px
    return q


def trail_stop(peak_close, atr_now, stop):
    ns = peak_close - TRAIL_ATR * atr_now
    return ns if ns > stop else stop


def simulate(bars, fund=None, start_ts=None, end_ts=None, cap0=10000.0, costs=COSTS, entry_fill=None):
    """Port research/engine._sim dla S2 (long, wejście rynkowe, SL/TP/trailing, funding). Jedno konto na instrument.
    entry_fill: opcjonalna funkcja (i_entry, open_px) -> (cena, 'maker'|'taker'|None) dla wariantu limit (None = pominięta).
    Zwraca (lista transakcji, equity końcowe)."""
    ind = indicators(bars)
    o, h, l, c, a, sig, ts = ind["o"], ind["h"], ind["l"], ind["c"], ind["atr"], ind["sig"], ind["ts"]
    n = len(c)
    fund = fund or [0.0] * n
    i0 = next((i for i in range(n) if start_ts is None or ts[i] >= start_ts), n)
    i0 = max(i0, 1)
    i1 = next((i for i in range(n) if end_ts is not None and ts[i] >= end_ts), n)
    tk, mk, sl = costs["taker"], costs["maker"], costs["slip"]
    eq = cap0
    trades = []
    inpos = pend_in = False
    entry = qty = stop = tp = peak = fee_acc = fund_acc = risk_usd = 0.0
    ei = 0
    for i in range(i0, i1):
        if inpos and fund[i] != 0.0:
            fc = qty * o[i] * fund[i]
            eq -= fc; fund_acc += fc
        if pend_in and not inpos:
            pend_in = False
            d = SL_ATR * a[i - 1]
            ep, kind = o[i] * (1 + sl), "taker"
            if entry_fill is not None:
                ep, kind = entry_fill(i, o[i])
            if d > 0 and eq > 0 and kind is not None:
                q = size(eq, ep, d)
                qty, entry = q, ep
                stop = o[i] - d
                tp = o[i] + TP_ATR * a[i - 1]
                peak = o[i]; ei = i
                fee_acc = qty * ep * (mk if kind == "maker" else tk); fund_acc = 0.0
                eq -= fee_acc
                risk_usd = RISK * (eq + fee_acc)
                inpos = True
        if inpos:
            hit_sl = l[i] <= stop
            hit_tp = h[i] > tp
            if hit_sl or hit_tp:
                if hit_sl:
                    px = min(o[i], stop) * (1 - sl); f = qty * px * tk; why = "SL"
                else:
                    px = o[i] if o[i] > tp else tp; f = qty * px * mk; why = "TP"
                pnl = qty * (px - entry) - f
                eq += pnl
                tot = pnl - fee_acc - fund_acc
                trades.append(dict(entry_ts=ts[ei], exit_ts=ts[i], entry=entry, exit=px, qty=qty, why=why, pnl=tot,
                                   R=tot / risk_usd, fee=fee_acc + f, fund=fund_acc, ei=ei, xi=i))
                inpos = False
        if inpos:
            peak = max(peak, c[i])
            stop = trail_stop(peak, a[i], stop)
        elif sig[i] and i < i1 - 1:
            pend_in = True
    if inpos:
        px = c[i1 - 1] * (1 - sl); f = qty * px * tk
        pnl = qty * (px - entry) - f; eq += pnl
        tot = pnl - fee_acc - fund_acc
        trades.append(dict(entry_ts=ts[ei], exit_ts=ts[i1 - 1], entry=entry, exit=px, qty=qty, why="END", pnl=tot,
                           R=tot / risk_usd, fee=fee_acc + f, fund=fund_acc, ei=ei, xi=i1 - 1))
    return trades, eq


def limit_fill(bars5, t_open_ms, limit_px, timeout_ms=3600_000, slip=COSTS["slip"], tp_px=None):
    """Wejście limit (research_edge 0.3): wypełnienie tylko gdy low 5m < limit w [t_open, t_open+timeout);
    potem rynek na open 5m po timeout (+poślizg), chyba że cena osiągnęła już TP → pominięcie (None).
    bars5: lista [ts, o, h, l, c] 5m. Zwraca (cena, 'maker'|'taker'|None)."""
    for b in bars5:
        if b[0] < t_open_ms:
            continue
        if b[0] >= t_open_ms + timeout_ms:
            if tp_px is not None and any(x[2] > tp_px for x in bars5 if t_open_ms <= x[0] < b[0]):
                return None, None
            return b[1] * (1 + slip), "taker"
        if b[3] < limit_px:
            return limit_px, "maker"
    return None, None


def round_step(x, step, mode="down"):
    n = x / step
    n = math.floor(n + 1e-9) if mode == "down" else round(n)
    dec = max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0
    return float(f"{n * step:.{dec}f}")
