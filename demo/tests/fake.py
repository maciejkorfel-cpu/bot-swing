"""FakeExchange: symulacja konta demo na historycznych świecach 5m (do testów i dry-run).
Zasady jak w modelu badań: limit post-only wypełniony tylko gdy cena PRZEJDZIE poziom (low < px dla kupna, high > px dla
sprzedaży) w świecy PÓŹNIEJSZEJ niż złożenie; rynek po open bieżącej świecy 5m ± poślizg; OCO: SL (pierwszy, pesymistycznie)
gdy low ≤ SL → min(open, SL)·(1−poślizg), TP gdy high > TP → TP (maker). Prowizje taker 0,05% / maker 0,02%."""
import bisect

TK, MK, SLIP = 0.0005, 0.0002, 0.0003


class FakeExchange:
    def __init__(self, bars5, equity=10000.0, acct_lv="3", inst_map=None, spot=None, ct=None, fail=None):
        """bars5: {inst: [[ts,o,h,l,c], ...]} rosnąco. inst_map: base -> perp inst (domyślnie BASE-USDT-SWAP)."""
        self.b5 = bars5
        self.ts = {k: [b[0] for b in v] for k, v in bars5.items()}
        self.t = 0
        self.eq0 = equity
        self.acct_lv, self.hedge = acct_lv, False
        self.inst_map = inst_map or {}
        self.spot_map = spot or {}
        self.ct = ct or {}
        self.fail = fail or set()          # instrumenty, na których zlecenia są odrzucane (test nogi)
        self.orders, self.algos, self.pos, self.bal = {}, {}, {}, {}
        self.closed = {}
        self.n = 0
        self.realized = 0.0
        self.funding = {}

    # ------------------------------------------------------------ czas
    def _i(self, inst):
        """indeks bieżącej (niezamkniętej) świecy 5m w chwili t."""
        return bisect.bisect_right(self.ts[inst], self.t) - 1

    def advance(self, t_new):
        """przetwarza świece 5m z [t, t_new) — wypełnienia limitów i OCO."""
        for inst in self.b5:
            a = bisect.bisect_left(self.ts[inst], self.t)
            b = bisect.bisect_left(self.ts[inst], t_new)
            for j in range(a, b):
                ts, o, h, l, c = self.b5[inst][j]
                for oid, od in self.orders.items():
                    if od["inst"] != inst or od["state"] not in ("live", "partially_filled") or ts <= od["placed"]:
                        continue
                    if (od["side"] == "buy" and l < od["px"]) or (od["side"] == "sell" and h > od["px"]):
                        self._fill(od, od["sz"] - od["filled"], od["px"], MK)
                for aid, al in self.algos.items():
                    if al["inst"] != inst or al["state"] != "live" or ts <= al["placed"]:
                        continue
                    q = self.pos.get(inst, 0.0)
                    if q <= 0:
                        al["state"] = "canceled"; continue
                    if l <= al["sl"]:
                        self._close(inst, min(o, al["sl"]) * (1 - SLIP), TK, ts); al["state"] = "effective"
                    elif h > al["tp"]:
                        self._close(inst, o if o > al["tp"] else al["tp"], MK, ts); al["state"] = "effective"
        self.t = t_new

    def _fill(self, od, q, px, fee):
        od["cost"] += q * px; od["filled"] += q
        od["avg"] = od["cost"] / od["filled"]
        od["fee"] += q * px * fee * (self.ct.get(od["inst"], 1.0))
        od["state"] = "filled"
        inst = od["inst"]
        sgn = 1 if od["side"] == "buy" else -1
        if od.get("spot"):
            base = inst.split("-")[0]
            self.bal[base] = self.bal.get(base, 0.0) + sgn * q
        else:
            p0 = self.pos.get(inst, 0.0)
            if sgn > 0 and p0 >= 0 or sgn < 0 and p0 <= 0:
                ct = self.ct.get(inst, 1.0)
                tot = abs(p0) + q
                self.entry = getattr(self, "entry", {})
                self.entry[inst] = (self.entry.get(inst, px) * abs(p0) + px * q) / tot
                self.pos[inst] = p0 + sgn * q
                self.efee = getattr(self, "efee", {})
                self.efee[inst] = self.efee.get(inst, 0.0) + q * px * fee * self.ct.get(inst, 1.0)
            else:
                self._close(inst, px, TK if fee == TK else MK, self.t, qty=q)
                return
        self.realized -= q * px * fee * self.ct.get(inst, 1.0)

    def _close(self, inst, px, fee, ts, qty=None):
        q = self.pos.get(inst, 0.0)
        qq = abs(q) if qty is None else qty
        ct = self.ct.get(inst, 1.0)
        e = self.entry[inst]
        sgn = 1 if q > 0 else -1
        pnl = sgn * qq * ct * (px - e) - qq * ct * px * fee
        self.realized += pnl
        self.pos[inst] = q - sgn * qq
        r = self.closed.setdefault(inst, dict(pnl=0.0))
        ef = getattr(self, "efee", {}).pop(inst, 0.0) if abs(self.pos[inst]) < 1e-12 else 0.0
        r["pnl"] += pnl - ef
        r.update(px=px, ts=ts)

    # ------------------------------------------------------------ API jak Exchange
    def total_equity(self):
        return self.eq0 + self.realized

    def spot_avail(self, ccy):
        if ccy in ("USDT", "USDC"):
            return 1e12
        return self.bal.get(ccy, 0.0)

    def perp_inst(self, base):
        inst = self.inst_map.get(base, f"{base}-USDT-SWAP")
        if inst not in self.b5:
            return None
        return dict(instId=inst, instType="SWAP", ctVal=self.ct.get(inst, 1.0), lot=1e-6, minSz=1e-6, tick=1e-6, maxMkt=None)

    def spot_inst(self, base, quotes=("USDT",)):
        inst = self.spot_map.get(base)
        if not inst or inst not in self.b5:
            return None
        return dict(instId=inst, quote="USDT", lot=1e-8, minSz=1e-8, tick=1e-8)

    def book_top(self, inst):
        j = self._i(inst)
        c = self.b5[inst][max(j - 1, 0)][4]          # ostatnia zamknięta 5m
        return c, c, c

    def place(self, inst, side, sz, ord_type, px=None, td=None, reduce_only=False, spot=False, cl_id=None):
        if inst in self.fail:
            raise RuntimeError(f"OKX /trade/order 51000: odrzucone ({inst})")
        self.n += 1
        oid = str(self.n)
        od = dict(inst=inst, side=side, sz=sz, px=px, state="live", filled=0.0, avg=0.0, cost=0.0, fee=0.0,
                  placed=self.t - 1, spot=spot, type=ord_type)
        self.orders[oid] = od
        if ord_type == "post_only":
            bid, ask, _ = self.book_top(inst)
            if (side == "buy" and px > ask) or (side == "sell" and px < bid):
                od["state"] = "canceled"
            od["placed"] = self.b5[inst][self._i(inst)][0] - 1   # może wypełnić się już w bieżącej świecy 5m
        elif ord_type == "market":
            j = self._i(inst)
            o = self.b5[inst][j][1]
            fpx = o * (1 + SLIP) if side == "buy" else o * (1 - SLIP)
            self._fill(od, sz, fpx, TK)
        return oid

    def get_order(self, inst, oid):
        od = self.orders[oid]
        return dict(state=od["state"], filled=od["filled"], avg=od["avg"], fee=od["fee"], fee_ccy="USDT", sz=od["sz"])

    def cancel(self, inst, oid):
        od = self.orders[oid]
        if od["state"] == "live":
            od["state"] = "canceled"

    def place_oco(self, inst, sz, sl, tp, side="sell"):
        self.n += 1
        aid = "A" + str(self.n)
        self.algos[aid] = dict(inst=inst, sz=sz, sl=sl, tp=tp, state="live", placed=self.b5[inst][self._i(inst)][0] - 1)
        return aid

    def amend_sl(self, inst, aid, sl):
        self.algos[aid]["sl"] = sl

    def cancel_algo(self, inst, aid):
        if aid in self.algos and self.algos[aid]["state"] == "live":
            self.algos[aid]["state"] = "canceled"

    def algo_state(self, aid):
        a = self.algos.get(aid)
        return (a["state"], a) if a else (None, {})

    def position(self, inst):
        return self.pos.get(inst, 0.0), getattr(self, "entry", {}).get(inst, 0.0)

    def last_close(self, inst, since):
        r = self.closed.pop(inst, None)
        return (r["px"], r["pnl"]) if r else (None, None)

    def funding_received(self, inst, since):
        return self.funding.get(inst, 0.0)
