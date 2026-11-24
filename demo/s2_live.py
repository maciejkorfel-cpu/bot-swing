"""S2 na koncie demo: maszyna stanów wywoływana co ~5–15 min.

Stan st["s2"]:
  base, realized, peak     — księga S2 (kapitał startowy S2, zrealizowany PnL, szczyt equity)
  last_bar[inst]           — ostatnia przetworzona zamknięta świeca 4H (sygnałowa)
  pos[inst]                — pozycja: phase "pending" (limit czeka) / "open"
  mode                     — RUN / STOP (kryteria R12)
Sygnały i poziomy liczone na BTC-USDT-SWAP / ETH-USDT-SWAP (dane badań), handel na instrumencie demo (exchange.perp_inst),
poziomy przeliczane współczynnikiem k = cena instrumentu handlu / cena SWAP w chwili wejścia.
"""
import time
from . import s2, journal
from .okx import candles

INSTS = {"BTC-USDT-SWAP": "BTC", "ETH-USDT-SWAP": "ETH"}
ENTRY_TIMEOUT_MS = 3600_000          # limit ważny 1 h od otwarcia świecy wejścia, potem rynek


def _now():
    return int(time.time() * 1000)


def _iso(ms):
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(ms / 1000)) if ms else ""


class S2Live:
    def __init__(self, ex, session, st, notify, now=None, bars_fn=None, cur_open_fn=None, swap_last_fn=None):
        self.ex, self.s, self.notify = ex, session, notify
        self.now = now or _now
        self.bars_fn = bars_fn or (lambda inst: candles(session, inst, "4H", 1500))
        self.cur_open_fn = cur_open_fn or self._cur_open
        self.swap_last_fn = swap_last_fn or self._swap_last
        self.st = st.setdefault("s2", {})
        self.st.setdefault("pos", {}); self.st.setdefault("last_bar", {}); self.st.setdefault("mode", "RUN")
        self.st.setdefault("realized", 0.0)

    # ------------------------------------------------------------- dane
    def _cur_open(self, inst):
        from .okx import public_get
        d = public_get(self.s, "/api/v5/market/candles", {"instId": inst, "bar": "4H", "limit": "1"})
        return int(d[0][0]), float(d[0][1])

    def _swap_last(self, inst):
        from .okx import public_get
        return float(public_get(self.s, "/api/v5/market/ticker", {"instId": inst})[0]["last"])

    def equity(self, upl=0.0):
        return self.st["base"] + self.st["realized"] + upl

    # ------------------------------------------------------------- główny krok
    def step(self):
        if "base" not in self.st:
            self.st["base"] = float(self.ex.total_equity()) * float(__import__("os").getenv("S2_FRAC", "1.0"))
            self.st["peak"] = self.st["base"]
            self.st["start_ms"] = self.now()
        msgs = []
        for inst, base in INSTS.items():
            bars = self.bars_fn(inst)
            if len(bars) < 400:
                raise RuntimeError(f"za mało świec 4H {inst}: {len(bars)}")
            ind = s2.indicators(bars)
            p = self.st["pos"].get(inst)
            if p and p["phase"] == "pending":
                self._manage_pending(inst, p, ind)
            p = self.st["pos"].get(inst)
            if p and p["phase"] == "open":
                self._manage_open(inst, p, ind, bars)
            self._new_signal(inst, base, ind, bars)
        return msgs

    # ------------------------------------------------------------- nowy sygnał
    def _new_signal(self, inst, base, ind, bars):
        last_i = len(bars) - 1
        last_ts = bars[last_i][0]
        prev = self.st["last_bar"].get(inst)
        if prev is None:                      # pierwszy start: niczego wstecz nie otwieramy
            self.st["last_bar"][inst] = last_ts
            return
        if last_ts <= prev:
            return
        # świece pominięte (bot nie działał) — sygnały na nich zapisujemy jako pominięte
        for i in range(last_i - 1, -1, -1):
            if bars[i][0] <= prev:
                break
            if ind["sig"][i]:
                journal.append("signals", dict(strategy="S2", inst=inst, signal_bar=_iso(bars[i][0]), decided_ts=_iso(self.now()),
                                               action="skipped", detail="bot nie działał przy zamknięciu świecy"))
        self.st["last_bar"][inst] = last_ts
        sg = s2.signal_at(ind, last_i)
        if not sg:
            return
        delay = (self.now() - (last_ts + s2.BAR_MS)) / 60000
        row = dict(strategy="S2", inst=inst, signal_bar=_iso(last_ts), decided_ts=_iso(self.now()), delay_min=round(delay, 1))
        if self.st["mode"] != "RUN":
            journal.append("signals", dict(row, action="stop_mode", detail="tryb STOP — bez nowych wejść"))
            return
        if inst in self.st["pos"]:
            journal.append("signals", dict(row, action="in_position", detail="pozycja trwa (jak w backteście)"))
            return
        try:
            info = self._enter(inst, base, sg, last_ts)
            journal.append("signals", dict(row, action="entered", detail=info))
        except Exception as e:  # noqa: BLE001
            journal.append("signals", dict(row, action="error", detail=str(e)[:200]))
            self.notify(f"⚠️ S2 {base}: nie udało się złożyć wejścia: {str(e)[:300]}")

    def _enter(self, inst, base, sg, sig_ts):
        ti = self.ex.perp_inst(base)
        if not ti:
            raise RuntimeError("brak instrumentu demo")
        bar_ts, open_swap = self.cur_open_fn(inst)
        if bar_ts != sig_ts + s2.BAR_MS:
            open_swap = sg["close"]           # świeca wejścia jeszcze nie opublikowana → open ≈ close sygnału
        bid, ask, last = self.ex.book_top(ti["instId"])
        k = ((bid + ask) / 2) / self.swap_last_fn(inst)
        if not (0.97 < k < 1.03):
            raise RuntimeError(f"cena instrumentu demo odbiega od SWAP (k={k:.4f})")
        sl_swap, tp_swap = open_swap - sg["sl_d"], open_swap + sg["tp_d"]
        q_coin = s2.size(self.equity(), open_swap, sg["sl_d"])
        sz = s2.round_step(q_coin / ti["ctVal"], ti["lot"])
        if ti["maxMkt"]:
            sz = min(sz, s2.round_step(ti["maxMkt"], ti["lot"]))
        if sz < ti["minSz"]:
            raise RuntimeError("za mała ilość")
        px = s2.round_step(min(open_swap * k, bid), ti["tick"])
        oid = self.ex.place(ti["instId"], "buy", sz, "post_only", px=px)
        self.st["pos"][inst] = dict(phase="pending", base=base, trade_inst=ti["instId"], ct=ti["ctVal"], lot=ti["lot"],
                                    tick=ti["tick"], minSz=ti["minSz"], sz=sz, oid=oid, limit_px=px, k=k,
                                    sig_ts=sig_ts, entry_bar=sig_ts + s2.BAR_MS, open_swap=open_swap, sl_swap=sl_swap,
                                    tp_swap=tp_swap, atr=sg["atr"], risk_usd=sz * ti["ctVal"] * sg["sl_d"] * k,
                                    deadline=sig_ts + s2.BAR_MS + ENTRY_TIMEOUT_MS, placed_ms=self.now(),
                                    delay_min=round((self.now() - sig_ts - s2.BAR_MS) / 60000, 1), fills=[])
        return f"limit {px} × {sz} ({ti['instId']})"

    # ------------------------------------------------------------- oczekujący limit
    def _manage_pending(self, inst, p, ind):
        o = self.ex.get_order(p["trade_inst"], p["oid"])
        filled = o["filled"]
        if o["state"] == "filled" or (filled >= p["sz"] - 1e-12):
            p["fills"].append(dict(sz=filled, px=o["avg"], kind="maker"))
            return self._activate(inst, p)
        if self.now() < p["deadline"] and o["state"] in ("live", "partially_filled"):
            return
        if o["state"] == "canceled" and self.now() < p["deadline"]:
            # post_only odrzucony (cena przeszła przez poziom) — złóż ponownie po bieżącym bid
            bid, ask, _ = self.ex.book_top(p["trade_inst"])
            px = s2.round_step(min(p["limit_px"], bid), p["tick"])
            p["oid"] = self.ex.place(p["trade_inst"], "buy", s2.round_step(p["sz"] - filled, p["lot"]), "post_only", px=px)
            if filled > 0:
                p["fills"].append(dict(sz=filled, px=o["avg"], kind="maker")); p["sz"] -= filled
            return
        # termin minął: anuluj resztę, rynek albo pominięcie (gdy cena osiągnęła już TP)
        self.ex.cancel(p["trade_inst"], p["oid"])
        o = self.ex.get_order(p["trade_inst"], p["oid"])
        if o["filled"] > 0:
            p["fills"].append(dict(sz=o["filled"], px=o["avg"], kind="maker"))
        rest = s2.round_step(p["sz"] - o["filled"], p["lot"])
        swap_now = self.swap_last_fn(inst)
        if swap_now >= p["tp_swap"]:
            if not p["fills"]:
                journal.append("signals", dict(strategy="S2", inst=inst, signal_bar=_iso(p["sig_ts"]), decided_ts=_iso(self.now()),
                                               action="skipped", detail="limit niewypełniony, cena już przy TP — pominięto"))
                self.notify(f"⏭ S2 {p['base']}: limit niewypełniony w 1 h, cena osiągnęła TP — transakcja pominięta (jak w modelu)")
                del self.st["pos"][inst]
                return
            rest = 0.0
        if rest >= p["minSz"]:
            ref = swap_now * p["k"]
            oid = self.ex.place(p["trade_inst"], "buy", rest, "market")
            om = self.ex.get_order(p["trade_inst"], oid)
            p["fills"].append(dict(sz=om["filled"], px=om["avg"], kind="taker", ref=ref))
        if not p["fills"] or sum(f["sz"] for f in p["fills"]) <= 0:
            del self.st["pos"][inst]
            self.notify(f"⚠️ S2 {p['base']}: wejście nie wypełnione")
            return
        self._activate(inst, p)

    def _activate(self, inst, p):
        sz = sum(f["sz"] for f in p["fills"])
        avg = sum(f["sz"] * f["px"] for f in p["fills"]) / sz
        k = p["k"]
        sl_x, tp_x = s2.round_step(p["sl_swap"] * k, p["tick"], "near"), s2.round_step(p["tp_swap"] * k, p["tick"], "near")
        try:
            aid = self.ex.place_oco(p["trade_inst"], sz, sl_x, tp_x)
        except Exception as e:  # noqa: BLE001
            # bez SL nie zostawiamy pozycji
            self.ex.place(p["trade_inst"], "sell", sz, "market", reduce_only=True)
            del self.st["pos"][inst]
            raise RuntimeError(f"nie udało się ustawić SL/TP, pozycję zamknięto: {e}")
        kinds = {f["kind"] for f in p["fills"]}
        taker = [f for f in p["fills"] if f["kind"] == "taker"]
        slip = (sum(f["sz"] * (f["px"] / f["ref"] - 1) for f in taker) / sum(f["sz"] for f in taker) * 100) if taker else 0.0
        p.update(phase="open", sz=sz, entry=avg, algo=aid, sl_x=sl_x, tp_x=tp_x, stop_swap=p["sl_swap"], peak_swap=p["open_swap"],
                 trail_bar=p["sig_ts"], opened_ms=self.now(), entry_type="maker" if kinds == {"maker"} else
                 ("taker" if kinds == {"taker"} else "mixed"), slip_entry_pct=round(slip, 4),
                 risk_usd=sz * p["ct"] * (avg - sl_x))
        self.notify(f"🟢 S2 LONG {p['base']} otwarta (demo)\nInstrument: {p['trade_inst']}\nWejście: {avg:.6g} ({p['entry_type']})\n"
                    f"SL: {sl_x} (3 ATR) | TP: {tp_x} (4 ATR) | trailing 4 ATR po każdej świecy 4H\n"
                    f"Ilość: {sz:g} | ryzyko ≈ {p['risk_usd']:.0f} USD (0,5% kapitału S2)")

    # ------------------------------------------------------------- otwarta pozycja
    def _manage_open(self, inst, p, ind, bars):
        pos, _ = self.ex.position(p["trade_inst"])
        if pos <= 1e-12:
            return self._closed(inst, p)
        # trailing: po każdej nowej zamkniętej świecy od świecy wejścia (włącznie)
        changed = False
        for i in range(len(bars)):
            ts = bars[i][0]
            if ts < p["entry_bar"] or ts <= p["trail_bar"]:
                continue
            p["peak_swap"] = max(p["peak_swap"], bars[i][4])
            ns = s2.trail_stop(p["peak_swap"], ind["atr"][i], p["stop_swap"])
            if ns > p["stop_swap"]:
                p["stop_swap"] = ns; changed = True
            p["trail_bar"] = ts
        if changed:
            new_sl = s2.round_step(p["stop_swap"] * p["k"], p["tick"], "near")
            if new_sl > p["sl_x"]:
                bid, _, _ = self.ex.book_top(p["trade_inst"])
                if bid and bid <= new_sl:
                    self.ex.place(p["trade_inst"], "sell", pos, "market", reduce_only=True)
                    self.ex.cancel_algo(p["trade_inst"], p["algo"])
                    p["exit_hint"] = "trailing (cena już pod nowym SL)"
                else:
                    self.ex.amend_sl(p["trade_inst"], p["algo"], new_sl)
                    p["sl_x"] = new_sl
        state, _ = self.ex.algo_state(p["algo"])
        if state in ("canceled", None):
            pos, _ = self.ex.position(p["trade_inst"])
            if pos > 1e-12:
                p["algo"] = self.ex.place_oco(p["trade_inst"], pos, p["sl_x"], p["tp_x"])
                self.notify(f"⚠️ S2 {p['base']}: zlecenie SL/TP zniknęło z giełdy — wystawiono ponownie")

    def _closed(self, inst, p):
        px, pnl = self.ex.last_close(p["trade_inst"], p["opened_ms"])
        state, a = self.ex.algo_state(p["algo"])
        self.ex.cancel_algo(p["trade_inst"], p["algo"])
        if pnl is None:
            px = px or 0.0
            pnl = (px - p["entry"]) * p["sz"] * p["ct"] if px else 0.0
        why = p.get("exit_hint") or ("TP" if px and px >= p["tp_x"] * 0.999 else "SL/trailing")
        R = pnl / p["risk_usd"] if p["risk_usd"] else 0.0
        slip_exit = ((p["sl_x"] - px) / p["sl_x"] * 100) if (px and why != "TP" and px < p["sl_x"]) else 0.0
        self.st["realized"] += pnl
        row = dict(strategy="S2", inst=inst, trade_inst=p["trade_inst"], side="long", signal_bar=_iso(p["sig_ts"]),
                   entry_ts=_iso(p["opened_ms"]), exit_ts=_iso(self.now()), entry_px=round(p["entry"], 8), exit_px=px,
                   qty=p["sz"], pnl=round(pnl, 4), R=round(R, 4), risk_usd=round(p["risk_usd"], 2), why=why,
                   entry_type=p["entry_type"], delay_min=p["delay_min"], slip_entry_pct=p["slip_entry_pct"],
                   slip_exit_pct=round(slip_exit, 4))
        journal.append("trades", row)
        del self.st["pos"][inst]
        self.notify(f"🏁 S2 {p['base']} zamknięta (demo): {why}\nWejście {p['entry']:.6g} → wyjście {px:.6g}\n"
                    f"Wynik: {pnl:+.2f} USD ({R:+.2f} R) | kapitał S2: {self.equity():.0f}")
        return row

    # ------------------------------------------------------------- miary
    def mtm_equity(self):
        upl = 0.0
        for inst, p in self.st["pos"].items():
            if p["phase"] == "open":
                bid, _, _ = self.ex.book_top(p["trade_inst"])
                upl += (bid - p["entry"]) * p["sz"] * p["ct"]
        eq = self.equity(upl)
        self.st["peak"] = max(self.st.get("peak", eq), eq)
        return eq, 1 - eq / self.st["peak"] if self.st["peak"] else 0.0
