"""
Automatyczne składanie zleceń na koncie DEMO OKX (Handel demo).

Każde zapytanie ma nagłówek x-simulated-trading: 1, więc prawdziwe konto
nie jest używane. Klucz API trzeba utworzyć w trybie demo.

Na każdą pozycję bot składa:
  1. zlecenie rynkowe (marża izolowana, dźwignia LEVERAGE),
  2. SL na całą pozycję (zlecenie warunkowe, tylko redukcja).
Po każdej zamkniętej świecy 4H swing_bot.py przesuwa SL (trailing 5,5 ATR) przez move_sl().
Gdy pozycja zniknie, anuluje pozostałe zlecenia i wysyła raport.
"""
import base64
import hashlib
import hmac
import json
import math
import os
import time
from datetime import datetime, timezone

DEMO_HEADER = {"x-simulated-trading": "1"}       # zawsze konto demo
MAX_SLIP_R = float(os.getenv("MAX_SLIP_R", "0.2"))  # maks. poślizg wyjścia z SL jako ułamek ryzyka (0,2 = 0,2R)


class OkxDemo:
    def __init__(self, session, host, key, secret, passphrase):
        self.s, self.host = session, host
        self.key, self.secret, self.pp = key, secret, passphrase

    def _req(self, method, path, params=None, body=None):
        if params:
            path += "?" + "&".join(f"{k}={v}" for k, v in params.items())
        data = json.dumps(body) if body is not None else ""
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + \
            f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z"
        sign = base64.b64encode(hmac.new(self.secret.encode(), (ts + method + path + data).encode(),
                                         hashlib.sha256).digest()).decode()
        h = {"OK-ACCESS-KEY": self.key, "OK-ACCESS-SIGN": sign, "OK-ACCESS-TIMESTAMP": ts,
             "OK-ACCESS-PASSPHRASE": self.pp, "Content-Type": "application/json", **DEMO_HEADER}
        r = self.s.request(method, self.host + path, headers=h, data=data or None, timeout=15)
        js = r.json()
        if js.get("code") != "0":
            detail = js.get("data") and js["data"][0].get("sMsg")
            raise RuntimeError(f"OKX {path.split('?')[0]} {js.get('code')}: {js.get('msg')} {detail or ''}".strip())
        return js["data"]

    def get(self, path, **params):
        return self._req("GET", path, params=params or None)

    def post(self, path, body):
        return self._req("POST", path, body=body)


def _round(x, step, mode="near"):
    n = x / step
    n = math.floor(n + 1e-9) if mode == "down" else round(n)
    dec = max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0
    return f"{n * step:.{dec}f}"


class Trader:
    def __init__(self, okx, state, send, risk_pct, leverage, max_pos):
        self.x, self.st, self.send = okx, state, send
        self.risk_pct, self.lev, self.max_pos = risk_pct, leverage, max_pos
        self.st.setdefault("_trades", {})
        cfg = self.x.get("/api/v5/account/config")[0]
        self.hedge = cfg.get("posMode") == "long_short_mode"

    # ---------- informacje o koncie ----------
    def equity(self):
        bal = self.x.get("/api/v5/account/balance", ccy="USDC")[0]
        for d in bal.get("details", []):
            if d["ccy"] == "USDC":
                return float(d.get("eq") or 0), float(d.get("availEq") or d.get("availBal") or 0)
        return float(bal.get("totalEq") or 0), 0.0

    def positions(self):
        return {p["instId"]: p for p in self.x.get("/api/v5/account/positions", instType="FUTURES")
                if float(p.get("pos") or 0) != 0}

    def instrument(self, base):
        data = self.x.get("/api/v5/public/instruments", instType="FUTURES")
        c = [d for d in data if d["instId"].startswith(f"{base}-USD_UM_XPERP") and d.get("state") == "live"]
        return c[0] if c else None

    def ticker(self, inst_id):
        return self.x.get("/api/v5/market/ticker", instId=inst_id)[0]

    def exit_slip(self, inst_id, long_, sz):
        """Średni poślizg (w cenie) zamknięcia sz kontraktów zleceniem rynkowym
        po aktualnym order booku demo. None, gdy książka jest za płytka."""
        book = self.x.get("/api/v5/market/books", instId=inst_id, sz="400")[0]
        levels = book["bids"] if long_ else book["asks"]      # long zamyka sprzedażą, short kupnem
        if not levels:
            return None
        best, left, cost = float(levels[0][0]), sz, 0.0
        for lv in levels:
            px, q = float(lv[0]), float(lv[1])
            take = min(left, q)
            cost += take * px
            left -= take
            if left <= 0:
                return abs(cost / sz - best)
        return None

    # ---------- otwieranie ----------
    def open_trade(self, base, s, spot_price):
        """Zwraca (True, opis) albo (False, powód pominięcia)."""
        trades = self.st["_trades"]
        if self.max_pos and len(trades) >= self.max_pos:
            return False, f"limit {self.max_pos} pozycji"
        ins = self.instrument(base)
        if not ins:
            return False, "brak kontraktu w demo"
        inst = ins["instId"]
        if inst in self.positions():
            return False, "na tym kontrakcie jest już pozycja (np. ręczna)"
        t = self.ticker(inst)
        bid, ask = float(t.get("bidPx") or 0), float(t.get("askPx") or 0)
        if not bid or not ask:
            return False, "brak ofert w order booku demo"
        mid = (bid + ask) / 2
        if abs(mid / spot_price - 1) > 0.006:
            return False, f"cena demo {mid} odbiega od rynku {spot_price}"
        long_ = s["dir"] == "long"
        px = ask if long_ else bid                          # przybliżona cena wejścia rynkowego
        if (px - s["entry"]) * (1 if long_ else -1) > 0.3 * s["atr"]:
            return False, "cena uciekła od wejścia o > 0,3 ATR"

        ct, lot, tick = float(ins["ctVal"]), float(ins["lotSz"]), float(ins["tickSz"])
        min_sz = float(ins.get("minSz") or lot)
        sgn = 1 if long_ else -1
        dist = abs(px - s["sl"])                            # ryzyko liczone od realnej ceny wejścia
        eq, avail = self.equity()
        risk = eq * self.risk_pct / 100
        sz = float(_round(risk / dist / ct, lot, "down"))
        if sz < min_sz:
            return False, "za mała ilość"
        if sz * ct * px / self.lev > avail * 0.9:
            return False, "za mało wolnego depozytu"

        # Order book demo jest płytki: rynkowy SL na dużą pozycję potrafi wypełnić się
        # daleko za stopem (26.10 SL ETH 2358 wykonał się po 2421). Zmniejszamy ilość,
        # aż szacowany poślizg wyjścia będzie <= MAX_SLIP_R ryzyka.
        cut = False
        while True:
            slip = self.exit_slip(inst, long_, sz)
            if slip is not None and slip <= MAX_SLIP_R * dist:
                break
            sz = float(_round(sz * 0.7, lot, "down"))
            cut = True
            if sz < min_sz:
                return False, "za płytki order book demo — wyjście z SL miałoby za duży poślizg"

        side, close_side = ("buy", "sell") if long_ else ("sell", "buy")
        pos_side = {"posSide": "long" if long_ else "short"} if self.hedge else {}

        self.x.post("/api/v5/account/set-leverage",
                    {"instId": inst, "lever": str(self.lev), "mgnMode": "isolated", **pos_side})
        o = self.x.post("/api/v5/trade/order", {"instId": inst, "tdMode": "isolated", "side": side,
                                                "ordType": "market", "sz": _round(sz, lot), **pos_side})[0]
        time.sleep(1.5)
        pos = self.positions().get(inst)
        if not pos:
            raise RuntimeError(f"zlecenie {o.get('ordId')} nie otworzyło pozycji")
        entry = float(pos["avgPx"])
        real_sz = abs(float(pos["pos"]))
        sl = entry - sgn * dist

        ids = {}
        try:
            ids["sl"] = self._algo(inst, close_side, pos_side,
                                   {"ordType": "conditional", "sz": _round(real_sz, lot),
                                    "slTriggerPx": _round(sl, tick), "slOrdPx": "-1"})
        except Exception as e:  # noqa: BLE001
            # bez SL nie zostawiamy pozycji — zamykamy ją od razu
            self.x.post("/api/v5/trade/close-position", {"instId": inst, "mgnMode": "isolated", **pos_side})
            raise RuntimeError(f"nie udało się ustawić SL, pozycję zamknięto: {e}") from e

        self.st["_trades"][inst] = {"base": base, "dir": s["dir"], "entry": entry, "sl": sl, "sz": real_sz,
                                    "ids": ids, "be": False, "strategy": "donchian", "tick": tick,
                                    "trail_bar": s["bar_ts"], "opened_ms": int(time.time() * 1000)}
        return True, (f"{'🟢 LONG' if long_ else '🔴 SHORT'} {base} — OTWARTO AUTOMATYCZNIE (demo)\n"
                      f"Strategia: wybicie kanału 20 świec 4H\nKontrakt: {inst}\nWejście: {entry}\n"
                      f"SL: {_round(sl, tick)} (trailing 5,5 ATR po każdej świecy 4H, bez TP)\n"
                      f"Ilość: {real_sz:g} kontraktów | ryzyko ~{round(real_sz * ct * dist)} USDC"
                      + ("\n(ilość zmniejszona — płytki order book demo)" if cut else ""))

    def _algo(self, inst, close_side, pos_side, body):
        b = {"instId": inst, "tdMode": "isolated", "side": close_side, **pos_side, **body}
        if not self.hedge:
            b["reduceOnly"] = "true"
        return self.x.post("/api/v5/trade/order-algo", b)[0]["algoId"]

    def move_sl(self, inst, t, new_sl):
        """Przesuwa SL (trailing). Gdy cena jest już za nowym poziomem, zamyka pozycję rynkowo."""
        long_ = t["dir"] == "long"
        tick = t.get("tick") or float(self.instrument(t["base"])["tickSz"])
        tk = self.ticker(inst)
        last = float(tk.get("last") or 0)
        pos_side = {"posSide": t["dir"]} if self.hedge else {}
        if last and ((last <= new_sl) if long_ else (last >= new_sl)):
            self.x.post("/api/v5/trade/close-position", {"instId": inst, "mgnMode": "isolated", **pos_side})
            return
        self.x.post("/api/v5/trade/amend-algos", {"instId": inst, "algoId": t["ids"]["sl"],
                                                  "newSlTriggerPx": _round(new_sl, tick)})
        t["sl"] = new_sl

    # ---------- zarządzanie ----------
    def manage(self):
        trades, live = self.st["_trades"], self.positions()
        pending = {a["algoId"] for a in
                   self.x.get("/api/v5/trade/orders-algo-pending", ordType="conditional", instType="FUTURES") +
                   self.x.get("/api/v5/trade/orders-algo-pending", ordType="move_order_stop", instType="FUTURES")}
        for inst, t in list(trades.items()):
            pos = live.get(inst)
            if not pos:                                         # pozycja zamknięta (SL / trailing)
                for k, aid in t["ids"].items():
                    if aid in pending:
                        try:
                            self.x.post("/api/v5/trade/cancel-algos", [{"algoId": aid, "instId": inst}])
                        except Exception:  # noqa: BLE001
                            pass
                pnl, val = "", None
                try:
                    h = self.x.get("/api/v5/account/positions-history", instType="FUTURES", instId=inst, limit="1")
                    if h:
                        val = float(h[0].get('realizedPnl') or h[0].get('pnl') or 0)
                        pnl = f"\nWynik: {val:+.2f} USDC"
                except Exception:  # noqa: BLE001
                    pass
                self.st.setdefault("_history", []).append(
                    {"base": t["base"], "dir": t["dir"], "pnl": val, "closed_ms": int(time.time() * 1000)})
                self.st["_history"] = self.st["_history"][-200:]
                self.send(f"🏁 {t['base']} {t['dir'].upper()} zamknięta (demo){pnl}")
                del trades[inst]
                continue
            tp1_done = t["ids"].get("tp1") and t["ids"]["tp1"] not in pending
            if tp1_done and not t["be"] and t["ids"].get("sl") in pending:
                try:
                    self.x.post("/api/v5/trade/amend-algos", {"instId": inst, "algoId": t["ids"]["sl"],
                                                              "newSlTriggerPx": str(t["entry"]),
                                                              "newSz": pos["pos"].lstrip("-")})
                    t["be"] = True
                    self.send(f"🎯 {t['base']}: TP1 zrealizowany, SL przesunięty na wejście {t['entry']}")
                except Exception as e:  # noqa: BLE001
                    self.send(f"⚠️ {t['base']}: TP1 zrealizowany, ale nie udało się przesunąć SL: {e}")

    # ---------- podsumowanie na żądanie ----------
    def summary(self):
        eq, avail = self.equity()
        live = self.positions()
        lines = ["📊 PODSUMOWANIE (konto demo)", f"Kapitał: {eq:,.2f} USDC | wolne: {avail:,.2f} USDC".replace(",", " "), ""]
        bot = self.st["_trades"]
        lines.append(f"Pozycje bota: {len(bot)}/{self.max_pos or 'bez limitu'}")
        total_u = 0.0
        for inst, t in bot.items():
            p = live.get(inst, {})
            u = float(p.get("upl") or 0)
            total_u += u
            stage = (f"SL {t['sl']:.6g} (trailing)" if t.get("strategy") == "donchian"
                     else "SL na wejściu" if t.get("be") else "czeka na TP1")
            lines.append(f"• {t['base']} {t['dir'].upper()} od {t['entry']} | teraz {p.get('markPx', '?')} | "
                         f"{u:+.2f} USDC | {stage}")
        manual = [p for i, p in live.items() if i not in bot]
        if manual:
            lines.append("")
            lines.append("Twoje pozycje ręczne:")
            for p in manual:
                u = float(p.get("upl") or 0)
                total_u += u
                side = "SHORT" if float(p["pos"]) < 0 else "LONG"
                lines.append(f"• {p['instId'].split('-')[0]} {side} od {p.get('avgPx')} | teraz {p.get('markPx')} | {u:+.2f} USDC")
        lines.append(f"Niezrealizowany wynik razem: {total_u:+.2f} USDC")
        hist = [h for h in self.st.get("_history", []) if h.get("pnl") is not None]
        day0 = int(time.time() // 86400 * 86400 * 1000)
        today = [h for h in hist if h["closed_ms"] >= day0]
        lines.append("")
        lines.append(f"Dziś zamknięte przez bota: {len(today)} | wynik {sum(h['pnl'] for h in today):+.2f} USDC")
        if hist:
            wins = sum(1 for h in hist if h["pnl"] > 0)
            lines.append(f"Od startu: {len(hist)} transakcji | trafione {wins}/{len(hist)} | "
                         f"wynik {sum(h['pnl'] for h in hist):+.2f} USDC")
            for h in hist[-3:]:
                lines.append(f"  · {h['base']} {h['dir'].upper()}: {h['pnl']:+.2f} USDC")
        else:
            lines.append("Od startu: brak zamkniętych transakcji bota")
        return "\n".join(lines)
