import math, numpy as np
import backtest as B
mu=6*30/49; var=6*(30/49)*(19/49)*(43/48)
Z={s:[] for s in B.STRATS}; G4={s:[] for s in B.STRATS}; G3={s:[] for s in B.STRATS}
for seed in range(20):
    h=B.run(seed=1000+seed)
    for s,x in h.items():
        tot=x.sum(1); n=len(tot)
        Z[s].append((tot.mean()-mu)/math.sqrt(var/n)); G4[s].append(int((x.max(1)>=4).sum())); G3[s].append(int((x.max(1)>=3).sum()))
print(f"{'strategia':11s} {'z śr':>6s} {'z min':>6s} {'z max':>6s} {'>=3 śr':>7s} {'>=4 śr':>7s} {'>=4 min-max':>11s}")
for s in B.STRATS:
    z=np.array(Z[s]); print(f"{s:11s} {z.mean():6.2f} {z.min():6.2f} {z.max():6.2f} {np.mean(G3[s]):7.1f} {np.mean(G4[s]):7.1f} {min(G4[s]):5d}-{max(G4[s])}")
