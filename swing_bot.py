"""
Bot sygnałów SWING dla OKX (demo) -> Telegram.

Bot NIE składa zleceń. Co 15 min sprawdza zamknięte świece 1H/4H,
a gdy wszystkie warunki są spełnione, wysyła na Telegram gotowy setup:
kierunek, wejście, SL, TP1, trailing stop i ilość kontraktów.

Strategia (z trendem):
  1. Trend 4H: EMA50 > EMA200, cena > EMA200 i EMA200 rośnie  -> tylko LONG
               EMA50 < EMA200, cena < EMA200 i EMA200 spada  -> tylko SHORT
  2. Korekta 1H: w ostatnich 3 świecach cena dotknęła strefy EMA21(1H) +/- 0,5 ATR
  3. Potwierdzenie: ostatnia zamknięta świeca 1H zamknęła się po właściwej
     stronie EMA21, w kierunku trendu, a RSI(14) mieści się w 40-65 (long) / 35-60 (short)
  4. SL: za lokalnym ekstremum z 10 świec 1H + 0,3 ATR, min. 1,5 ATR, maks. 3 ATR
  5. TP1 = 1,5R -> zamknij 30%, SL na wejście; reszta jedzie z trailing stopem 3 ATR(1H)
  6. Wielkość pozycji = RISK_PCT kapitału / odległość do SL

Backtest (10 par, ~166 dni 1H do 24.10.2026, opłaty 0,1R/transakcję):
  589 transakcji, śr. +0,14R; obie połowy okresu na plusie (+0,14R i +0,15R).

Uruchomienie:
  python swing_bot.py              -> jeden przebieg (GitHub Actions)
  python swing_bot.py --dry        -> bez Telegrama, wynik w konsoli
  python swing_bot.py --backtest   -> prosty backtest strategii na historii 1H/4H
"""
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import requests

# ------------------------------- USTAWIENIA -------------------------------
PAIRS = ["BTC", "ETH", "SOL", "XRP", "HYPE", "DOGE", "AVAX", "LINK", "SUI", "BCH"]
CAPITAL = float(os.getenv("CAPITAL", "99400"))        # kapitał demo w USDC
RISK_PCT = float(os.getenv("RISK_PCT", "0.5"))        # ryzyko na transakcję w %
LEVERAGE = int(os.getenv("LEVERAGE", "3"))            # sugerowana dźwignia (izolowana)
COOLDOWN_H = 12                                        # min. przerwa między sygnałami na tej samej parze
SIGNAL_VALID_H = 2                                     # jak długo setup jest aktualny
MAX_SIGNALS_PER_RUN = 2                                # pary są skorelowane -> maks. 2 sygnały naraz
TREND_FAST, TREND_SLOW = 50, 200                      # EMA na 4H
TP1_R, TP1_PART = 1.5, 0.3                             # TP1 przy 1,5R zamyka 30% pozycji
TRAIL_ATR = 3.0                                        # trailing stop reszty = 3 x ATR(1H)
SL_MIN_ATR, SL_MAX_ATR, SL_BUFFER_ATR = 1.5, 3.0, 0.3
STATE_FILE = os.getenv("STATE_FILE", "state.json")
OKX_HOSTS = ["https://www.okx.com", "https://my.okx.com", "https://eea.okx.com"]
TG_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")
# --------------------------------------------------------------------------

session = requests.Session()
session.headers.update({"User-Agent": "swing-signal-bot/1.0"})
_host = None


def okx_get(path, params=None, demo=False):
    """GET do publicznego API OKX; próbuje kolejnych domen, aż któraś odpowie."""
    global _host
    hosts = [_host] if _host else OKX_HOSTS
    headers = {"x-simulated-trading": "1"} if demo else {}
    last_err = None
    for host in hosts:
        for attempt in range(3):
            try:
                r = session.get(host + path, params=params, headers=headers, timeout=15)
                if r.status_code == 429:
                    time.sleep(1 + attempt)
                    continue
                r.raise_for_status()
                js = r.json()
                if js.get("code") != "0":
                    raise RuntimeError(f"OKX {js.get('code')}: {js.get('msg')}")
                _host = host
                return js["data"]
            except Exception as e:  # noqa: BLE001
                last_err = e
                time.sleep(0.5)
    raise RuntimeError(f"Brak dostępu do API OKX ({path}): {last_err}")


def candles(inst, bar, limit=300):
    """Zamknięte świece (najstarsza pierwsza): [ts, o, h, l, c]."""
    out, after = [], None
    while len(out) < limit:
        p = {"instId": inst, "bar": bar, "limit": "100"}
        if after:
            p["after"] = after
        data = okx_get("/api/v5/market/history-candles", p)
        if not data:
            break
        out += data
        after = data[-1][0]
        time.sleep(0.12)
    rows = [[int(c[0]), float(c[1]), float(c[2]), float(c[3]), float(c[4])]
            for c in out if c[8] == "1"]           # c[8] == "1" -> świeca zamknięta
    rows.sort(key=lambda x: x[0])
    return rows[-limit:]


def contract_info(base):
    """Kontrakt X-Perp z konta demo (ctVal = ile coina w 1 kontrakcie)."""
    try:
        data = okx_get("/api/v5/public/instruments", {"instType": "FUTURES"}, demo=True)
        cands = [d for d in data if d["instId"].startswith(f"{base}-USD_UM_XPERP")]
        if cands:
            d = cands[0]
            return d["instId"], float(d["ctVal"]), float(d["tickSz"])
    except Exception:  # noqa: BLE001
        pass
    return None, None, None


# ------------------------------- WSKAŹNIKI -------------------------------
def ema(vals, p):
    k, e, out = 2 / (p + 1), vals[0], []
    for v in vals:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def rsi(closes, p=14):
    out = [50.0] * len(closes)
    if len(closes) <= p:
        return out
    g = l = 0.0
    for i in range(1, p + 1):
        d = closes[i] - closes[i - 1]
        g += max(d, 0)
        l += max(-d, 0)
    g, l = g / p, l / p
    for i in range(p + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        g = (g * (p - 1) + max(d, 0)) / p
        l = (l * (p - 1) + max(-d, 0)) / p
        out[i] = 100.0 if l == 0 else 100 - 100 / (1 + g / l)
    return out


def atr(k, p=14):
    trs = [k[0][2] - k[0][3]]
    for i in range(1, len(k)):
        h, l, pc = k[i][2], k[i][3], k[i - 1][4]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    out, a = [], sum(trs[:p]) / p
    for i, t in enumerate(trs):
        if i >= p:
            a = (a * (p - 1) + t) / p
        out.append(a)
    return out


# ------------------------------- STRATEGIA -------------------------------
def trend_4h(k4, idx=None):
    """'long' / 'short' / None na podstawie zamkniętej świecy 4H o indeksie idx."""
    idx = len(k4) - 1 if idx is None else idx
    if idx < TREND_SLOW + 10:
        return None
    c = [x[4] for x in k4[: idx + 1]]
    f, s = ema(c, TREND_FAST), ema(c, TREND_SLOW)
    slope = s[-1] - s[-6]
    trend_4h.strength = abs(slope) / s[-1]                 # siła trendu (do wyboru najlepszych par)
    if f[-1] > s[-1] and c[-1] > s[-1] and slope > 0:
        return "long"
    if f[-1] < s[-1] and c[-1] < s[-1] and slope < 0:
        return "short"
    return None


def setup_1h(k1, direction):
    """Sprawdza setup na ostatniej zamkniętej świecy 1H. Zwraca dict albo None."""
    if direction is None or len(k1) < 60:
        return None
    c = [x[4] for x in k1]
    e21, r, a = ema(c, 21), rsi(c), atr(k1)
    last, A = k1[-1], a[-1]
    zone = 0.5 * A
    recent = k1[-3:]
    if direction == "long":
        touched = any(x[3] <= e21[-1 - (2 - i)] + zone for i, x in enumerate(recent))
        confirm = last[4] > e21[-1] and last[4] > last[1] and 40 <= r[-1] <= 65 and r[-1] > r[-2]
        if not (touched and confirm):
            return None
        entry = last[4]
        swing = min(x[3] for x in k1[-10:])
        dist = entry - swing + SL_BUFFER_ATR * A
    else:
        touched = any(x[2] >= e21[-1 - (2 - i)] - zone for i, x in enumerate(recent))
        confirm = last[4] < e21[-1] and last[4] < last[1] and 35 <= r[-1] <= 60 and r[-1] < r[-2]
        if not (touched and confirm):
            return None
        entry = last[4]
        swing = max(x[2] for x in k1[-10:])
        dist = swing - entry + SL_BUFFER_ATR * A
    dist = max(dist, SL_MIN_ATR * A)
    if dist > SL_MAX_ATR * A:
        return None                      # stop za daleko -> słaby R:R, pomijamy
    s = 1 if direction == "long" else -1
    return {
        "dir": direction, "entry": entry, "sl": entry - s * dist,
        "tp1": entry + s * TP1_R * dist,
        "trail": TRAIL_ATR * A, "cb_pct": TRAIL_ATR * A / entry * 100,
        "dist": dist, "atr": A, "rsi": r[-1], "ema21": e21[-1], "bar_ts": last[0],
    }


# ------------------------------- WYJŚCIE -------------------------------
def fmt(x, tick=None):
    if tick:
        dec = max(0, len(f"{tick:.10f}".rstrip("0").split(".")[1]))
        return f"{round(x / tick) * tick:,.{dec}f}".replace(",", " ")
    for d in (2, 4, 6, 8):
        if abs(x) >= 10 ** (2 - d) or d == 8:
            return f"{x:,.{d}f}".replace(",", " ")


def build_message(base, s, inst, ct_val, tick):
    risk_usd = CAPITAL * RISK_PCT / 100
    qty_coin = risk_usd / s["dist"]
    notional = qty_coin * s["entry"]
    contracts = int(qty_coin / ct_val) if ct_val else None
    valid_to = datetime.fromtimestamp(s["bar_ts"] / 1000, tz=timezone.utc) + timedelta(hours=1 + SIGNAL_VALID_H)
    valid_pl = valid_to.astimezone(ZoneInfo("Europe/Warsaw")).strftime("%d.%m %H:%M")
    arrow = "🟢 LONG" if s["dir"] == "long" else "🔴 SHORT"
    button = "Kup (długie)" if s["dir"] == "long" else "Sprzedaj (krótkie)"
    lines = [
        f"{arrow} {base} — sygnał SWING",
        f"Kontrakt: {inst or base + ' X-Perp'}",
        "",
        f"Wejście:  {fmt(s['entry'], tick)}  (limit lub rynek)",
        f"SL:       {fmt(s['sl'], tick)}",
        f"TP1:      {fmt(s['tp1'], tick)}  (1,5R — zamknij 30%, SL przesuń na wejście)",
        f"Trailing: reszta (70%) — aktywacja {fmt(s['tp1'], tick)}, callback {s['cb_pct']:.1f}%",
        "          (bez górnego TP — trailing zamknie pozycję)",
        "",
        f"Ilość:    {contracts:,} kontraktów".replace(",", " ") if contracts else f"Ilość: {qty_coin:.4f} {base}",
        f"Wartość:  ~{notional:,.0f} USD | marża izolowana {LEVERAGE}x ≈ {notional / LEVERAGE:,.0f} USDC".replace(",", " "),
        f"Ryzyko:   ~{risk_usd:,.0f} USDC ({RISK_PCT}% kapitału)".replace(",", " "),
        "",
        f"Trend 4H (EMA50/200): {'wzrostowy' if s['dir'] == 'long' else 'spadkowy'} | RSI 1H {s['rsi']:.0f} | ATR 1H {fmt(s['atr'])}",
        f"Ważny do: {valid_pl} — potem nie wchodź",
        f"Przycisk: {button}, zaznacz TP/SL (TP1 na 30% ilości), potem dodaj Trailing stop",
    ]
    return "\n".join(lines)


def send_telegram(text):
    if not (TG_TOKEN and TG_CHAT):
        print("[brak TELEGRAM_TOKEN/CHAT_ID]\n" + text)
        return
    r = session.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                     json={"chat_id": TG_CHAT, "text": text}, timeout=15)
    if not r.ok:
        print("Telegram error:", r.text)


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(st):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=1, sort_keys=True)


# ------------------------------- PRZEBIEG -------------------------------
def run(dry=False):
    st = load_state()
    now_ms = int(time.time() * 1000)
    sent, errors, cands = 0, [], []
    for base in PAIRS:
        spot = f"{base}-USDT"
        try:
            k4 = candles(spot, "4H", 320)
            k1 = candles(spot, "1H", 200)
            # świeca 1H musi być świeża (zamknięta < 75 min temu), inaczej nie wysyłamy
            if not k1 or now_ms - (k1[-1][0] + 3600_000) > 75 * 60_000:
                continue
            s = setup_1h(k1, trend_4h(k4))
            if not s:
                continue
            prev = st.get(base, {})
            if prev.get("bar_ts") == s["bar_ts"]:
                continue                                   # ten sam sygnał już rozpatrzony
            if now_ms - prev.get("sent_ms", 0) < COOLDOWN_H * 3600_000:
                continue
            cands.append((trend_4h.strength, base, s))
        except Exception as e:  # noqa: BLE001
            errors.append(f"{base}: {e}")
    cands.sort(key=lambda x: x[0], reverse=True)            # najsilniejszy trend 4H pierwszy
    skipped = [b for _, b, _ in cands[MAX_SIGNALS_PER_RUN:]]
    for _, base, s in cands[:MAX_SIGNALS_PER_RUN]:
        inst, ct_val, tick = contract_info(base)
        msg = build_message(base, s, inst, ct_val, tick)
        if skipped:
            msg += f"\n\nTen sam sygnał dały też: {', '.join(skipped)} — pominięte (te same ruchy rynku)."
        if dry:
            print(msg + "\n" + "-" * 40)
        else:
            send_telegram(msg)
            st[base] = {"bar_ts": s["bar_ts"], "sent_ms": now_ms, "dir": s["dir"]}
        sent += 1
    for _, base, s in cands[MAX_SIGNALS_PER_RUN:]:
        if not dry:                                        # pominięte nie wrócą przy tej samej świecy
            st[base] = {"bar_ts": s["bar_ts"], "sent_ms": st.get(base, {}).get("sent_ms", 0), "dir": s["dir"]}
    if not dry:
        save_state(st)
        # błąd tylko, gdy padło wszystko (np. brak dostępu do API) — maks. raz na 6 h
        if errors and len(errors) == len(PAIRS) and now_ms - st.get("_err_ms", 0) > 6 * 3600_000:
            send_telegram("⚠️ Bot swing: brak danych z OKX.\n" + errors[0][:300])
            st["_err_ms"] = now_ms
            save_state(st)
    summary = f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC | sygnałów: {sent} | błędów: {len(errors)}"
    if not dry and os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch":
        send_telegram(f"✅ Bot swing działa (test ręczny)\n{summary}\nPar sprawdzonych: {len(PAIRS) - len(errors)}/{len(PAIRS)}")
    print(summary)
    for e in errors:
        print("  ", e)


# ------------------------------- BACKTEST -------------------------------
def backtest(bars_1h=2000):
    """Backtest: wejście na zamknięciu świecy sygnałowej, 30% na TP1 (SL -> BE), reszta trailing 3 ATR."""
    total_r, trades, wins = 0.0, 0, 0
    for base in PAIRS:
        spot = f"{base}-USDT"
        try:
            k1 = candles(spot, "1H", bars_1h)
            k4 = candles(spot, "4H", bars_1h // 4 + 260)
        except Exception as e:  # noqa: BLE001
            print(base, "błąd:", e)
            continue
        pair_r, n, i, cool_until = 0.0, 0, 120, 0
        j4 = 0
        while i < len(k1) - 1:
            ts = k1[i][0]
            while j4 + 1 < len(k4) and k4[j4 + 1][0] + 4 * 3600_000 <= ts + 3600_000:
                j4 += 1
            if ts < cool_until:
                i += 1
                continue
            s = setup_1h(k1[i - 119: i + 1], trend_4h(k4, j4))
            if not s:
                i += 1
                continue
            long_ = s["dir"] == "long"
            r, half, j = 0.0, False, i + 1
            sl, a1 = s["sl"], atr(k1)
            rest = 1 - TP1_PART
            while j < len(k1):
                h, l, c = k1[j][2], k1[j][3], k1[j][4]
                if (l <= sl) if long_ else (h >= sl):       # konserwatywnie: SL przed TP w tej samej świecy
                    move = (sl - s["entry"]) if long_ else (s["entry"] - sl)
                    r += (rest if half else 1.0) * move / s["dist"]
                    break
                if not half and ((h >= s["tp1"]) if long_ else (l <= s["tp1"])):
                    r += TP1_PART * TP1_R
                    half, sl = True, s["entry"]
                if half:                                    # trailing liczony od zamknięcia świecy
                    sl = max(sl, c - TRAIL_ATR * a1[j]) if long_ else min(sl, c + TRAIL_ATR * a1[j])
                j += 1
            else:
                last = k1[-1][4]
                r += (rest if half else 1.0) * ((last - s["entry"]) if long_ else (s["entry"] - last)) / s["dist"]
            r -= 0.1                                        # opłaty + poślizg ≈ 0,1R
            pair_r += r
            n += 1
            wins += r > 0
            cool_until = k1[min(j, len(k1) - 1)][0] + COOLDOWN_H * 3600_000
            i = j + 1
        trades += n
        total_r += pair_r
        print(f"{base:5} transakcji: {n:3}  wynik: {pair_r:+6.1f} R")
    if trades:
        print(f"\nRAZEM: {trades} transakcji | win rate {wins / trades:.0%} | "
              f"wynik {total_r:+.1f} R | śr. {total_r / trades:+.2f} R/transakcję "
              f"| przy ryzyku {RISK_PCT}% ≈ {total_r * RISK_PCT:+.1f}% kapitału")


if __name__ == "__main__":
    if "--backtest" in sys.argv:
        backtest()
    else:
        run(dry="--dry" in sys.argv)
