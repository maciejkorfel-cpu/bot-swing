"""Dziennik (CSV w demo_log/) i kryteria wyłączenia z research/STAN_BADAN.md sekcja 5 (R12)."""
import csv, os

LOG_DIR = os.getenv("DEMO_LOG_DIR", "demo_log")
FILES = {
    "trades": ["strategy", "inst", "trade_inst", "side", "signal_bar", "entry_ts", "exit_ts", "entry_px", "exit_px", "qty",
               "pnl", "R", "risk_usd", "fees", "funding", "why", "entry_type", "delay_min", "slip_entry_pct", "slip_exit_pct",
               "bt_R", "note"],
    "signals": ["strategy", "inst", "signal_bar", "decided_ts", "action", "delay_min", "detail"],
    "carry": ["event", "ts", "base", "perp_inst", "spot_inst", "qty", "spot_px", "perp_px", "fav_7d", "funding_recv",
              "pnl", "fees", "detail"],
    "daily": ["day", "total_eq", "s2_eq", "s2_dd_pct", "carry_eq", "s2_open", "carry_open", "s2_n", "s2_sumR", "s2_pf",
              "mode", "note"],
    "compare": ["day", "strategy", "inst", "bt_trades", "demo_trades", "matched", "bt_sumR", "demo_sumR", "avg_diff_R",
                "missing_in_demo", "extra_in_demo", "detail"],
}


def path(kind):
    return os.path.join(LOG_DIR, f"{kind}_demo.csv")


def append(kind, row):
    os.makedirs(LOG_DIR, exist_ok=True)
    p = path(kind)
    new = not os.path.exists(p)
    with open(p, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FILES[kind], extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerow({k: ("" if row.get(k) is None else row.get(k)) for k in FILES[kind]})


def read(kind):
    p = path(kind)
    if not os.path.exists(p):
        return []
    with open(p, encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ---------------------------------------------------------------------- kryteria wyłączenia (R12)
LIMITS = dict(dd=0.10, streak=15, r40=-9.0, r100=-10.0, pf100=1.0, slip=0.0006, delay_min=30, delay_share=0.10,
              skip_share=0.05, diff_R=0.3)


def streak_losses(rs):
    m = cur = 0
    for r in rs:
        cur = cur + 1 if r < 0 else 0
        m = max(m, cur)
    return m


def pf(rs):
    g = sum(r for r in rs if r > 0); l = -sum(r for r in rs if r < 0)
    return g / l if l > 0 else float("inf")


def kill_reasons(s2_trades, s2_dd, signals, compare_rows=None):
    """s2_trades: lista dict z polem R (float) w kolejności; s2_dd: bieżące obsunięcie (ułamek, dodatni);
    signals: lista dict sygnałów S2 (action, delay_min, slip). Zwraca listę powodów STOP (pusta = OK)."""
    L = LIMITS
    out = []
    rs = [float(t["R"]) for t in s2_trades if t.get("R") not in (None, "")]
    if s2_dd > L["dd"]:
        out.append(f"obsunięcie S2 {s2_dd:.1%} > {L['dd']:.0%}")
    st = streak_losses(rs)
    if st >= L["streak"]:
        out.append(f"{st} strat z rzędu (próg {L['streak']})")
    n = len(rs)
    if 40 <= n < 100 and sum(rs[:40]) < L["r40"]:
        out.append(f"suma R po 40 transakcjach {sum(rs[:40]):.1f} < {L['r40']}")
    if n >= 100 and sum(rs[:100]) < L["r100"]:
        out.append(f"suma R po 100 transakcjach {sum(rs[:100]):.1f} < {L['r100']}")
    if n >= 100 and pf(rs) < L["pf100"]:
        out.append(f"PF po {n} transakcjach {pf(rs):.2f} < 1,0")
    # wykonanie
    slips = [abs(float(x)) for t in s2_trades for x in (t.get("slip_entry_pct"), t.get("slip_exit_pct"))
             if x not in (None, "")]
    if len(slips) >= 10 and sum(slips) / len(slips) / 100 > L["slip"]:
        out.append(f"średni poślizg {sum(slips) / len(slips):.3f}% > 0,06%/zlecenie")
    sig = [s for s in signals if s.get("action") in ("entered", "skipped", "error")]
    if len(sig) >= 10:
        late = [s for s in sig if s.get("delay_min") not in (None, "") and float(s["delay_min"]) > L["delay_min"]]
        if len(late) / len(sig) > L["delay_share"]:
            out.append(f"opóźnienie > 30 min w {len(late)}/{len(sig)} sygnałów (> 10%)")
    if len(sig) >= 20:
        sk = [s for s in sig if s["action"] in ("skipped", "error") and "TP" not in (s.get("detail") or "")]
        if len(sk) / len(sig) > L["skip_share"]:
            out.append(f"pominięte sygnały {len(sk)}/{len(sig)} (> 5%)")
    if compare_rows:
        last = compare_rows[-1]
        try:
            if int(last.get("matched") or 0) >= 10 and abs(float(last.get("avg_diff_R") or 0)) > L["diff_R"]:
                out.append(f"rozbieżność demo vs backtest {float(last['avg_diff_R']):+.2f} R/transakcję (> 0,3R)")
        except ValueError:
            pass
    return out
