"""Szybkie 600: porównanie planów gry za 10 zł (dokładnie, wszystkie 906 192 wyniki)."""
from itertools import combinations
from collections import Counter
import secrets
PRIZE = {6: 31200, 5: 250, 4: 50, 3: 4, 2: 2}
ALL = [sum(1 << (x - 1) for x in c) for c in combinations(range(1, 33), 6)]
N = len(ALL)
def dist(tickets):
    ms = [sum(1 << (x - 1) for x in t) for t in tickets]
    c = Counter()
    for d in ALL:
        c[sum(PRIZE.get(bin(d & m).count("1"), 0) for m in ms)] += 1
    return {k: v / N for k, v in c.items()}
def conv(a, b):
    out = Counter()
    for x, p in a.items():
        for y, q in b.items():
            out[x + y] += p * q
    return dict(out)
def show(name, d, cost):
    ev = sum(k * p for k, p in d.items())
    f = lambda cond: 100 * sum(p for k, p in d.items() if cond(k))
    print(f"{name:34s} koszt {cost:2d} zł | śr. zwrot {ev:5.2f} zł | odzyskasz >=połowę {f(lambda k: k >= cost/2):5.1f}% | "
          f"wyjdziesz na plus {f(lambda k: k > cost):5.2f}% | >=50 zł {f(lambda k: k >= 50):5.2f}% | 0 zł {f(lambda k: k == 0):5.1f}%")
r = secrets.SystemRandom(); p = r.sample(range(1, 33), 32)
T = [sorted(p[6*i:6*i+6]) for i in range(5)]
one = dist(T[:1])
show("A: 5 zakładów x 1 losowanie", dist(T), 10)
d = one
for _ in range(4): d = conv(d, one)
show("B: 1 zakład x 5 losowań", d, 10)
two = dist(T[:2]); d2 = conv(conv(two, two), one)
show("C: 2+2+1 zakłady w 3 losowaniach", d2, 10)
print("Zakłady (bez wspólnych liczb):", T)
