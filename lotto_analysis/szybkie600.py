"""Szybkie 600 (6 z 32): dokładna ocena na wszystkich C(32,6)=906 192 wynikach."""
from itertools import combinations
from math import comb
import secrets
PRIZE = {6: 31200, 5: 250, 4: 50, 3: 4, 2: 2}   # stawka 2 zł; 6/6 = 600 zł/tydz. x 52 (nominalnie)
ALL = [sum(1 << (x - 1) for x in c) for c in combinations(range(1, 33), 6)]
N = len(ALL)
def evaluate(tickets):
    ms = [sum(1 << (x - 1) for x in t) for t in tickets]
    best = [0] * 7; ev = 0
    for d in ALL:
        h = [bin(d & m).count("1") for m in ms]
        best[max(h)] += 1
        ev += sum(PRIZE.get(k, 0) for k in h)
    ge = {k: sum(best[k:]) for k in range(7)}
    return ge, ev / N
r = secrets.SystemRandom(); perm = r.sample(range(1, 33), 32)
tickets = [sorted(perm[6 * i:6 * i + 6]) for i in range(5)]
print("Zakłady:", tickets)
for B in (1, 2, 5):
    ge, ev = evaluate(tickets[:B])
    print(f"{B} zakł. ({2*B} zł): " + "  ".join(f">={k}: {100*ge[k]/N:.3f}% (1:{N/ge[k]:.1f})" for k in (2, 3, 4, 5, 6))
          + f"  | śr. wygrana {ev:.3f} zł, zwrot {100*ev/(2*B):.1f}%")
# porównanie: 2 zakłady z 2 wspólnymi liczbami
t2 = [tickets[0], sorted(tickets[0][:2] + tickets[1][:4])]
ge, ev = evaluate(t2)
print("2 zakł. z 2 wspólnymi:", "  ".join(f">={k}: {100*ge[k]/N:.3f}%" for k in (3, 4, 5)))
