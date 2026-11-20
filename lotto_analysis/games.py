from math import comb as C
def hyp(N,K,n,k):  # losuje się K z N, gracz ma n liczb, P(k trafień)
    return C(n,k)*C(N-n,K-k)/C(N,K)
def ge(N,K,n,k): return sum(hyp(N,K,n,j) for j in range(k,min(n,K)+1))
f=lambda p: f"1 : {1/p:,.0f}".replace(","," ")
print("Lotto 6/49: 6:",f(ge(49,6,6,6))," >=5:",f(ge(49,6,6,5))," >=4:",f(ge(49,6,6,4))," >=3:",f(ge(49,6,6,3)))
print("Mini 5/42:  5:",f(ge(42,5,5,5))," >=4:",f(ge(42,5,5,4))," >=3:",f(ge(42,5,5,3)))
print("Szybkie 600 6/32: 6:",f(ge(32,6,6,6))," >=5:",f(ge(32,6,6,5))," >=4:",f(ge(32,6,6,4))," >=3:",f(ge(32,6,6,3))," >=2:",f(ge(32,6,6,2)))
k=lambda j: hyp(24,12,12,j)
one=lambda j: sum(k(i) for i in range(j,13))
print("Kaskada 12/24 (2 zestawy na los, liczone jak 2 niezal. zakłady≈): 12:",f(2*k(12))," >=8 w zestawie:",f(one(8)), " los>=8 ~", f(1-(1-one(8))**2))
print("  rozklad trafien w zestawie:", {j: round(k(j),6) for j in range(4,13)})
# Ekstra Pensja: 5 z 35 + 1 z 4
for tiers in ([(5,1),(5,0),(4,1),(4,0),(3,1),(3,0),(2,1)], [(5,1),(5,0),(4,1),(4,0),(3,1),(3,0),(2,1),(1,1)], [(5,1),(5,0),(4,1),(4,0),(3,1),(3,0),(2,1),(2,0)]):
    p=sum(hyp(35,5,5,a)*(0.25 if b else 0.75) for a,b in tiers)
    print("Ekstra Pensja tiers",tiers[-1], "P(any)=",f(p))
print("Ekstra Pensja 5+1:",f(hyp(35,5,5,5)/4)," >=5 (bez względu na +1):",f(hyp(35,5,5,5))," >=4:",f(ge(35,5,5,4)))
ej=lambda a,b: hyp(50,5,5,a)*hyp(12,2,2,b)
print("Eurojackpot 5+2:",f(ej(5,2))," 5+x:",f(hyp(50,5,5,5))," >=4 z 50:",f(ge(50,5,5,4)))
ejany=sum(ej(a,b) for a in range(6) for b in range(3) if (a,b) in [(5,2),(5,1),(5,0),(4,2),(4,1),(4,0),(3,2),(2,2),(3,1),(3,0),(1,2),(2,1)])
print("Eurojackpot any (12 stopni):",f(ejany))
for n in (1,2,3,4,5,10):
    print(f"Multi Multi {n}/{n} (20 z 80):",f(hyp(80,20,n,n)), f"  Keno {n}/{n} (20 z 70):", f(hyp(70,20,n,n)))
