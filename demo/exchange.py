"""Adapter giełdy dla strategii (konto DEMO OKX). Strategie używają tylko tych metod — w testach podmieniane na FakeExchange.

Instrument perp do handlu (zmienna OKX_PERP_VENUE):
  xperp (domyślnie) — kontrakty X-Perp konta UE (np. BTC-USD_UM_XPERP-310328, rozliczane w USD); to na nich działał
                       poprzedni bot na tym koncie;
  swap              — BTC-USDT-SWAP itd. (jeśli konto ma do nich dostęp).
Sygnały zawsze z danych BTC-USDT-SWAP / ETH-USDT-SWAP (jak w badaniach), poziomy przeliczane na instrument handlu.
"""
import os, time

VENUE = os.getenv("OKX_PERP_VENUE", "xperp")


class Exchange:
    def __init__(self, okx, td_mode="cross"):
        self.x, self.td = okx, td_mode
        self._inst = {}
        cfg = self.x.verify_demo_key()           # twarda blokada demo (okx.py)
        self.acct_lv = str(cfg.get("acctLv"))
        self.hedge = cfg.get("posMode") == "long_short_mode"

    # ------------------------------------------------------------- konto
    def total_equity(self):
        b = self.x.get("/api/v5/account/balance")[0]
        return float(b.get("totalEq") or 0)

    def spot_avail(self, ccy):
        b = self.x.get("/api/v5/account/balance", ccy=ccy)[0]
        for d in b.get("details", []):
            if d["ccy"] == ccy:
                return float(d.get("availBal") or d.get("cashBal") or 0)
        return 0.0

    # ------------------------------------------------------------- instrumenty
    def _instruments(self, t):
        if t not in self._inst:
            self._inst[t] = self.x.get("/api/v5/public/instruments", instType=t)
        return self._inst[t]

    def perp_inst(self, base):
        if VENUE == "swap":
            c = [d for d in self._instruments("SWAP") if d["instId"] == f"{base}-USDT-SWAP" and d.get("state") == "live"]
        else:
            c = [d for d in self._instruments("FUTURES") if d["instId"].startswith(f"{base}-USD_UM_XPERP")
                 and d.get("state") == "live"]
        if not c:
            return None
        d = sorted(c, key=lambda z: z["instId"])[-1]
        return dict(instId=d["instId"], instType=d["instType"], ctVal=float(d["ctVal"]), lot=float(d["lotSz"]),
                    minSz=float(d.get("minSz") or d["lotSz"]), tick=float(d["tickSz"]),
                    maxMkt=float(d.get("maxMktSz") or 0) or None)

    def spot_inst(self, base, quotes=("USDT", "USDC")):
        for q in quotes:
            c = [d for d in self._instruments("SPOT") if d["instId"] == f"{base}-{q}" and d.get("state") == "live"]
            if c:
                d = c[0]
                return dict(instId=d["instId"], quote=q, lot=float(d["lotSz"]), minSz=float(d.get("minSz") or d["lotSz"]),
                            tick=float(d["tickSz"]))
        return None

    # ------------------------------------------------------------- ceny
    def book_top(self, inst):
        t = self.x.get("/api/v5/market/ticker", instId=inst)[0]
        return float(t.get("bidPx") or 0), float(t.get("askPx") or 0), float(t.get("last") or 0)

    # ------------------------------------------------------------- zlecenia
    def place(self, inst, side, sz, ord_type, px=None, td=None, reduce_only=False, spot=False, cl_id=None):
        b = {"instId": inst, "tdMode": "cash" if spot else (td or self.td), "side": side,
             "ordType": ord_type, "sz": f"{sz:.10f}".rstrip("0").rstrip(".")}
        if spot:                                   # spot zawsze "cash": bez pożyczania (konto ujednolicone liczy go jako zabezpieczenie)
            if ord_type == "market":
                b["tgtCcy"] = "base_ccy"
        if px is not None:
            b["px"] = f"{px:.10f}".rstrip("0").rstrip(".")
        if reduce_only and not spot:
            b["reduceOnly"] = "true"
        if self.hedge and not spot:
            b["posSide"] = "long" if (side == "buy") != reduce_only else "short"
        if cl_id:
            b["clOrdId"] = cl_id
        return self.x.post("/api/v5/trade/order", b)[0]["ordId"]

    def get_order(self, inst, oid):
        d = self.x.get("/api/v5/trade/order", instId=inst, ordId=oid)[0]
        return dict(state=d["state"], filled=float(d.get("accFillSz") or 0), avg=float(d.get("avgPx") or 0),
                    fee=-float(d.get("fee") or 0), fee_ccy=d.get("feeCcy"), sz=float(d.get("sz") or 0))

    def cancel(self, inst, oid):
        try:
            self.x.post("/api/v5/trade/cancel-order", {"instId": inst, "ordId": oid})
        except Exception:  # noqa: BLE001 — już wypełnione/anulowane
            pass

    def place_oco(self, inst, sz, sl, tp, side="sell"):
        b = {"instId": inst, "tdMode": self.td, "side": side, "ordType": "oco", "sz": f"{sz:.10f}".rstrip("0").rstrip("."),
             "slTriggerPx": f"{sl:.10f}".rstrip("0").rstrip("."), "slOrdPx": "-1", "slTriggerPxType": "mark",
             "tpTriggerPx": f"{tp:.10f}".rstrip("0").rstrip("."), "tpOrdPx": f"{tp:.10f}".rstrip("0").rstrip("."),
             "tpTriggerPxType": "last"}
        if self.hedge:
            b["posSide"] = "long"
        else:
            b["reduceOnly"] = "true"
        return self.x.post("/api/v5/trade/order-algo", b)[0]["algoId"]

    def amend_sl(self, inst, algo_id, sl):
        self.x.post("/api/v5/trade/amend-algos", {"instId": inst, "algoId": algo_id,
                                                  "newSlTriggerPx": f"{sl:.10f}".rstrip("0").rstrip(".")})

    def cancel_algo(self, inst, algo_id):
        try:
            self.x.post("/api/v5/trade/cancel-algos", [{"algoId": algo_id, "instId": inst}])
        except Exception:  # noqa: BLE001
            pass

    def algo_state(self, algo_id):
        for path in ("/api/v5/trade/orders-algo-pending", "/api/v5/trade/orders-algo-history"):
            try:
                d = self.x.get(path, algoId=algo_id, ordType="oco") if "pending" in path else \
                    self.x.get(path, algoId=algo_id, ordType="oco")
            except Exception:  # noqa: BLE001
                continue
            if d:
                return d[0].get("state"), d[0]
        return None, {}

    def position(self, inst):
        d = self.x.get("/api/v5/account/positions", instId=inst)
        tot, avg = 0.0, 0.0
        for p in d:
            q = float(p.get("pos") or 0)
            if q:
                tot += q; avg = float(p.get("avgPx") or 0)
        return tot, avg

    def last_close(self, inst, since_ms):
        """wynik ostatniego zamknięcia pozycji (positions-history) po since_ms: (cena wyjścia, realizedPnl)."""
        d = self.x.get("/api/v5/account/positions-history", instId=inst, limit="5")
        for p in d:
            if int(p.get("uTime") or 0) >= since_ms:
                return float(p.get("closeAvgPx") or 0), float(p.get("realizedPnl") or p.get("pnl") or 0)
        return None, None

    def funding_received(self, inst, since_ms):
        """suma fundingu (bills typ 8) od since_ms; dodatnia = otrzymany."""
        tot, after = 0.0, None
        for _ in range(10):
            p = dict(type="8", instId=inst, limit="100")
            if after:
                p["after"] = after
            d = self.x.get("/api/v5/account/bills", **p)
            if not d:
                break
            for b in d:
                if int(b["ts"]) >= since_ms:
                    tot += float(b.get("pnl") or b.get("balChg") or 0)
            if int(d[-1]["ts"]) < since_ms:
                break
            after = d[-1]["billId"]
            time.sleep(0.1)
        return tot
