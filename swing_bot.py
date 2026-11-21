"""
Bot SWING dla OKX (konto demo) -> Telegram.

Co 5 min (cron-job.org -> GitHub Actions) sprawdza, czy zamknęła się nowa świeca 4H.
Jeśli tak, szuka sygnałów i przesuwa trailing stopy otwartych pozycji.
Z kluczami API konta demo sam otwiera pozycje (trader.py), bez nich wysyła sygnały.

Strategia v2: wybicie kanału Donchiana 4H, tylko LONG, z filtrem trendu pary
  LONG : zamknięcie 4H > najwyższe high z 40 poprzednich świec ORAZ zamknięcie > EMA200 (4H)
  SL startowy: 2 x ATR(14) 4H od wejścia (= 1R, od niego liczona wielkość)
  Wyjście: SL "miękki" = max(SL startowy, najniższe low z 20 ostatnich świec 4H), sprawdzany
           na ZAMKNIĘCIU świecy 4H rynku spot OKX -> zamknięcie pozycji rynkowo.
  SL awaryjny na koncie demo: 2R od wejścia (tylko na wypadek krachu między przebiegami).
  Maks. MAX_SAME_DIR (6) pozycji naraz. Bez TP.
  Dlaczego stop na zamknięciu: cena kontraktu X-Perp w demo odbiega od rynku i wybijała
  stopy z poślizgiem do -2R (np. ETH 11.2026), choć na rynku spot pozycja była na plusie.

Backtest (OKX spot 4H, 2019-2026, 20 par, prowizje+poślizg 0,08%/stronę, funding 0,01%/8h, ryzyko 0,5%):
  2019-23 (dobór parametrów):  PF 2,45 | 2024-11.2026 (poza próbą): PF 1,71, ok. +155%, obsunięcie 26%
  poprzednia strategia (L/S 20, trailing 5,5 ATR) w tym samym teście poza próbą: PF 1,27, obsunięcie 36%

Uruchomienie:
  python swing_bot.py              -> jeden przebieg (GitHub Actions)
  python swing_bot.py --dry        -> bez Telegrama i zleceń, wynik w konsoli
  python swing_bot.py --backtest   -> backtest strategii na historii 4H
"""
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import requests

# ------------------------------- USTAWIENIA -------------------------------
# pary z kontraktem X-Perp na koncie demo (SOL zniknął z demo w 11.2026)
PAIRS = ["BTC", "ETH", "XRP", "HYPE", "DOGE", "AVAX", "LINK", "SUI", "BCH", "DOT", "FIL", "NEAR", "UNI"]
CAPITAL = float(os.getenv("CAPITAL", "99400"))        # kapitał demo w USDC (do sygnałów bez kluczy)
RISK_PCT = float(os.getenv("RISK_PCT", "0.5"))        # ryzyko na transakcję w %
LEVERAGE = int(os.getenv("LEVERAGE", "3"))            # dźwignia (marża izolowana)
DON_N = 40                                             # kanał wejścia: liczba świec 4H
EXIT_N = 20                                            # kanał wyjścia: najniższe low z tylu świec 4H
TREND_EMA = 200                                        # long tylko nad EMA200 (4H) danej pary
STOP_ATR = 2.0                                         # SL startowy = 2 x ATR(14) 4H (= 1R)
HARD_R = 2.0                                           # SL awaryjny na giełdzie: 2R od wejścia
MAX_SAME_DIR = int(os.getenv("MAX_SAME_DIR", "6"))     # maks. pozycji bota w jedną stronę
TRAIL_ATR = 5.5                                        # tylko dla starych pozycji (strategia v1)
BTC_FILTER = int(os.getenv("BTC_FILTER", "0"))         # 1 = long tylko gdy BTC > EMA300 4H, short gdy pod
BTC_EMA = 300
H4 = 4 * 3600_000
STATE_FILE = os.getenv("STATE_FILE", "state.json")
OKX_HOSTS = ["https://www.okx.com", "https://my.okx.com", "https://eea.okx.com"]
TG_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")
# Automatyczny handel (tylko konto DEMO). Działa, gdy ustawione są trzy sekrety poniżej.
OKX_KEY = os.getenv("OKX_API_KEY", "")
OKX_SECRET = os.getenv("OKX_API_SECRET", "")
OKX_PASSPHRASE = os.getenv("OKX_API_PASSPHRASE", "")
MAX_POSITIONS = int(os.getenv("MAX_POSITIONS", "0"))   # maks. pozycji bota naraz; 0 = bez limitu

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
        cands = [d for d in data if d["instId"].startswith(f"{base}-USD_UM_XPERP") and d.get("state") == "live"]
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
def btc_regime(k_btc):
    """1 = BTC nad EMA300 (4H), -1 = pod, 0 = filtr wyłączony."""
    if not BTC_FILTER:
        return 0
    c = [x[4] for x in k_btc]
    return 1 if c[-1] > ema(c, BTC_EMA)[-1] else -1


def donchian_signal(k4, regime):
    """Sygnał LONG na ostatniej zamkniętej świecy 4H. Zwraca dict albo None."""
    if len(k4) < max(DON_N, TREND_EMA) + 50:
        return None
    last, prev = k4[-1], k4[-1 - DON_N:-1]
    hi = max(x[2] for x in prev)
    closes = [x[4] for x in k4]
    if not (last[4] > hi and last[4] > ema(closes, TREND_EMA)[-1] and regime >= 0):
        return None
    A = atr(k4)[-1]
    entry = last[4]
    return {"dir": "long", "entry": entry, "sl": entry - STOP_ATR * A, "dist": STOP_ATR * A,
            "hard_sl": entry - HARD_R * STOP_ATR * A, "atr": A, "level": hi, "bar_ts": last[0]}


def exit_level(k4):
    """Poziom wyjścia v2: najniższe low z EXIT_N ostatnich zamkniętych świec 4H."""
    return min(x[3] for x in k4[-EXIT_N:])


def trail_level(k4, direction):
    """Trailing v1 (tylko stare pozycje): zamknięcie -/+ 5,5 x ATR."""
    A, c = atr(k4)[-1], k4[-1][4]
    return c - TRAIL_ATR * A if direction == "long" else c + TRAIL_ATR * A


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
    arrow = "🟢 LONG" if s["dir"] == "long" else "🔴 SHORT"
    lines = [
        f"{arrow} {base} — wybicie kanału {DON_N} świec 4H (nad EMA{TREND_EMA})",
        f"Kontrakt: {inst or base + ' X-Perp'}",
        "",
        f"Wejście:  {fmt(s['entry'], tick)}  (rynek)",
        f"SL:       {fmt(s['sl'], tick)}  (2 x ATR 4H) — zamknij, gdy świeca 4H ZAMKNIE się pod nim",
        f"SL awaryjny na giełdzie: {fmt(s['hard_sl'], tick)}",
        f"Wyjście:  po każdej świecy 4H podnieś SL do najniższego low z {EXIT_N} świec (tylko w górę), bez TP",
        "",
        f"Ilość:    {contracts:,} kontraktów".replace(",", " ") if contracts else f"Ilość: {qty_coin:.4f} {base}",
        f"Wartość:  ~{notional:,.0f} USD | marża izolowana {LEVERAGE}x ≈ {notional / LEVERAGE:,.0f} USDC".replace(",", " "),
        f"Ryzyko:   ~{risk_usd:,.0f} USDC ({RISK_PCT}% kapitału)".replace(",", " "),
    ]
    if BTC_FILTER:
        lines.append(f"Filtr BTC: {'nad' if s['dir'] == 'long' else 'pod'} EMA300 4H")
    return "\n".join(lines)

def send_telegram(text):
    if not (TG_TOKEN and TG_CHAT):
        print("[brak TELEGRAM_TOKEN/CHAT_ID]\n" + text)
        return
    # błąd Telegrama nie może przerwać przebiegu: inaczej otwarta pozycja nie trafi do state.json
    for attempt in range(3):
        try:
            r = session.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                             json={"chat_id": TG_CHAT, "text": text}, timeout=15)
            if r.ok:
                return
            print("Telegram error:", r.text)
        except Exception as e:  # noqa: BLE001
            print("Telegram error:", e)
        time.sleep(2)


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
def check_commands(st, trader):
    """Odczytuje wiadomości wysłane do bota na Telegramie i odpowiada na komendy."""
    if not (TG_TOKEN and TG_CHAT):
        return
    r = session.get(f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates",
                    params={"offset": st.get("_tg_offset", 0), "timeout": 0}, timeout=15).json()
    for u in r.get("result", []):
        st["_tg_offset"] = u["update_id"] + 1
        m = u.get("message") or {}
        if str(m.get("chat", {}).get("id")) != str(TG_CHAT):
            continue                                       # ignoruj obcych
        txt = (m.get("text") or "").lower()
        if "podsum" in txt or txt.startswith("/status"):
            if trader:
                send_telegram(trader.summary())
            else:
                send_telegram("📊 Handel automatyczny jest wyłączony (brak kluczy OKX), brak pozycji bota.")
        elif txt.startswith("/start") or "pomoc" in txt:
            send_telegram("Komendy:\n• podsumowanie — stan konta, pozycje i wyniki bota\n"
                          "Bot odpowiada przy najbliższym uruchomieniu (do ok. 5 min).")


def make_trader(st):
    """Łączy się z kontem demo OKX; None, gdy brak kluczy."""
    if not (OKX_KEY and OKX_SECRET and OKX_PASSPHRASE):
        return None
    from trader import OkxDemo, Trader
    last = None
    for host in [os.getenv("OKX_API_HOST", "https://eea.okx.com"), "https://my.okx.com", "https://www.okx.com"]:
        try:
            return Trader(OkxDemo(session, host, OKX_KEY, OKX_SECRET, OKX_PASSPHRASE),
                          st, send_telegram, RISK_PCT, LEVERAGE, MAX_POSITIONS)
        except Exception as e:  # noqa: BLE001
            last = e
    raise RuntimeError(f"Nie udało się połączyć z kontem demo OKX: {last}")



# ------------------------------- PRZEBIEG -------------------------------
def update_trailing(st, trader, k4_by_base, errors):
    """Po zamknięciu świecy 4H zarządza wyjściem pozycji bota."""
    for inst, t in list(st.get("_trades", {}).items()):
        if t.get("strategy") not in ("donchian", "donchian2"):
            continue
        k4 = k4_by_base.get(t["base"])
        if not k4 or k4[-1][0] <= t.get("trail_bar", 0):
            continue
        if t["strategy"] == "donchian2":
            # v2: stop na zamknięciu świecy 4H rynku spot (jak w backteście), potem podniesienie poziomu
            try:
                if k4[-1][4] <= t["sl"]:
                    trader.close_trade(inst, t, f"zamknięcie 4H {k4[-1][4]:.6g} pod SL {t['sl']:.6g}")
                else:
                    t["sl"] = max(t["sl"], exit_level(k4))
                t["trail_bar"] = k4[-1][0]
            except Exception as e:  # noqa: BLE001
                errors.append(f"AUTO: wyjście {t['base']}: {e}")
            continue
        try:
            new = trail_level(k4, t["dir"])
            better = new > t["sl"] if t["dir"] == "long" else new < t["sl"]
            if better:
                trader.move_sl(inst, t, new)
            t["trail_bar"] = k4[-1][0]
        except Exception as e:  # noqa: BLE001
            errors.append(f"AUTO: trailing {t['base']}: {e}")


def run(dry=False):
    st = load_state()
    now_ms = int(time.time() * 1000)
    sent, errors = 0, []
    trader = None
    if not dry:
        try:
            trader = make_trader(st)
            if trader:
                trader.manage()
        except Exception as e:  # noqa: BLE001
            errors.append(f"AUTO: {e}")
            if now_ms - st.get("_auto_err_ms", 0) > 3 * 3600_000:
                send_telegram(f"⚠️ Bot swing: błąd handlu automatycznego\n{str(e)[:400]}")
                st["_auto_err_ms"] = now_ms
        try:
            check_commands(st, trader)
        except Exception as e:  # noqa: BLE001
            errors.append(f"AUTO: Telegram: {e}")

    # sygnały i trailing liczymy raz na każdą zamkniętą świecę 4H
    expected = now_ms // H4 * H4 - H4                     # początek ostatniej zamkniętej świecy 4H
    if dry or st.get("_last_4h") != expected:
        try:
            k_btc = candles("BTC-USDT", "4H", 1000)
            regime = btc_regime(k_btc)
        except Exception as e:  # noqa: BLE001
            k_btc, regime = None, None
            errors.append(f"BTC: {e}")
        k4_by_base, complete = {}, regime is not None and k_btc[-1][0] == expected
        for base in (PAIRS if regime is not None else []):
            try:
                k4 = k_btc if base == "BTC" else candles(f"{base}-USDT", "4H", 600)
                if not k4 or k4[-1][0] != expected:
                    complete = False                       # OKX jeszcze nie opublikował świecy
                    continue
                k4_by_base[base] = k4
                s = donchian_signal(k4, regime)
                if not s or st.get(base, {}).get("bar_ts") == s["bar_ts"]:
                    continue
                if any(t["base"] == base for t in st.get("_trades", {}).values()):
                    continue                               # pozycja na tej parze już trwa (jak w backteście)
                inst, ct_val, tick = contract_info(base)
                msg = build_message(base, s, inst, ct_val, tick)
                if trader:
                    try:
                        ok, info = trader.open_trade(base, s, s["entry"])
                        msg = info if ok else msg + f"\n\n⏭ Nie otwarto automatycznie: {info}"
                    except Exception as e:  # noqa: BLE001
                        msg += f"\n\n⚠️ Błąd przy otwieraniu: {str(e)[:300]}"
                if dry:
                    print(msg + "\n" + "-" * 40)
                else:
                    send_telegram(msg)
                    st[base] = {"bar_ts": s["bar_ts"], "sent_ms": now_ms, "dir": s["dir"]}
                sent += 1
            except Exception as e:  # noqa: BLE001
                errors.append(f"{base}: {e}")
                complete = False
        if trader:
            update_trailing(st, trader, k4_by_base, errors)
        if complete and not dry:
            st["_last_4h"] = expected
        if dry:
            print(f"BTC vs EMA300: {'nad' if regime == 1 else 'pod' if regime == -1 else 'filtr wyłączony'}")

    if not dry:
        save_state(st)
        # błąd tylko, gdy padło wszystko (np. brak dostępu do API) — maks. raz na 6 h
        if errors and len([e for e in errors if not e.startswith("AUTO")]) >= len(PAIRS) and now_ms - st.get("_err_ms", 0) > 6 * 3600_000:
            send_telegram("⚠️ Bot swing: brak danych z OKX.\n" + errors[0][:300])
            st["_err_ms"] = now_ms
            save_state(st)
    summary = f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC | sygnałów: {sent} | błędów: {len(errors)}"
    if not dry and os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch" and not os.getenv("QUIET"):
        auto = (f"Handel automatyczny (demo): WŁĄCZONY, pozycji bota: {len(st.get('_trades', {}))}/{MAX_POSITIONS or 'bez limitu'}"
                if trader else "Handel automatyczny: wyłączony (brak kluczy OKX)")
        send_telegram(f"✅ Bot swing działa (test ręczny)\nStrategia: v2 long-only, wybicie {DON_N} świec 4H nad EMA{TREND_EMA}, "
                      f"wyjście {EXIT_N} świec na zamknięciu 4H{' + filtr BTC' if BTC_FILTER else ''}\n{summary}\n{auto}")
    print(summary)
    for e in errors:
        print("  ", e)


# ------------------------------- BACKTEST -------------------------------
def backtest(bars=3000, fee=0.0008, fund_8h=0.0001):
    """Backtest v2 na pojedynczych parach: wejście na otwarciu następnej świecy, stop na zamknięciu 4H,
    SL awaryjny 2R w trakcie świecy, wyjście po kanale EXIT_N. Wynik w R po kosztach (bez limitu pozycji)."""
    total, n_all, wins = 0.0, 0, 0
    for base in PAIRS:
        try:
            k = candles(f"{base}-USDT", "4H", bars)
        except Exception as e:  # noqa: BLE001
            print(base, "błąd:", e)
            continue
        a, c_all = atr(k), [x[4] for x in k]
        e200 = ema(c_all, TREND_EMA)
        pos, res = None, []
        for i in range(max(DON_N, TREND_EMA), len(k) - 1):
            o, h, l, c = k[i][1:5]
            if pos:
                cat = l <= pos["hard"]
                if cat or c <= pos["s"]:
                    px = min(o, pos["hard"]) if cat else k[i + 1][1]
                    bars_held = i - pos["i"] + 1
                    cost = fee * (pos["e"] + px) + fund_8h * pos["e"] * bars_held / 2
                    res.append((px - pos["e"] - cost) / pos["r"])
                    pos = None
                else:
                    pos["s"] = max(pos["s"], min(x[3] for x in k[i - EXIT_N + 1:i + 1]))
            if not pos and c > max(x[2] for x in k[i - DON_N:i]) and c > e200[i]:
                e, r = k[i + 1][1], STOP_ATR * a[i]
                pos = {"e": e, "s": e - r, "hard": e - HARD_R * r, "r": r, "i": i + 1}
        if pos:
            res.append((k[-1][4] - pos["e"] - fee * 2 * pos["e"]) / pos["r"])
        total += sum(res)
        n_all += len(res)
        wins += sum(1 for x in res if x > 0)
        print(f"{base:5} transakcji: {len(res):3}  wynik: {sum(res):+6.1f} R")
    if n_all:
        print(f"\nRAZEM: {n_all} transakcji | trafność {wins / n_all:.0%} | wynik {total:+.1f} R "
              f"| śr. {total / n_all:+.2f} R | przy ryzyku {RISK_PCT}% ≈ {total * RISK_PCT:+.1f}% kapitału (bez procentu składanego)")


if __name__ == "__main__":
    if "--backtest" in sys.argv:
        backtest()
    else:
        run(dry="--dry" in sys.argv)
