"""Bot DEMO OKX: S2 + carry C2 (paper trading). Uruchomienie: python -m demo.bot  (GitHub Actions co 5–15 min).

Bezpieczeństwo: OKX_DEMO=1 wymagane; klucz musi być kluczem demo (okx.verify_demo_key) — inaczej koniec bez zleceń.
Sekrety tylko ze zmiennych środowiskowych: OKX_API_KEY, OKX_API_SECRET, OKX_API_PASSPHRASE, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID.
Stan: demo_state.json; dziennik: demo_log/*.csv.
"""
import json, os, sys, time, traceback
import requests

from . import journal, notify, s2
from .okx import OkxDemo, DemoGuardError, require_demo_env, candles
from .exchange import Exchange
from .s2_live import S2Live, INSTS, _iso
from .carry_live import CarryLive

STATE = os.getenv("DEMO_STATE", "demo_state.json")
HOST = os.getenv("OKX_API_HOST", "https://eea.okx.com")
ENABLE_S2 = os.getenv("ENABLE_S2", "1") == "1"
ENABLE_CARRY = os.getenv("ENABLE_CARRY", "1") == "1"


def load_state():
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(st):
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=1, sort_keys=True)
    os.replace(tmp, STATE)


def compare_s2(session, st, now_ms, bars_fn=None):
    """Backtest S2 (silnik 1:1 z research) na tych samych świecach od startu bota vs transakcje demo."""
    start = st.get("s2", {}).get("start_ms")
    if not start:
        return None
    demo = [t for t in journal.read("trades") if t["strategy"] == "S2"]
    rows = []
    for inst in INSTS:
        bars = (bars_fn or (lambda i: candles(session, i, "4H", 1500)))(inst)
        bt, _ = s2.simulate(bars, start_ts=start)
        bt = [t for t in bt if t["why"] != "END"]
        sig_bars = {_iso(t["entry_ts"] - s2.BAR_MS): t for t in bt}
        mine = {t["signal_bar"]: t for t in demo if t["inst"] == inst}
        matched = [(sig_bars[k]["R"], float(mine[k]["R"])) for k in mine if k in sig_bars]
        diff = sum(d - b for b, d in matched) / len(matched) if matched else 0.0
        rows.append(dict(day=_iso(now_ms)[:10], strategy="S2", inst=inst, bt_trades=len(bt), demo_trades=len(mine),
                         matched=len(matched), bt_sumR=round(sum(t["R"] for t in bt), 3),
                         demo_sumR=round(sum(float(t["R"]) for t in mine.values()), 3), avg_diff_R=round(diff, 4),
                         missing_in_demo=";".join(k for k in sig_bars if k not in mine)[:500],
                         extra_in_demo=";".join(k for k in mine if k not in sig_bars)[:500]))
    for r in rows:
        journal.append("compare", r)
    tot = dict(rows[0]); tot["inst"] = "RAZEM"
    m = sum(r["matched"] for r in rows)
    tot["matched"] = m
    tot["avg_diff_R"] = round(sum(r["avg_diff_R"] * r["matched"] for r in rows) / m, 4) if m else 0.0
    for k in ("bt_trades", "demo_trades", "bt_sumR", "demo_sumR"):
        tot[k] = round(sum(r[k] for r in rows), 3)
    tot["missing_in_demo"] = tot["extra_in_demo"] = ""
    journal.append("compare", tot)
    return tot


def daily_summary(ex, s2l, cl, st, cmp_row):
    eq_tot = ex.total_equity()
    s2eq, dd = s2l.mtm_equity() if s2l else (0, 0)
    tr = [t for t in journal.read("trades") if t["strategy"] == "S2"]
    rs = [float(t["R"]) for t in tr]
    pfv = journal.pf(rs) if rs else 0.0
    row = dict(day=time.strftime("%Y-%m-%d", time.gmtime()), total_eq=round(eq_tot, 2), s2_eq=round(s2eq, 2),
               s2_dd_pct=round(dd * 100, 2), carry_eq=round(cl.equity(), 2) if cl else "", s2_open=len(st.get("s2", {}).get("pos", {})),
               carry_open=len(st.get("carry", {}).get("pos", {})), s2_n=len(rs), s2_sumR=round(sum(rs), 3),
               s2_pf=round(pfv, 3) if rs else "", mode=f"S2:{st.get('s2', {}).get('mode')} C2:{st.get('carry', {}).get('mode')}")
    journal.append("daily", row)
    lines = [f"📊 Podsumowanie dnia (DEMO) {row['day']}",
             f"Konto demo: {eq_tot:,.0f} USD".replace(",", " "),
             f"S2: kapitał {s2eq:,.0f} | DD {dd:.1%} | transakcji {len(rs)} | suma {sum(rs):+.2f} R".replace(",", " ")
             + (f" | PF {pfv:.2f}" if rs else ""),
             f"S2 otwarte: {', '.join(st.get('s2', {}).get('pos', {}).keys()) or 'brak'}"]
    if cl:
        c = st.get("carry", {})
        lines.append(f"Carry C2: kapitał {cl.equity():,.0f} | pozycje: {', '.join(c.get('pos', {}).keys()) or 'brak'}".replace(",", " "))
    if cmp_row:
        lines.append(f"Backtest vs demo (S2): dopasowane {cmp_row['matched']} | śr. różnica {cmp_row['avg_diff_R']:+.2f} R")
    lines.append(f"Tryb: {row['mode']}")
    return "\n".join(lines)


def commands(session, st, send):
    if not (notify.TG_TOKEN and notify.TG_CHAT):
        return
    try:
        r = session.get(f"https://api.telegram.org/bot{notify.TG_TOKEN}/getUpdates",
                        params={"offset": st.get("_tg_offset", 0), "timeout": 0}, timeout=15).json()
    except Exception:  # noqa: BLE001
        return
    for u in r.get("result", []):
        st["_tg_offset"] = u["update_id"] + 1
        m = u.get("message") or {}
        if str(m.get("chat", {}).get("id")) != str(notify.TG_CHAT):
            continue
        txt = (m.get("text") or "").strip().lower()
        if txt.startswith("/stop"):
            for k in ("s2", "carry"):
                st.setdefault(k, {})["mode"] = "STOP"
            send("🛑 Tryb STOP włączony ręcznie (bez nowych wejść; SL/TP zostają na giełdzie).")
        elif txt.startswith("/status") or "podsum" in txt:
            st["_force_summary"] = True


def run(dry=False):
    require_demo_env()
    session = requests.Session()
    session.headers["User-Agent"] = "bot-swing-demo/2.0"
    st = load_state()
    now = int(time.time() * 1000)
    send = lambda t: notify.send(session, t, dry=dry)
    key, sec, pp = os.getenv("OKX_API_KEY", ""), os.getenv("OKX_API_SECRET", ""), os.getenv("OKX_API_PASSPHRASE", "")
    try:
        ex = Exchange(OkxDemo(session, HOST, key, sec, pp))
    except DemoGuardError as e:
        send(f"🛑 Bot DEMO zatrzymany (blokada bezpieczeństwa): {e}")
        print("DemoGuard:", e)
        sys.exit(2)
    commands(session, st, send)
    errors = []
    s2l = cl = None
    if ENABLE_S2:
        try:
            s2l = S2Live(ex, session, st, send)
            s2l.step()
        except Exception as e:  # noqa: BLE001
            errors.append(f"S2: {e}"); traceback.print_exc()
    if ENABLE_CARRY:
        try:
            cl = CarryLive(ex, session, st, send)
            cl.step()
        except Exception as e:  # noqa: BLE001
            errors.append(f"C2: {e}"); traceback.print_exc()
    # dzienne: porównanie z backtestem, kryteria STOP, podsumowanie
    day = now // 86400_000
    cmp_row = None
    if s2l and (st.get("_last_daily") != day or st.pop("_force_summary", False)):
        try:
            if st.get("_last_daily") != day:
                cmp_row = compare_s2(session, st, now)
            send(daily_summary(ex, s2l, cl, st, cmp_row))
            st["_last_daily"] = day
        except Exception as e:  # noqa: BLE001
            errors.append(f"podsumowanie: {e}")
    if s2l and st["s2"].get("mode") == "RUN":
        try:
            _, dd = s2l.mtm_equity()
            reasons = journal.kill_reasons([t for t in journal.read("trades") if t["strategy"] == "S2"], dd,
                                           [r for r in journal.read("signals") if r["strategy"] == "S2"],
                                           [r for r in journal.read("compare") if r["inst"] == "RAZEM"] or
                                           ([cmp_row] if cmp_row else None))
            if reasons:
                st["s2"]["mode"] = "STOP"; st["s2"]["stop_reason"] = reasons
                send("🛑 S2 → tryb STOP (kryteria R12):\n• " + "\n• ".join(reasons) +
                     "\nBez nowych wejść; otwarte pozycje mają SL/TP na giełdzie. Przegląd przed wznowieniem.")
        except Exception as e:  # noqa: BLE001
            errors.append(f"kryteria: {e}")
    if errors and now - st.get("_err_ms", 0) > 3 * 3600_000:
        send("⚠️ Bot DEMO — błąd:\n" + "\n".join(x[:300] for x in errors))
        st["_err_ms"] = now
    st["_last_run"] = _iso(now)
    save_state(st)
    print(f"{_iso(now)} UTC | błędów: {len(errors)}")
    for e in errors:
        print("  ", e)


def dry_report():
    """Bez kluczy i bez zleceń: bieżący stan reguł S2 i C2 z publicznych danych OKX."""
    session = requests.Session()
    session.headers["User-Agent"] = "bot-swing-demo/2.0"
    for inst in INSTS:
        b = candles(session, inst, "4H", 1500)
        ind = s2.indicators(b)
        i = len(b) - 1
        print(f"S2 {inst}: świeca {_iso(b[i][0])} close {b[i][4]} | max55 {ind['hh'][i]:.2f} | EMA300 {ind['ema'][i]:.2f} | "
              f"ATR {ind['atr'][i]:.2f} | SYGNAŁ: {'TAK' if ind['sig'][i] else 'nie'}")
    from .carry_live import PublicMarket, fav_from_settlements, decide, DAY
    m = PublicMarket(session)
    today = int(time.time() * 1000) // DAY * DAY
    uni = m.universe(today)[:20]
    fav = {b: fav_from_settlements(m.funding(f"{b}-USDT-SWAP", today - 9 * DAY), today) for b, _, _ in uni}
    print("C2 top 20 i funding 7 d (%/rok):", {b: round(f * 100, 1) for b, (f, ok) in fav.items()})
    print("C2 wejścia dziś wg reguły:", decide({}, [(b, fav[b][0], fav[b][1], rk) for b, _, rk in uni])[1])


if __name__ == "__main__":
    if "--dry" in sys.argv:
        dry_report()
    else:
        run()
