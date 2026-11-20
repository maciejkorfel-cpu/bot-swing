"""Chronologiczny backtest popularnych metod typowania (walk-forward).
Przy losowaniu t każda strategia widzi tylko losowania 0..t-1.
Każda strategia gra 5 rozłącznych zakładów (30 liczb)."""
import math, json, sys
import numpy as np
from common import load_draws, evaluate

draws = load_draws()
X = np.zeros((len(draws), 49), dtype=np.int8)
for i, d in enumerate(draws):
    X[i, np.array(d["numbers"]) - 1] = 1
T = len(draws)
START = 500  # rozgrzewka: pierwsze typowanie dla losowania nr 501
NUMS = np.arange(49)


def tickets_from_rank(order):
    top = order[:30]
    return [sorted(top[6 * k:6 * k + 6]) for k in range(5)]


def rank(score, rng):
    # malejąco po score, remisy losowo (żeby nie faworyzować małych liczb)
    return np.lexsort((rng.random(49), -score))


def pair_tickets(C, freq, rng):
    """Zachłannie: zaczynamy od najczęstszej wolnej pary, dokładamy liczbę o
    największej sumie współwystąpień z liczbami w zakładzie. Zakłady rozłączne."""
    C = C.astype(float) + rng.random((49, 49)) * 1e-3
    C = (C + C.T) / 2
    np.fill_diagonal(C, -1)
    free = np.ones(49, bool)
    out = []
    for _ in range(5):
        sub = np.where(free[:, None] & free[None, :], C, -1)
        a, b = np.unravel_index(np.argmax(sub), sub.shape)
        t = [a, b]; free[a] = free[b] = False
        while len(t) < 6:
            s = C[:, t].sum(1); s[~free] = -1e9
            c = int(np.argmax(s)); t.append(c); free[c] = False
        out.append(sorted(t))
    return out


STRATS = ["hot_50", "hot_200", "hot_all", "cold_50", "cold_200", "cold_all",
          "overdue", "fresh", "pairs_200", "pairs_all", "random"]


def run(seed=0, n_random=0):
    rng = np.random.default_rng(seed)
    cum = np.cumsum(X, 0)
    last = np.full(49, -1)
    for i in range(START):
        last[X[i] == 1] = i
    # współwystąpienia: dla "all" kumulatywnie, dla 200 okno przesuwne
    P_all = X[:START].T.astype(np.int32) @ X[:START].astype(np.int32)
    P_200 = X[START - 200:START].T.astype(np.int32) @ X[START - 200:START].astype(np.int32)
    names = STRATS + [f"rand_{k}" for k in range(n_random)]
    hits = {s: np.zeros((T - START, 5), np.int8) for s in names}
    for t in range(START, T):
        def win(w): return cum[t - 1] - (cum[t - 1 - w] if t - 1 - w >= 0 else 0)
        f50, f200, fall = win(50), win(200), cum[t - 1]
        gap = t - last
        P = {
            "hot_50": tickets_from_rank(rank(f50, rng)),
            "hot_200": tickets_from_rank(rank(f200, rng)),
            "hot_all": tickets_from_rank(rank(fall, rng)),
            "cold_50": tickets_from_rank(rank(-f50, rng)),
            "cold_200": tickets_from_rank(rank(-f200, rng)),
            "cold_all": tickets_from_rank(rank(-fall, rng)),
            "overdue": tickets_from_rank(rank(gap, rng)),
            "fresh": tickets_from_rank(rank(-gap, rng)),
            "pairs_200": pair_tickets(P_200, f200, rng),
            "pairs_all": pair_tickets(P_all, fall, rng),
            "random": tickets_from_rank(rng.permutation(49)),
        }
        for k in range(n_random):
            P[f"rand_{k}"] = tickets_from_rank(rng.permutation(49))
        row = X[t]
        for s, tk in P.items():
            hits[s][t - START] = [row[np.array(x)].sum() for x in tk]
        # aktualizacja stanu PO typowaniu (tylko przeszłość przy typowaniu t)
        last[row == 1] = t
        v = row.astype(np.int32)
        P_all += np.outer(v, v)
        P_200 += np.outer(v, v)
        o = X[t - 200].astype(np.int32)
        P_200 -= np.outer(o, o)
    return hits


def binom_sf(k, n, p):
    """P(X>=k), X~Bin(n,p) — dokładnie, w logach."""
    if k <= 0:
        return 1.0
    lp, lq = math.log(p), math.log1p(-p)
    terms = [math.lgamma(n + 1) - math.lgamma(j + 1) - math.lgamma(n - j + 1) + j * lp + (n - j) * lq
             for j in range(k, n + 1)]
    m = max(terms)
    return min(1.0, math.exp(m) * sum(math.exp(x - m) for x in terms))


if __name__ == "__main__":
    n_random = int(sys.argv[1]) if len(sys.argv) > 1 else 200
    # Dokładny rozkład zerowy dla 5 rozłącznych zakładów (wszystkie takie portfele są
    # równoważne z dokładnością do przenumerowania liczb):
    ex = evaluate([list(range(1 + 6 * k, 7 + 6 * k)) for k in range(5)])
    p3, p4 = ex[3] / 13983816, ex[4] / 13983816
    # suma trafień w 30 liczbach ~ hipergeometryczny(49, 30, 6)
    mu = 6 * 30 / 49
    var = 6 * (30 / 49) * (19 / 49) * (43 / 48)
    hits = run(seed=12345, n_random=n_random)
    n = T - START
    half = n // 2
    res = {}
    for s, h in hits.items():
        tot = h.sum(1).astype(float); mx = h.max(1)
        r = {}
        for name, sl in (("all", slice(None)), ("h1", slice(0, half)), ("h2", slice(half, None))):
            m = len(tot[sl])
            z = (tot[sl].mean() - mu) / math.sqrt(var / m)
            r[name] = dict(n=m, mean=tot[sl].mean(), z=z, ge3=int((mx[sl] >= 3).sum()),
                           ge4=int((mx[sl] >= 4).sum()), ge5=int((mx[sl] >= 5).sum()))
        res[s] = r
    print(f"Losowania testowe: nr {START + 1}..{T} ({draws[START]['date']} .. {draws[-1]['date']}), n={n}")
    print(f"Teoria (5 rozłącznych zakładów): E[suma trafień]={mu:.4f}, P(max>=3)={p3:.5f}, P(max>=4)={p4:.6f}")
    print(f"Oczekiwane w n={n}: >=3: {n * p3:.1f}, >=4: {n * p4:.2f}")
    print(f"{'strategia':11s} {'śr.traf':>8s} {'z':>6s} {'>=3':>5s} {'p(>=3)':>7s} {'>=4':>4s} {'p(>=4)':>7s} {'>=5':>4s} | z_h1  z_h2")
    for s in STRATS:
        a = res[s]["all"]
        print(f"{s:11s} {a['mean']:8.4f} {a['z']:6.2f} {a['ge3']:5d} {binom_sf(a['ge3'], n, p3):7.3f} "
              f"{a['ge4']:4d} {binom_sf(a['ge4'], n, p4):7.3f} {a['ge5']:4d} | {res[s]['h1']['z']:5.2f} {res[s]['h2']['z']:5.2f}")
    # kontrola: rozkład wyników dla n_random losowych strategii
    rz = np.array([res[f"rand_{k}"]["all"]["z"] for k in range(n_random)])
    r3 = np.array([res[f"rand_{k}"]["all"]["ge3"] for k in range(n_random)])
    r4 = np.array([res[f"rand_{k}"]["all"]["ge4"] for k in range(n_random)])
    print(f"\n{n_random} losowych strategii-kontroli: z śr={rz.mean():.2f} sd={rz.std():.2f} "
          f"max={rz.max():.2f}; >=3 śr={r3.mean():.1f} [{np.percentile(r3, 2.5):.0f}, {np.percentile(r3, 97.5):.0f}]; "
          f">=4 śr={r4.mean():.2f} [{np.percentile(r4, 2.5):.0f}, {np.percentile(r4, 97.5):.0f}]")
    # data snooping: jak często najlepsza z 10 losowych "strategii" ma z >= najlepszego z?
    real = [s for s in STRATS if s != "random"]
    best_real = max(res[s]["all"]["z"] for s in real)
    groups = rz[: (n_random // 10) * 10].reshape(-1, 10).max(1)
    print(f"Najlepsze z wśród 10 metod: {best_real:.2f}; P(max z z 10 losowych >= tego) ~ {(groups >= best_real).mean():.2f} "
          f"(na {len(groups)} grupach); próg Bonferroniego (10 testów, alfa=0.05, dwustronnie): |z|>{2.81}")
    # walidacja poza próbą: wybór najlepszej metody na 1. połowie, ocena na 2.
    b = max(real, key=lambda s: res[s]["h1"]["z"])
    print(f"Najlepsza w 1. połowie: {b} (z={res[b]['h1']['z']:.2f}); w 2. połowie z={res[b]['h2']['z']:.2f}")
    b4 = max(real, key=lambda s: res[s]["h1"]["ge4"])
    print(f"Najwięcej >=4 w 1. połowie: {b4} ({res[b4]['h1']['ge4']} vs oczek. {half * p4:.1f}); "
          f"w 2. połowie {res[b4]['h2']['ge4']} vs oczek. {(n - half) * p4:.1f}")
    json.dump({s: res[s] for s in STRATS}, open("backtest_results.json", "w"), indent=1, default=float)
