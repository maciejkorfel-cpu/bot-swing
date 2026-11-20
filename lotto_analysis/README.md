# Analiza Lotto (6 z 49) — portfel 5 zakładów

Dane: `data/lotto.json` z https://github.com/kkmitaDev370/Lotto (archiwum z mbnet.com.pl,
losowania nr 1–7429, 27.01.1957–10.11.2026) + losowania 7430–7433 (12–19.11.2026) wpisane
ręcznie w `common.py` po zweryfikowaniu w kilku serwisach z wynikami.

    git clone --depth 1 https://github.com/kkmitaDev370/Lotto /tmp/lotto
    export LOTTO_JSON=/tmp/lotto/data/lotto.json LOTTO_CACHE=/tmp   # cache bitmasek ~112 MB
    python3 backtest.py 2000   # walk-forward 10 metod + 2000 losowych strategii kontrolnych
    python3 robust.py          # te same metody na 20 ziarnach rozstrzygania remisów
    python3 portfolio.py 300   # dokładna ocena portfela na 13 983 816 wynikach + 300 losowych portfeli

Wyniki przebiegów: `*_out.txt`, `backtest_results.json`.
