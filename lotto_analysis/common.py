"""Wspólne: dane Lotto + dokładne przeliczanie wszystkich C(49,6) losowań."""
import json, itertools, os
import numpy as np

SRC = os.environ.get("LOTTO_JSON", "/home/user/ext/kkmitaDev370_Lotto/data/lotto.json")
# Losowania brakujące w archiwum (po 10.11.2026), zweryfikowane w kilku serwisach
EXTRA = [
    {"nr": 7430, "date": "2026-11-12", "numbers": [1, 4, 20, 27, 33, 34]},
    {"nr": 7431, "date": "2026-11-14", "numbers": [20, 32, 33, 36, 40, 42]},
    {"nr": 7432, "date": "2026-11-17", "numbers": [20, 22, 25, 39, 41, 45]},
    {"nr": 7433, "date": "2026-11-19", "numbers": [5, 11, 13, 41, 44, 48]},
]
HERE = os.environ.get("LOTTO_CACHE", os.path.dirname(os.path.abspath(__file__)))
N_ALL = 13983816


def load_draws():
    d = json.load(open(SRC)) + EXTRA
    for i, x in enumerate(d):
        assert x["nr"] == i + 1, x
        s = x["numbers"]
        assert len(s) == 6 and len(set(s)) == 6 and all(1 <= v <= 49 for v in s), x
        if i:
            assert x["date"] >= d[i - 1]["date"], x
    return d


def mask(nums):
    m = 0
    for v in nums:
        m |= 1 << (v - 1)
    return m


def all_masks():
    """Bitmaski wszystkich 13 983 816 kombinacji (cache na dysku)."""
    path = os.path.join(HERE, "all_masks.npy")
    if os.path.exists(path):
        return np.load(path)
    bits = [1 << i for i in range(49)]
    arr = np.fromiter((a | b | c | d | e | f for a, b, c, d, e, f in
                       itertools.combinations(bits, 6)), dtype=np.uint64, count=N_ALL)
    np.save(path, arr)
    return arr


def evaluate(tickets, masks=None):
    """Dokładnie: liczba losowań (z 13 983 816), w których co najmniej jeden
    zakład ma >=3, >=4, >=5, 6 trafień. Każde losowanie liczone raz."""
    if masks is None:
        masks = all_masks()
    best = np.zeros(len(masks), dtype=np.uint8)
    for t in tickets:
        h = np.bitwise_count(masks & np.uint64(mask(t)))
        np.maximum(best, h, out=best)
    cnt = np.bincount(best, minlength=7)
    return {k: int(cnt[k:].sum()) for k in (3, 4, 5, 6)}
