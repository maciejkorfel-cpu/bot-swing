"""Keno (20 z 70, gracz typuje 1-10 liczb): dokładne prawdopodobieństwa + losowanie liczb."""
from math import comb as C
import secrets
N, D = 70, 20
def hyp(n, k): return C(n, k) * C(N - n, D - k) / C(N, D)       # P(k trafień przy n typowanych)
def all_drawn(m): return C(N - m, D - m) / C(N, D) if m <= D else 0.0              # P(m konkretnych liczb jest wśród 20)
inv = lambda p: f"1 : {1/p:,.1f}".replace(",", " ")
print("n | P(n z n)        | rozkład trafień k: P(k)")
for n in range(1, 11):
    print(f"{n:2d} | {inv(hyp(n, n)):15s} | " + "  ".join(f"{k}:{100*hyp(n,k):.3f}%" for k in range(n + 1)))
# n=10: P(>=5), P(0), P(>=7)...
for lo in (5, 6, 7, 8, 9):
    print(f"n=10, P(>={lo}) = {100*sum(hyp(10,k) for k in range(lo,11)):.4f}%  ({inv(sum(hyp(10,k) for k in range(lo,11)))})")
# 5 rozłącznych zakładów po n liczb: P(co najmniej jeden trafia n z n), dokładnie (włączanie-wyłączanie)
for n in (3, 4, 5, 10):
    for B in (5,):
        pu = sum((-1) ** (j + 1) * C(B, j) * all_drawn(j * n) for j in range(1, B + 1))
        # zakłady z jedną wspólną liczbą dla porównania (para z 1 wspólną): P(A∩B)=all_drawn(2n-1)
        print(f"{B} rozłącznych zakładów po {n}: P(>=1 zakład {n} z {n}) = {100*pu:.4f}% ({inv(pu)});  suma pojedynczych = {100*B*hyp(n,n):.4f}%")
# kontrola: czy rozłączność jest najlepsza? dokładne włączanie-wyłączanie dla dowolnych zakładów
from itertools import combinations
def p_union(T):
    return sum((-1) ** (len(S) + 1) * all_drawn(len(set().union(*S))) for r in range(1, len(T) + 1) for S in combinations(T, r))
for n in (4, 10):
    dis = [set(range(n*i, n*i+n)) for i in range(5)]
    ov1 = [set(range((n-1)*i, (n-1)*i+n)) for i in range(5)]       # łańcuch: sąsiednie mają 1 wspólną
    ovh = [set(range(i, i+n)) for i in range(5)]                    # duże nakładanie
    print(f"n={n}: rozłączne {100*p_union(dis):.5f}%  po 1 wspólnej {100*p_union(ov1):.5f}%  mocno nakładające {100*p_union(ovh):.5f}%")
rng = secrets.SystemRandom()
pool = rng.sample(range(1, 71), 50)
print("Liczby (secrets.SystemRandom):", [sorted(pool[10*i:10*i+10]) for i in range(5)])
