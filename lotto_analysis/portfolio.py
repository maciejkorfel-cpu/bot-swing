"""Portfel 5 zakładów: dokładna ocena na wszystkich C(49,6) wynikach + porównanie z losowymi."""
import math, itertools, sys
import numpy as np
from common import all_masks, evaluate, N_ALL

N = N_ALL
K4 = 15 * 903 + 6 * 43 + 1   # zakłady z >=4 trafieniami dla danego losowania: 13 804
K5 = 6 * 43 + 1              # >=5: 259

PORTFEL = [
    [2, 18, 33, 37, 44, 49],
    [6, 21, 29, 35, 40, 42],
    [9, 24, 32, 38, 43, 47],
    [14, 26, 30, 34, 41, 48],
    [15, 28, 36, 39, 45, 46],
]

for t in PORTFEL:
    assert len(set(t)) == 6 and t == sorted(t) and all(1 <= v <= 49 for v in t)
inter = [len(set(a) & set(b)) for a, b in itertools.combinations(PORTFEL, 2)]
print("maks. część wspólna dwóch zakładów:", max(inter))

M = all_masks()
r = evaluate(PORTFEL, M)
print("Portfel — liczba wyników (z 13 983 816), w których >=1 zakład ma k+ trafień:", r)
for k in (3, 4, 5, 6):
    print(f"  P(>= {k}) = {r[k]}/{N} = {100 * r[k] / N:.5f}%")
print(f"Górna granica (nierówność Boole'a) dla 5 zakładów: >=4: {5 * K4} , >=5: {5 * K5}")

# Średnia dla 5 różnych zakładów losowanych jednolicie (dokładnie, bez symulacji):
# dla każdego losowania D jest dokładnie K "wygrywających" zakładów, więc
# P(żaden z 5 różnych losowych zakładów nie wygrywa) = C(N-K,5)/C(N,5).
def rand_p(K):
    return 1 - math.comb(N - K, 5) / math.comb(N, 5)
p4r, p5r = rand_p(K4), rand_p(K5)
p3r = None
print(f"Losowe 5 różnych zakładów (dokładnie): P(>=4)={100 * p4r:.5f}%  P(>=5)={100 * p5r:.6f}%")
print(f"Różnica: >=4: {100 * (r[4] / N - p4r):+.6f} pp, >=5: {100 * (r[5] / N - p5r):+.7f} pp")

# Kontrola symulacją: losowe portfele, każdy oceniony dokładnie na wszystkich wynikach
S = int(sys.argv[1]) if len(sys.argv) > 1 else 0
if S:
    rng = np.random.default_rng(7)
    v4, v5, v3 = [], [], []
    for _ in range(S):
        while True:
            P = [sorted(rng.choice(49, 6, replace=False) + 1) for _ in range(5)]
            if len({tuple(p) for p in P}) == 5:
                break
        e = evaluate(P, M)
        v4.append(e[4] / N); v5.append(e[5] / N); v3.append(e[3] / N)
    bound = {"P(>=4)": 5 * K4 / N, "P(>=5)": 5 * K5 / N}
    for name, v in (("P(>=3)", v3), ("P(>=4)", v4), ("P(>=5)", v5)):
        v = np.array(v); se = v.std(ddof=1) / math.sqrt(S)
        print(f"Symulacja {S} losowych portfeli: {name} śr={100 * v.mean():.5f}%  "
              f"95% CI [{100 * (v.mean() - 1.96 * se):.5f}%, {100 * (v.mean() + 1.96 * se):.5f}%]  "
              f"min={100 * v.min():.5f}%  odsetek portfeli na granicy optimum: {(v >= bound[name] - 1e-12).mean() if name in bound else float('nan'):.3f}")
