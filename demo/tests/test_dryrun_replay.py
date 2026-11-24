"""TEST NA SUCHO: logika bota (S2Live) na historii 5m przez FakeExchange vs silnik backtestu (demo/s2.simulate = research)
z tym samym wariantem wejścia (decyzja 5 min po zamknięciu 4H, limit = min(open, ostatnia cena), ważny do 60 min, potem
rynek; pominięcie, gdy cena ≥ TP). Wymaga research/data (lokalnie). Okres: COMPARE 2024-07-01 – 2026-11-22.
Uruchom: python -m unittest demo.tests.test_dryrun_replay -v   (kilka minut)"""
import bisect, os, sys, tempfile, unittest
import numpy as np
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
DATA = os.path.join(ROOT, "research", "data", "candles")
PERIOD = (os.getenv("REPLAY_START", "2024-07-01"), os.getenv("REPLAY_END", "2026-11-22"))
M5 = 300_000


def ms(d):
    return int(np.datetime64(d).astype("datetime64[ms]").astype(np.int64))


@unittest.skipUnless(os.path.isdir(DATA) and os.listdir(DATA), "brak research/data")
class TestReplay(unittest.TestCase):
    def test_replay_matches_backtest(self):
        os.environ["DEMO_LOG_DIR"] = tempfile.mkdtemp()
        import importlib
        from demo import journal, s2
        importlib.reload(journal)
        from demo import s2_live
        importlib.reload(s2_live)
        from demo.tests.fake import FakeExchange
        sys.path.insert(0, os.path.join(ROOT, "research"))
        import engine as E
        insts = list(s2_live.INSTS)
        to_list = lambda m: [[int(t.astype("datetime64[ms]").astype(np.int64)), o, h, l, c]
                             for t, o, h, l, c in zip(m["idx"], m["o"], m["h"], m["l"], m["c"])]
        b4 = {i: to_list(E.market(i, "4h")) for i in insts}
        b5 = {i: to_list(E.market(i, "5m")) for i in insts}
        t5 = {i: [b[0] for b in b5[i]] for i in insts}
        t4 = {i: [b[0] for b in b4[i]] for i in insts}
        a, z = ms(PERIOD[0]), ms(PERIOD[1])
        ex = FakeExchange(b5, equity=20000.0)
        st = {"s2": {"base": 20000.0, "peak": 20000.0, "realized": 0.0, "start_ms": a,
                     "last_bar": {i: a - s2.BAR_MS for i in insts}}}
        clock = {"t": a}
        bars_fn = lambda i: b4[i][max(0, bisect.bisect_right(t4[i], clock["t"] - s2.BAR_MS) - 1500):
                                  bisect.bisect_right(t4[i], clock["t"] - s2.BAR_MS)]

        def cur_open(i):
            k = bisect.bisect_right(t4[i], clock["t"]) - 1
            return b4[i][k][0], b4[i][k][1]

        def swap_last(i):
            k = bisect.bisect_right(t5[i], clock["t"]) - 2
            return b5[i][k][4]
        bot = s2_live.S2Live(ex, None, st, lambda m: None, now=lambda: clock["t"], bars_fn=bars_fn,
                             cur_open_fn=cur_open, swap_last_fn=swap_last)
        t = (a // s2.BAR_MS + 1) * s2.BAR_MS + M5
        while t < z:
            ex.advance(t); clock["t"] = t
            bot.step()
            pend = any(p["phase"] == "pending" for p in st["s2"]["pos"].values())
            nxt_bar = (t // s2.BAR_MS + 1) * s2.BAR_MS + M5
            t = t + M5 if pend else nxt_bar
        live = journal.read("trades")

        # backtest z tym samym wariantem wejścia
        res = {}
        for inst in insts:
            def fill(i, o, inst=inst):
                t0 = b4[inst][i][0]
                k0 = bisect.bisect_left(t5[inst], t0)
                lim = min(o, b5[inst][k0][4])
                atr = s2.indicators(b4[inst][max(0, i - 1500):i])["atr"][-1]
                tp = o + s2.TP_ATR * atr
                for k in range(k0 + 1, k0 + 12):
                    if b5[inst][k][3] < lim:
                        return lim, "maker"
                if b5[inst][k0 + 11][4] >= tp:
                    return None, None
                return b5[inst][k0 + 12][1] * (1 + s2.COSTS["slip"]), "taker"
            bt, _ = s2.simulate(b4[inst], start_ts=a, end_ts=z, entry_fill=fill)
            res[inst] = [x for x in bt if x["why"] != "END"]
        for inst in insts:
            L = {r["signal_bar"]: float(r["R"]) for r in live if r["inst"] == inst}
            B = {s2_live._iso(x["entry_ts"] - s2.BAR_MS): x["R"] for x in res[inst]}
            common = set(L) & set(B)
            dR = [L[k] - B[k] for k in common]
            print(f"\n{inst}: demo {len(L)} | backtest {len(B)} | wspólne {len(common)} | suma R demo {sum(L.values()):+.2f} "
                  f"vs backtest {sum(B.values()):+.2f} | śr. |ΔR| {np.mean(np.abs(dR)):.3f} | "
                  f"tylko demo {sorted(set(L) - set(B))[:5]} | tylko backtest {sorted(set(B) - set(L))[:5]}")
            self.assertGreaterEqual(len(common), 0.9 * max(len(L), len(B)))
            self.assertLess(float(np.median(np.abs(dR))), 0.05)
            self.assertLess(abs(sum(L.values()) - sum(B.values())), 0.15 * sum(abs(v) for v in B.values()))


if __name__ == "__main__":
    unittest.main()
