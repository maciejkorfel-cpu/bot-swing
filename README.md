# Bot DEMO OKX: S2 + carry C2 (paper trading, 6 miesięcy)

> **Tylko konto DEMO.** Każde zapytanie ma nagłówek `x-simulated-trading: 1`. Bot zatrzymuje się bez żadnego zlecenia,
> gdy `OKX_DEMO` ≠ `1` albo gdy klucz API działa na koncie realnym (sprawdzane przy każdym starcie, `demo/okx.py`).
> Klucze tylko w GitHub Secrets — repo jest publiczne.

Stara strategia (Donchian 20 4H) jest wyłączona: `.github/workflows/swing-bot.yml` nie ma harmonogramu, a jego zadanie
się nie wykonuje. Nowy bot: `.github/workflows/demo-bot.yml` → `python swing_bot.py` → `demo/bot.py`.

## Strategie (reguły zamrożone w badaniach, bez zmian)
**S2** (`research/finalists.py`, `research/STAN_BADAN.md`): tylko LONG, BTC i ETH, świece 4H.
- Sygnał: zamknięcie 4H > najwyższe high z 55 poprzednich świec **i** > EMA(300 świec 4H ≈ 50 dni).
- SL = open − 3·ATR14, TP = open + 4·ATR14 (limit), trailing = najwyższe zamknięcie od wejścia − 4·ATR14
  (po każdej świecy 4H, tylko w górę). SL i TP stoją na giełdzie (zlecenie OCO).
- Ryzyko 0,5% kapitału S2 na transakcję, nominał ≤ 3× kapitału, wspólne konto BTC+ETH.
- Wejście (research_edge, część 2): limit post-only po cenie open świecy (≤ bid), ważny 1 h, potem rynek.
  Gdy cena po 1 h jest już przy TP → transakcja pominięta (jak w modelu).

**Carry C2** (`research_crypto2/finalists_c2.py`): long spot + short perp na tym samym coinie, konto ujednolicone.
- Uniwersum: top 20 perpów OKX USDT-SWAP wg średniego wolumenu z 30 dni, z parą spot USDT, ≥ 60 dni od listingu.
- Raz dziennie (po 00:10 UTC): wejście gdy średni funding z 7 dni ≥ 10%/rok (maks. 10 pozycji, malejąco),
  wyjście gdy < 0. Pozycja = (kapitał carry / 10) / 1,1.
- Obie nogi jako limit (1 h), potem rynek. Gdy jedna noga się nie wypełni albo zniknie, druga jest zamykana i przychodzi alarm.
- Odchylenie od backtestu: w demo UE handlujemy X-Perp; coin bez X-Perp/spot w demo jest pomijany
  (wpis `skipped_no_inst` w `demo_log/carry_demo.csv`).

## Dziennik i porównanie z backtestem (`demo_log/`)
| plik | zawartość |
|---|---|
| `trades_demo.csv` | każda transakcja S2: wejście/wyjście, typ wejścia (maker/taker), opóźnienie, poślizg, wynik w USD i R |
| `signals_demo.csv` | każdy sygnał S2 i decyzja (wejście, pominięcie, błąd, tryb STOP) |
| `carry_demo.csv` | otwarcia/zamknięcia carry, otrzymany funding, opłaty, pominięcia |
| `daily_demo.csv` | dzienne equity, DD, liczba transakcji, suma R, PF, tryb |
| `compare_demo.csv` | codziennie: backtest S2 (ten sam silnik co research) na tych samych świecach od startu vs demo |

## Kryteria wyłączenia (R12) — bot sam przechodzi w tryb STOP i wysyła alarm
Obsunięcie S2 > 10%; 15 strat z rzędu; suma R po 40 transakcjach < −9 albo po 100 < −10; PF po 100 < 1,0;
średni poślizg > 0,06%/zlecenie; opóźnienie wejścia > 30 min w > 10% sygnałów; pominięte sygnały > 5%;
rozbieżność demo vs backtest > 0,3R/transakcję. Carry: obsunięcie > 3% (backtest < 1%).
STOP = brak nowych wejść; otwarte pozycje zostają z SL/TP na giełdzie. Ręcznie: komenda `/stop` na Telegramie,
`/status` = podsumowanie. Wznowienie po STOP tylko przez edycję `demo_state.json` (`"mode": "RUN"`) po przeglądzie.

## Telegram
Otwarcie i zamknięcie pozycji, codzienne podsumowanie (pierwszy przebieg po północy UTC), alarm przy błędzie
(maks. raz na 3 h) i przy problemie z nogą carry.

## Konfiguracja (GitHub → Settings → Secrets and variables → Actions)
*Secrets* (wymagane, workflow bez nich kończy się błędem i nic nie robi):
`OKX_API_KEY`, `OKX_API_SECRET`, `OKX_API_PASSPHRASE` (klucz utworzony w **Demo trading**), `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`.
*Variables* (opcjonalne): `OKX_API_HOST` (domyślnie `https://eea.okx.com`), `OKX_PERP_VENUE` (`xperp` / `swap`),
`CARRY_FRAC` (część kapitału demo dla carry, domyślnie 0.8), `ENABLE_S2`, `ENABLE_CARRY` (`1`/`0`).
Konto demo: carry wymaga trybu **multi-currency margin** albo **portfolio margin**. W innym trybie carry się
nie uruchomi i przyjdzie alarm.

## Testy
```
python -m unittest discover -s demo/tests -t . -p "test_units.py"   # 20 testów (blokada demo, R12, carry, nogi, OCO)
python -m unittest demo.tests.test_s2_vs_engine -v                 # S2 = research/engine.py (wymaga research/data)
python -m unittest demo.tests.test_dryrun_replay -v                # na sucho: bot na historii 5m vs backtest (~1 min)
python swing_bot.py --dry                                          # bez kluczy: bieżący stan reguł S2 i C2
```
