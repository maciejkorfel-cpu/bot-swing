"""Testy jednostkowe bota demo (bez sieci): blokada demo, kryteria R12, reguła carry, obsługa nóg, OCO."""
import os, sys, tempfile, unittest
from unittest import mock
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
os.environ["DEMO_LOG_DIR"] = tempfile.mkdtemp()
from demo import okx, journal, carry_live, s2  # noqa: E402
from demo.tests.fake import FakeExchange  # noqa: E402

DAY = 86400_000


class Resp:
    def __init__(self, js, code=200):
        self._js, self.status_code = js, code

    def json(self):
        return self._js


class Sess:
    """rejestruje zapytania; odpowiedź zależy od nagłówka demo."""
    def __init__(self, live_ok=False):
        self.calls, self.live_ok = [], live_ok

    def get(self, url, headers=None, timeout=None):
        self.calls.append(("GET", url, dict(headers or {})))
        demo = (headers or {}).get("x-simulated-trading") == "1"
        if not demo:
            return Resp({"code": "0" if self.live_ok else "50101", "msg": "", "data": [{}]})
        return Resp({"code": "0", "data": [{"acctLv": "3", "posMode": "net_mode"}]})

    def request(self, method, url, headers=None, data=None, timeout=None):
        self.calls.append((method, url, dict(headers or {})))
        return Resp({"code": "0", "data": [{"acctLv": "3", "posMode": "net_mode", "ordId": "1"}]})


class TestDemoGuard(unittest.TestCase):
    def test_requires_env(self):
        with mock.patch.dict(os.environ, {"OKX_DEMO": ""}):
            with self.assertRaises(okx.DemoGuardError):
                okx.OkxDemo(Sess(), "https://x", "k", "s", "p")

    def test_requires_keys(self):
        with mock.patch.dict(os.environ, {"OKX_DEMO": "1"}):
            with self.assertRaises(okx.DemoGuardError):
                okx.OkxDemo(Sess(), "https://x", "", "", "")

    def test_live_key_rejected_and_no_orders(self):
        with mock.patch.dict(os.environ, {"OKX_DEMO": "1"}):
            s = Sess(live_ok=True)
            c = okx.OkxDemo(s, "https://x", "k", "s", "p")
            with self.assertRaises(okx.DemoGuardError):
                c.verify_demo_key()
            with self.assertRaises(okx.DemoGuardError):
                c.post("/api/v5/trade/order", {"instId": "BTC-USDT-SWAP"})
            self.assertFalse(any("/trade/" in u for _, u, _ in s.calls))

    def test_order_before_verify_blocked(self):
        with mock.patch.dict(os.environ, {"OKX_DEMO": "1"}):
            s = Sess()
            c = okx.OkxDemo(s, "https://x", "k", "s", "p")
            with self.assertRaises(okx.DemoGuardError):
                c.post("/api/v5/trade/order", {})
            c.verify_demo_key()
            c.post("/api/v5/trade/order", {})
            sent = [h for m, u, h in s.calls if "/trade/order" in u]
            self.assertTrue(sent and all(h.get("x-simulated-trading") == "1" for h in sent))

    def test_env_switched_off_midway(self):
        with mock.patch.dict(os.environ, {"OKX_DEMO": "1"}):
            c = okx.OkxDemo(Sess(), "https://x", "k", "s", "p")
            c.verify_demo_key()
        with mock.patch.dict(os.environ, {"OKX_DEMO": "0"}):
            with self.assertRaises(okx.DemoGuardError):
                c.post("/api/v5/trade/order", {})

    def test_all_private_requests_have_demo_header(self):
        with mock.patch.dict(os.environ, {"OKX_DEMO": "1"}):
            s = Sess()
            c = okx.OkxDemo(s, "https://x", "k", "s", "p")
            c.verify_demo_key(); c.get("/api/v5/account/balance"); c.post("/api/v5/trade/order", {})
            priv = [h for m, u, h in s.calls if "OK-ACCESS-KEY" in h]
            no_hdr = [h for h in priv if h.get("x-simulated-trading") != "1"]
            self.assertEqual(len(no_hdr), 1)            # tylko test klucza (GET /account/config), które ma się NIE udać
            self.assertTrue(all("/account/config" in u for m, u, h in s.calls if "OK-ACCESS-KEY" in h and
                                h.get("x-simulated-trading") != "1"))


class TestKill(unittest.TestCase):
    def tr(self, rs):
        return [{"R": r, "slip_entry_pct": 0.0, "slip_exit_pct": 0.0} for r in rs]

    def test_ok(self):
        self.assertEqual(journal.kill_reasons(self.tr([1, -1, 2, -1] * 10), 0.05, []), [])

    def test_dd(self):
        self.assertTrue(any("obsunięcie" in x for x in journal.kill_reasons([], 0.11, [])))

    def test_streak(self):
        self.assertTrue(any("z rzędu" in x for x in journal.kill_reasons(self.tr([1] + [-1] * 15), 0, [])))
        self.assertFalse(any("z rzędu" in x for x in journal.kill_reasons(self.tr([1] + [-1] * 14), 0, [])))

    def test_r40_r100_pf(self):
        self.assertTrue(any("40" in x for x in journal.kill_reasons(self.tr([-0.5] * 10 + [0.1] * 30), 0, [])) is False)
        self.assertTrue(any("po 40" in x for x in journal.kill_reasons(self.tr([-1, -1, 0.5] * 13 + [-1]), 0, [])))
        rs = [1.0, -1.0, -0.2] * 34
        out = journal.kill_reasons(self.tr(rs), 0, [])
        self.assertTrue(any("PF" in x for x in out))

    def test_execution(self):
        t = [{"R": 0.1, "slip_entry_pct": 0.1, "slip_exit_pct": 0.1} for _ in range(10)]
        self.assertTrue(any("poślizg" in x for x in journal.kill_reasons(t, 0, [])))
        sig = [{"action": "entered", "delay_min": 45} for _ in range(3)] + [{"action": "entered", "delay_min": 5}] * 17
        self.assertTrue(any("opóźnienie" in x for x in journal.kill_reasons([], 0, sig)))
        sig = [{"action": "skipped", "detail": "bot nie działał"}] * 2 + [{"action": "entered", "delay_min": 1}] * 18
        self.assertTrue(any("pominięte" in x for x in journal.kill_reasons([], 0, sig)))
        sig = [{"action": "skipped", "detail": "cena już przy TP"}] * 2 + [{"action": "entered", "delay_min": 1}] * 18
        self.assertFalse(any("pominięte" in x for x in journal.kill_reasons([], 0, sig)))
        cmp = [{"matched": "12", "avg_diff_R": "-0.4"}]
        self.assertTrue(any("rozbieżność" in x for x in journal.kill_reasons([], 0, [], cmp)))


class TestCarryRule(unittest.TestCase):
    def test_fav(self):
        day_end = 100 * DAY
        settle = [(day_end - d * DAY + h * 3600_000, 0.0001) for d in range(1, 8) for h in (0, 8, 16)]
        f, ok = carry_live.fav_from_settlements(settle, day_end)
        self.assertTrue(ok)
        self.assertAlmostEqual(f, 0.0003 * 365, places=9)          # 0,03%/dzień → 10,95%/rok
        f, ok = carry_live.fav_from_settlements(settle[3:], day_end)
        self.assertFalse(ok)

    def test_decide(self):
        held = {"A": (-0.01, True), "B": (0.05, True), "C": (0.2, False)}
        cand = [("D", 0.30, True, 1), ("E", 0.12, True, 5), ("F", 0.50, True, 25), ("G", 0.09, True, 2),
                ("B", 0.05, True, 3)]
        ex, en = carry_live.decide(held, cand, K=3)
        self.assertEqual(sorted(ex), ["A", "C"])
        self.assertEqual(en, ["D", "E"])                             # F poza top 20, G < 10%, B już trzymany
        ex, en = carry_live.decide({"B": (0.05, True)}, cand, K=2)
        self.assertEqual(en, ["D"])


def bars(ts0, n, px=100.0):
    return [[ts0 + k * 300_000, px, px * 1.001, px * 0.999, px] for k in range(n)]


class TestCarryLegs(unittest.TestCase):
    def mk(self, fail=()):
        t0 = 10 * DAY
        b5 = {"X-USDT-SWAP": bars(t0, 400), "X-USDT": bars(t0, 400)}
        ex = FakeExchange(b5, equity=100000.0, spot={"X": "X-USDT"}, fail=set(fail))
        ex.t = t0 + 3 * 300_000
        st = {"carry": {"base": 100000.0, "peak": 100000.0}}
        msgs = []
        cl = carry_live.CarryLive(ex, None, st, msgs.append, now=lambda: ex.t, market=None)
        return ex, cl, st, msgs

    def test_entry_both_legs(self):
        ex, cl, st, msgs = self.mk()
        cl._start_entry("X", ex.perp_inst("X"), ex.spot_inst("X"), 0.2)
        self.assertIn("X", st["carry"]["pending"])                   # obie nogi czekają jako limit
        ex.advance(ex.t + 300_000); cl._pending()                    # cena przeszła oba poziomy → obie nogi maker
        self.assertIn("X", st["carry"]["pos"])
        p = st["carry"]["pos"]["X"]
        self.assertAlmostEqual(ex.pos["X-USDT-SWAP"], -p["contracts"])
        self.assertAlmostEqual(ex.bal["X"], p["qs"], places=6)
        self.assertAlmostEqual(p["qs"] * 100, 100000 / 10 / 1.1, delta=50)

    def test_timeout_fallback_market(self):
        t0 = 10 * DAY
        flat = lambda: [[t0 + k * 300_000, 100.0, 100.0, 100.0, 100.0] for k in range(400)]
        ex = FakeExchange({"X-USDT-SWAP": flat(), "X-USDT": flat()}, equity=100000.0, spot={"X": "X-USDT"})
        ex.t = t0 + 3 * 300_000
        st = {"carry": {"base": 100000.0, "peak": 100000.0}}
        cl = carry_live.CarryLive(ex, None, st, lambda m: None, now=lambda: ex.t, market=None)
        cl._start_entry("X", ex.perp_inst("X"), ex.spot_inst("X"), 0.2)
        ex.advance(ex.t + 1800_000); cl._pending()
        self.assertIn("X", st["carry"]["pending"])                   # po 30 min nadal czeka
        ex.advance(ex.t + 1800_000 + 300_000); cl._pending()
        self.assertIn("X", st["carry"]["pos"])                       # po 1 h: rynek na obu nogach
        self.assertAlmostEqual(ex.bal["X"] * 1.0, -ex.pos["X-USDT-SWAP"], places=6)

    def test_perp_leg_rejected_closes_spot(self):
        ex, cl, st, msgs = self.mk(fail={"X-USDT-SWAP"})
        with self.assertRaises(RuntimeError):
            cl._start_entry("X", ex.perp_inst("X"), ex.spot_inst("X"), 0.2)
        self.assertNotIn("X", st["carry"]["pos"])
        self.assertAlmostEqual(ex.bal.get("X", 0.0), 0.0)

    def test_hedge_broken_closes_other_leg(self):
        ex, cl, st, msgs = self.mk()
        cl._start_entry("X", ex.perp_inst("X"), ex.spot_inst("X"), 0.2)
        ex.advance(ex.t + 3600_000 + 300_000); cl._pending()
        self.assertIn("X", st["carry"]["pos"])
        ex.pos["X-USDT-SWAP"] = 0.0                                   # noga perp zniknęła (np. likwidacja)
        cl._hedge_check()
        self.assertNotIn("X", st["carry"]["pos"])
        self.assertLess(ex.bal["X"], 1e-6)                            # spot sprzedany
        self.assertTrue(any("🚨" in m for m in msgs))


class FakeMarket:
    def __init__(self, uni, rates):
        self.uni, self.rates = uni, rates

    def universe(self, today):
        return self.uni

    def funding(self, inst, since):
        r = self.rates.get(inst.split("-")[0], 0.0)
        return [(since + k * 8 * 3600_000, r) for k in range(30)]


class TestCarryDaily(unittest.TestCase):
    def test_daily_entry_skip_and_exit(self):
        t0 = 10 * DAY
        b5 = {"X-USDT-SWAP": bars(t0, 400), "X-USDT": bars(t0, 400)}
        ex = FakeExchange(b5, equity=100000.0, spot={"X": "X-USDT"})
        ex.t = t0 + 20 * 60000
        st = {"carry": {"base": 100000.0, "peak": 100000.0}}
        mk = FakeMarket([("X", "X-USDT-SWAP", 1), ("Y", "Y-USDT-SWAP", 2), ("Z", "Z-USDT-SWAP", 3)],
                        {"X": 0.0005, "Y": 0.0005, "Z": 0.00001})
        cl = carry_live.CarryLive(ex, None, st, lambda m: None, now=lambda: ex.t, market=mk)
        cl.step()
        self.assertIn("X", st["carry"]["pending"])                   # X: 54%/rok → wejście
        rows = journal.read("carry")
        self.assertTrue(any(r["event"] == "skipped_no_inst" and r["base"] == "Y" for r in rows))   # brak instrumentu w demo
        self.assertFalse(any(r["base"] == "Z" for r in rows))        # Z < 10%/rok
        ex.advance(ex.t + 300_000); cl._pending()
        self.assertIn("X", st["carry"]["pos"])
        mk.rates["X"] = -0.0001                                       # funding ujemny → wyjście następnego dnia
        ex.t += DAY; cl.step()
        ex.advance(ex.t + 300_000); cl._pending()
        self.assertNotIn("X", st["carry"]["pos"])
        self.assertTrue(any(r["event"] == "close" for r in journal.read("carry")))


class TestS2Sizing(unittest.TestCase):
    def test_size(self):
        self.assertAlmostEqual(s2.size(10000, 100.0, 3.0), 0.005 * 10000 / 3.0)
        self.assertAlmostEqual(s2.size(10000, 100.0, 0.01), 3 * 10000 / 100.0)    # limit nominału 3×

    def test_oco_failure_closes(self):
        from demo import s2_live
        ex = FakeExchange({"BTC-USDT-SWAP": bars(0, 50)})
        ex.t = 10 * 300_000
        ex.place_oco = mock.Mock(side_effect=RuntimeError("algo odrzucone"))
        st = {"s2": {"base": 10000.0, "realized": 0.0, "peak": 10000.0}}
        bot = s2_live.S2Live(ex, None, st, lambda m: None, now=lambda: ex.t)
        ex.place("BTC-USDT-SWAP", "buy", 1.0, "market")
        p = dict(phase="pending", base="BTC", trade_inst="BTC-USDT-SWAP", fills=[dict(sz=1.0, px=100.0, kind="maker")],
                 k=1.0, sl_swap=90.0, tp_swap=120.0, tick=0.1, open_swap=100.0, sig_ts=0, ct=1.0)
        st["s2"]["pos"]["BTC-USDT-SWAP"] = p
        with self.assertRaises(RuntimeError):
            bot._activate("BTC-USDT-SWAP", p)
        self.assertAlmostEqual(ex.pos["BTC-USDT-SWAP"], 0.0)
        self.assertNotIn("BTC-USDT-SWAP", st["s2"]["pos"])


if __name__ == "__main__":
    unittest.main()
