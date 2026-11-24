"""S2 w demo/s2.py = research/engine.py co do transakcji i USDT (TUNE, COMPARE, FINAL; BTC i ETH).
Wymaga danych research/data (lokalnie); w CI bez danych test jest pomijany."""
import os, sys, unittest
import numpy as np
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
from demo import s2  # noqa: E402

DATA = os.path.join(ROOT, "research", "data", "candles")


@unittest.skipUnless(os.path.isdir(DATA) and os.listdir(DATA), "brak research/data")
class TestS2VsEngine(unittest.TestCase):
    def test_identical(self):
        sys.path.insert(0, os.path.join(ROOT, "research"))
        import engine as E, lib as L
        from finalists import FINALISTS
        F = FINALISTS["S2"]
        for inst in F["insts"]:
            m = E.market(inst, "4h")
            spec = L.FAMILIES["breakout"](m, 1, **F["params"])
            sig = L.apply_filters(m, inst, "4h", 1, spec, F["filters"])
            bars = [[int(t.astype("datetime64[ms]").astype(np.int64)), o, h, l, c]
                    for t, o, h, l, c in zip(m["idx"], m["o"], m["h"], m["l"], m["c"])]
            ind = s2.indicators(bars)
            self.assertEqual(int(np.sum(sig[60:])), int(np.sum(ind["sig"][60:])), "liczba sygnałów")
            for per in ["TUNE", "COMPARE", "FINAL"]:
                tr, met = E.simulate(m, sig, 1, sl_d=spec["sl_d"], tp_d=spec["tp_d"], tr_d=spec["tr_d"], period=per)
                a, b = E.PERIODS[per]
                ms = lambda d: int(np.datetime64(d).astype("datetime64[ms]").astype(np.int64))
                mine, eq = s2.simulate(bars, list(m["fund"]), ms(a), ms(b))
                self.assertEqual(len(mine), len(tr), f"{inst} {per} liczba transakcji")
                self.assertEqual([t["ei"] for t in mine], list(tr.ei), f"{inst} {per} wejścia")
                self.assertEqual([t["xi"] for t in mine], list(tr.xi), f"{inst} {per} wyjścia")
                np.testing.assert_allclose([t["pnl"] for t in mine], tr.pnl.values, rtol=1e-9, atol=1e-6)
                self.assertAlmostEqual(eq - 10000, met["net"], delta=0.1)
                print(inst, per, len(mine), round(eq - 10000, 1), met["net"])


if __name__ == "__main__":
    unittest.main()
