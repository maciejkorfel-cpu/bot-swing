"""Carry C2 (research_crypto2/finalists_c2.py, zamrożone 2026-11-23) na koncie demo.

Reguła 1:1 (h2_carry.run, tryb 'unified'):
  - uniwersum: perpy OKX USDT-SWAP (krypto, bez stablecoinów/złota/wrapped), ≥ 60 dni od listingu, z parą spot USDT,
    |perp/spot − 1| < 5%; ranking wg średniego dziennego wolumenu (USDT) z 30 dni; top 20;
  - decyzja raz dziennie po zamknięciu dnia UTC t: fav = średni funding z dni t−6..t (suma dzienna, ×365);
    wymagane 7 dni danych;
  - wyjście: fav < 0 (X = 0) albo brak 7 dni danych;  wejście: fav ≥ 10%/rok, malejąco wg fav, maks. K = 10 pozycji;
  - pozycja: nominał P = (kapitał carry / K) / 1,1; long spot P + short perp P (konto ujednolicone: spot = zabezpieczenie);
Wykonanie demo (research_edge cz. 2): obie nogi jako limit post-only (spot kupno po bid, perp sprzedaż po ask), ważne 1 h,
potem reszta rynkowo; kontrola obu nóg: gdy jedna noga się nie wypełni → druga jest zamykana + alarm.
Odchylenia od backtestu zapisywane w demo_log/carry_demo.csv: coin bez instrumentu w demo jest pomijany (bez zastępstwa).
"""
import os, time
from collections import defaultdict
from . import journal
from .okx import public_get, funding_history
from .s2 import round_step

EXCL = {"USDC", "DAI", "TUSD", "FDUSD", "USDE", "USD1", "BUSD", "PYUSD", "RLUSD", "USDP", "USDD", "UST", "USTC", "EUR",
        "EURC", "EURI", "PAXG", "XAUT", "WBTC", "STETH", "WBETH", "BETH", "CBBTC", "USDS", "USDG", "SUSDE", "GUSD"}
CFG = dict(D=7, E=0.10, X=0.0, K=10, topn=20, capmult=1.1, min_age_d=60)
LEG_TIMEOUT_MS = 3600_000
DAY = 86400_000
CARRY_FRAC = float(os.getenv("CARRY_FRAC", "0.8"))
CARRY_MAX_DD = float(os.getenv("CARRY_MAX_DD", "0.03"))


def _now():
    return int(time.time() * 1000)


def _iso(ms):
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(ms / 1000)) if ms else ""


# ---------------------------------------------------------------------- reguła (czysta, testowalna)
def fav_from_settlements(settle, day_end_ms, D=CFG["D"]):
    """settle: lista (ts_ms, stopa). Dni UTC [day_end−D·DAY, day_end). Zwraca (fav roczny, ok)."""
    days = defaultdict(float); seen = set()
    for ts, r in settle:
        if day_end_ms - D * DAY <= ts < day_end_ms:
            d = ts // DAY
            days[d] += r; seen.add(d)
    ok = len(seen) >= D
    return (sum(days.values()) / D * 365 if ok else float("nan")), ok


def decide(held, cand, K=CFG["K"], E=CFG["E"], X=CFG["X"]):
    """held: {base: (fav, ok)}, cand: lista (base, fav, ok, rank). Zwraca (wyjścia, wejścia w kolejności)."""
    exits = [b for b, (f, ok) in held.items() if (not ok) or f < X]
    stay = len(held) - len(exits)
    free = K - stay
    c = sorted([(f, b) for b, f, ok, rk in cand if b not in held and ok and rk <= CFG["topn"] and f >= E], reverse=True)
    return exits, [b for _, b in c[:max(free, 0)]]


class CarryLive:
    def __init__(self, ex, session, st, notify, now=None, market=None):
        self.ex, self.s, self.notify = ex, session, notify
        self.now = now or _now
        self.market = market or PublicMarket(session)
        self.st = st.setdefault("carry", {})
        for k, v in dict(pos={}, pending={}, realized=0.0, mode="RUN", last_day=None).items():
            self.st.setdefault(k, v)

    def equity(self):
        return self.st["base"] + self.st["realized"]

    def step(self):
        if "base" not in self.st:
            self.st["base"] = float(self.ex.total_equity()) * CARRY_FRAC
            self.st["peak"] = self.st["base"]
        if self.ex.acct_lv not in ("3", "4"):
            if not self.st.get("warned_acct"):
                self.notify("⚠️ Carry C2 wyłączony: konto demo nie jest w trybie multi-currency/portfolio margin "
                            f"(acctLv={self.ex.acct_lv}). Spot nie zabezpiecza nogi perp. Zmień tryb konta w ustawieniach "
                            "Demo trading (Ustawienia → Tryb konta) i daj znać.")
                self.st["warned_acct"] = True
            return
        self._pending()
        self._hedge_check()
        today = self.now() // DAY * DAY
        if self.st["last_day"] != today and self.now() - today >= 10 * 60000:   # 10 min po północy UTC
            self._daily(today)
            self.st["last_day"] = today
        eq = self.equity()
        self.st["peak"] = max(self.st.get("peak", eq), eq)
        dd = 1 - eq / self.st["peak"]
        if dd > CARRY_MAX_DD and self.st["mode"] == "RUN":
            self.st["mode"] = "STOP"
            self.notify(f"🛑 Carry C2 → STOP: obsunięcie {dd:.1%} > {CARRY_MAX_DD:.0%} (backtest < 1%). Bez nowych wejść.")

    # ------------------------------------------------------------- decyzja dzienna
    def _daily(self, today):
        uni = self.market.universe(today)                     # [(base, swap_inst, rank)]
        bases = {b for b, _, _ in uni} | set(self.st["pos"])
        favs = {}
        for b in bases:
            settle = self.market.funding(f"{b}-USDT-SWAP", today - 9 * DAY)
            favs[b] = fav_from_settlements(settle, today)
        held = {b: favs.get(b, (float("nan"), False)) for b in self.st["pos"]}
        cand = [(b, favs[b][0], favs[b][1], rk) for b, _, rk in uni]
        exits, entries = decide(held, cand)
        for b in exits:
            if b in self.st["pending"]:
                continue
            self._start_exit(b, held[b][0])
        if self.st["mode"] != "RUN":
            return
        free_cash_ok = True
        for b in entries:
            if b in self.st["pending"]:
                continue
            perp, spot = self.ex.perp_inst(b), self.ex.spot_inst(b, ("USDT", "USDC"))
            if not perp or not spot:
                journal.append("carry", dict(event="skipped_no_inst", ts=_iso(self.now()), base=b, fav_7d=round(favs[b][0], 4),
                                             detail="brak X-Perp/spot w demo — w backteście ta pozycja byłaby otwarta"))
                continue
            if free_cash_ok:
                free_cash_ok = self._start_entry(b, perp, spot, favs[b][0])

    def _start_entry(self, b, perp, spot, fav):
        slot = self.equity() / CFG["K"]
        P = slot / CFG["capmult"]
        bid_s, ask_s, _ = self.ex.book_top(spot["instId"])
        bid_p, ask_p, _ = self.ex.book_top(perp["instId"])
        if not (bid_s and ask_p):
            return True
        q = P / ask_s
        contracts = round_step(q / perp["ctVal"], perp["lot"])
        qs = round_step(contracts * perp["ctVal"], spot["lot"])
        if contracts < perp["minSz"] or qs < spot["minSz"]:
            journal.append("carry", dict(event="skipped_size", ts=_iso(self.now()), base=b, detail="za mała ilość"))
            return True
        if self.ex.spot_avail(spot["quote"]) < qs * ask_s * 1.01:
            journal.append("carry", dict(event="skipped_cash", ts=_iso(self.now()), base=b, detail="za mało gotówki"))
            return False
        so = self.ex.place(spot["instId"], "buy", qs, "post_only", px=bid_s, spot=True)
        try:
            po = self.ex.place(perp["instId"], "sell", contracts, "post_only", px=ask_p)
        except Exception as e:  # noqa: BLE001
            self.ex.cancel(spot["instId"], so)
            self._flatten_leg(spot, perp, b, "spot", self.ex.get_order(spot["instId"], so)["filled"], 0.0)
            raise RuntimeError(f"carry {b}: noga perp odrzucona ({e}); noga spot anulowana/zamknięta")
        self.st["pending"][b] = dict(kind="entry", spot=spot, perp=perp, qs=qs, contracts=contracts, so=so, po=po,
                                     deadline=self.now() + LEG_TIMEOUT_MS, fav=fav, t0=self.now(), fills={"spot": [], "perp": []})
        return True

    def _start_exit(self, b, fav):
        p = self.st["pos"][b]
        bid_s, ask_s, _ = self.ex.book_top(p["spot"]["instId"])
        bid_p, ask_p, _ = self.ex.book_top(p["perp"]["instId"])
        so = self.ex.place(p["spot"]["instId"], "sell", p["qs"], "post_only", px=ask_s, spot=True)
        po = self.ex.place(p["perp"]["instId"], "buy", p["contracts"], "post_only", px=bid_p, reduce_only=True)
        self.st["pending"][b] = dict(kind="exit", spot=p["spot"], perp=p["perp"], qs=p["qs"], contracts=p["contracts"],
                                     so=so, po=po, deadline=self.now() + LEG_TIMEOUT_MS, fav=fav, t0=self.now(),
                                     fills={"spot": [], "perp": []})

    # ------------------------------------------------------------- obie nogi
    def _pending(self):
        for b, q in list(self.st["pending"].items()):
            os_ = self.ex.get_order(q["spot"]["instId"], q["so"])
            op = self.ex.get_order(q["perp"]["instId"], q["po"])
            done_s = os_["filled"] >= q["qs"] - 1e-12
            done_p = op["filled"] >= q["contracts"] - 1e-12
            if not (done_s and done_p) and self.now() < q["deadline"]:
                continue
            spot_side = "buy" if q["kind"] == "entry" else "sell"
            perp_side = "sell" if q["kind"] == "entry" else "buy"
            fills = {"spot": [(os_["filled"], os_["avg"], _fee_usd(os_))], "perp": [(op["filled"], op["avg"], _fee_usd(op))]}
            net_coin = -_fee_coin(os_, b)
            err = None
            for leg, inst, o, need, side, oid in (("spot", q["spot"], os_, q["qs"], spot_side, q["so"]),
                                                  ("perp", q["perp"], op, q["contracts"], perp_side, q["po"])):
                if o["filled"] >= need - 1e-12:
                    continue
                self.ex.cancel(inst["instId"], oid)
                o2 = self.ex.get_order(inst["instId"], oid)
                fills[leg] = [(o2["filled"], o2["avg"], _fee_usd(o2))]
                if leg == "spot":
                    net_coin = -_fee_coin(o2, b)
                rest = round_step(need - o2["filled"], inst["lot"])
                if rest >= inst["minSz"]:
                    try:
                        mo = self.ex.place(inst["instId"], side, rest, "market", spot=(leg == "spot"),
                                           reduce_only=(leg == "perp" and q["kind"] == "exit"))
                        m = self.ex.get_order(inst["instId"], mo)
                        fills[leg].append((m["filled"], m["avg"], _fee_usd(m)))
                        if leg == "spot":
                            net_coin -= _fee_coin(m, b)
                    except Exception as e:  # noqa: BLE001
                        err = f"{leg}: {e}"
            fs = sum(f[0] for f in fills["spot"]); fp = sum(f[0] for f in fills["perp"]) * q["perp"]["ctVal"]
            del self.st["pending"][b]
            if q["kind"] == "entry":
                if err or abs(fs - fp) > 0.02 * max(fs, fp, 1e-12):
                    self._flatten_leg(q["spot"], q["perp"], b, "both", fs, sum(f[0] for f in fills["perp"]))
                    self.notify(f"🚨 Carry {b}: noga nie wypełniona / niezgodna (spot {fs:g} vs perp {fp:g} coina). "
                                f"Druga noga zamknięta. {err or ''}")
                    journal.append("carry", dict(event="leg_fail", ts=_iso(self.now()), base=b, detail=f"{err or ''} spot={fs} perp={fp}"))
                    continue
                avg = lambda L: sum(a * p for a, p, _ in L) / max(sum(a for a, _, _ in L), 1e-12)
                fee = sum(f[2] for f in fills["spot"]) + sum(f[2] for f in fills["perp"])
                self.st["pos"][b] = dict(spot=q["spot"], perp=q["perp"], qs=round_step(fs + net_coin, q["spot"]["lot"]), contracts=sum(f[0] for f in fills["perp"]),
                                         spot_px=avg(fills["spot"]), perp_px=avg(fills["perp"]), fees=fee, t0=self.now(),
                                         fav=q["fav"], legs_delay_min=round((self.now() - q["t0"]) / 60000, 1))
                journal.append("carry", dict(event="open", ts=_iso(self.now()), base=b, perp_inst=q["perp"]["instId"],
                                             spot_inst=q["spot"]["instId"], qty=fs, spot_px=avg(fills["spot"]),
                                             perp_px=avg(fills["perp"]), fav_7d=round(q["fav"], 4), fees=round(fee, 4)))
                self.notify(f"🟢 Carry {b} otwarty (demo): long spot {fs:g} @ {avg(fills['spot']):.6g} + short "
                            f"{q['perp']['instId']} @ {avg(fills['perp']):.6g} | funding 7 d {q['fav']:.0%}/rok")
            else:
                p = self.st["pos"].pop(b)
                if err or abs(fs - fp) > 0.02 * max(fs, fp, 1e-12):
                    self._flatten_leg(q["spot"], q["perp"], b, "both", p["qs"] - fs, p["contracts"] - sum(f[0] for f in fills["perp"]))
                    self.notify(f"🚨 Carry {b}: problem przy zamykaniu nóg ({err or 'niezgodne ilości'}) — zamknięto rynkowo resztę.")
                avg = lambda L: sum(a * pp for a, pp, _ in L) / max(sum(a for a, _, _ in L), 1e-12)
                sx, fx = avg(fills["spot"]), avg(fills["perp"])
                fund = self.ex.funding_received(p["perp"]["instId"], p["t0"])
                fee = p["fees"] + sum(f[2] for f in fills["spot"]) + sum(f[2] for f in fills["perp"])
                pnl = p["qs"] * (sx - p["spot_px"]) - p["contracts"] * p["perp"]["ctVal"] * (fx - p["perp_px"]) + fund - fee
                self.st["realized"] += pnl
                journal.append("carry", dict(event="close", ts=_iso(self.now()), base=b, perp_inst=p["perp"]["instId"],
                                             spot_inst=p["spot"]["instId"], qty=p["qs"], spot_px=sx, perp_px=fx,
                                             fav_7d=round(q["fav"], 4), funding_recv=round(fund, 4), pnl=round(pnl, 4),
                                             fees=round(fee, 4), detail=f"otwarty {_iso(p['t0'])}"))
                self.notify(f"🏁 Carry {b} zamknięty (demo): wynik {pnl:+.2f} USD (funding {fund:+.2f}, opłaty {fee:.2f})")

    def _flatten_leg(self, spot, perp, b, which, qs, contracts):
        if qs and qs >= spot["minSz"]:
            try:
                self.ex.place(spot["instId"], "sell", round_step(qs, spot["lot"]), "market", spot=True)
            except Exception as e:  # noqa: BLE001
                self.notify(f"🚨 Carry {b}: nie udało się zamknąć nogi spot: {e}")
        if contracts and contracts >= perp["minSz"]:
            try:
                self.ex.place(perp["instId"], "buy", round_step(contracts, perp["lot"]), "market", reduce_only=True)
            except Exception as e:  # noqa: BLE001
                self.notify(f"🚨 Carry {b}: nie udało się zamknąć nogi perp: {e}")

    def _hedge_check(self):
        for b, p in list(self.st["pos"].items()):
            if b in self.st["pending"]:
                continue
            pos, _ = self.ex.position(p["perp"]["instId"])
            spot_bal = self.ex.spot_avail(b)
            if abs(pos) < p["contracts"] * 0.98 or spot_bal < p["qs"] * 0.98:
                self._flatten_leg(p["spot"], p["perp"], b, "both", min(spot_bal, p["qs"]), min(abs(pos), p["contracts"]))
                self.st["pos"].pop(b)
                journal.append("carry", dict(event="hedge_broken", ts=_iso(self.now()), base=b,
                                             detail=f"perp {pos} / {p['contracts']}, spot {spot_bal} / {p['qs']}"))
                self.notify(f"🚨 Carry {b}: jedna noga zniknęła (perp {pos:g}/{p['contracts']:g}, spot {spot_bal:g}/{p['qs']:g}) "
                            "— zamknięto pozostałą nogę. Sprawdź konto demo.")


def _fee_coin(o, base):
    """opłata pobrana w coinie bazowym (kupno spot) — ujemna liczba coinów do odjęcia."""
    return o["fee"] if (o.get("fee_ccy") or "") == base else 0.0


def _fee_usd(o):
    """opłata w USD (dodatnia = koszt); w coinie → × średnia cena."""
    f = o.get("fee") or 0.0
    ccy = o.get("fee_ccy") or "USD"
    return f * o["avg"] if ccy not in ("USDT", "USDC", "USD") else f


class PublicMarket:
    """Dane publiczne OKX do reguły (jak w badaniu: USDT-SWAP i spot USDT, świece 1Dutc)."""

    def __init__(self, session):
        self.s = session

    def universe(self, today):
        ins = public_get(self.s, "/api/v5/public/instruments", {"instType": "SWAP"})
        spot = {d["instId"] for d in public_get(self.s, "/api/v5/public/instruments", {"instType": "SPOT"})}
        ok = {}
        for d in ins:
            i = d["instId"]
            if not i.endswith("-USDT-SWAP") or d.get("state") != "live" or str(d.get("instCategory", "1")) != "1":
                continue
            b = i.split("-")[0]
            if b in EXCL or f"{b}-USDT" not in spot or today - int(d["listTime"]) < CFG["min_age_d"] * DAY:
                continue
            ok[b] = i
        tk = public_get(self.s, "/api/v5/market/tickers", {"instType": "SWAP"})
        vol = sorted([(float(t.get("volCcy24h") or 0) * float(t.get("last") or 0), t["instId"].split("-")[0])
                      for t in tk if t["instId"] in ok.values()], reverse=True)[:60]
        rows = []
        for _, b in vol:
            c = public_get(self.s, "/api/v5/market/history-candles", {"instId": ok[b], "bar": "1Dutc", "limit": "31"})
            c = [x for x in c if x[8] == "1" and int(x[0]) < today]
            if len(c) < 20:
                continue
            qv30 = sum(float(x[7]) for x in c[:30]) / len(c[:30])
            sp = public_get(self.s, "/api/v5/market/history-candles", {"instId": f"{b}-USDT", "bar": "1Dutc", "limit": "2"})
            sp = [x for x in sp if x[8] == "1"]
            if not sp or abs(float(c[0][4]) / float(sp[0][4]) - 1) >= 0.05:
                continue
            rows.append((qv30, b, ok[b]))
            time.sleep(0.1)
        rows.sort(reverse=True)
        return [(b, i, k + 1) for k, (_, b, i) in enumerate(rows)]

    def funding(self, inst, since_ms):
        try:
            return funding_history(self.s, inst, since_ms)
        except Exception:  # noqa: BLE001
            return []
