# Bot swing: sygnały OKX na Telegram

> **Zegar:** cron GitHuba bywa uruchamiany tylko co kilka godzin, dlatego bota co 5 min
> uruchamia darmowy [cron-job.org](https://cron-job.org) (zapytanie POST do GitHub API,
> `workflow_dispatch` z `cicho=true`). Cron GitHuba co 15 min zostaje jako zapas.

Bez kluczy API OKX bot tylko wysyła sygnały; z kluczami konta demo sam otwiera pozycje. Co 5 minut skanuje 10 par
(BTC, ETH, SOL, XRP, HYPE, DOGE, AVAX, LINK, SUI, BCH). Gdy setup spełnia wszystkie
warunki, wysyła na Telegram gotowe dane do zlecenia. Pozycję otwierasz sam w telefonie.

## Strategia: wybicie kanału Donchiana 4H

| Krok | Warunek |
|---|---|
| Wejście LONG | Zamknięcie świecy 4H powyżej najwyższego high z 20 poprzednich świec |
| Wejście SHORT | Zamknięcie świecy 4H poniżej najniższego low z 20 poprzednich świec |
| SL startowy | 2 x ATR(14) z 4H od wejścia |
| Trailing | Po każdej zamkniętej świecy 4H SL = zamknięcie -/+ 5,5 x ATR, tylko w stronę zysku. Bez TP. |
| Wielkość | RISK_PCT (domyślnie 0,5%) kapitału ryzyka na transakcję, marża izolowana 3x |
| Limit pozycji | brak (MAX_POSITIONS=0); jedna pozycja na parę |
| Filtr BTC | opcjonalny (BTC_FILTER=1): long tylko gdy BTC > EMA300 4H, short tylko pod |

### Wyniki backtestu (dane OKX 4H, X 2023 - X 2026, 9 par z kontraktem demo, prowizje 0,08%/stronę, ryzyko 0,5%)

| Wariant | Transakcje | Trafność | PF | Wynik | Obsunięcie | Ostatnie 14 mies. |
|---|---|---|---|---|---|---|
| **Bez filtra BTC (domyślny)** | 883 | 26% | 1,59 | +379R (ok. +447%) | 36% | PF 1,31, +49% |
| Z filtrem BTC | 749 | 24% | 1,50 | +280R (ok. +242%) | 34% | PF 1,07, +7% |

Trafność jest niska: zysk robią nieliczne długie ruchy. Serie 10-20 strat z rzędu są normalne.
Pary chodzą razem, więc bez limitu bywa otwartych naraz do 9 pozycji w tę samą stronę.
Na mniejszych altach (DOT, LTC, FIL...) strategia dawała wynik ok. zera.

## Instalacja (ok. 10 minut)

1. **Bot Telegram (osobny, nie ten od nieruchomości)**
   - W Telegramie napisz do `@BotFather` → `/newbot` → zapisz **token**.
   - Napisz cokolwiek do nowego bota, potem otwórz w przeglądarce
     `https://api.telegram.org/bot<TOKEN>/getUpdates` i skopiuj `"chat":{"id": ...}`. To jest **chat_id**.

2. **Repozytorium:** wgraj pliki do repo `bot-krypto`, np. do folderu głównego albo nowego repo `bot-swing`.
   Musi się tam znaleźć `.github/workflows/swing-bot.yml`.

3. **Sekrety:** Settings → Secrets and variables → Actions
   - *Secrets*: `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`
   - *Variables* (opcjonalnie): `CAPITAL` (np. 99400), `RISK_PCT` (0.5), `LEVERAGE` (3)

4. **Test:** zakładka Actions → „Bot swing - sygnaly” → **Run workflow**.
   W logu ma się pojawić `sygnałów: X | błędów: 0`.
   - Jeśli są błędy dostępu do OKX (serwery GitHuba stoją w USA, a OKX może je blokować),
     daj znać. Przeniesiemy bota na VPS w UE albo na Twój komputer.

5. **Backtest** (w Codespaces):
   ```
   pip install requests
   python swing_bot.py --backtest    # ok. 80 dni historii 1H
   python swing_bot.py --dry         # bieżący skan bez wysyłania
   ```

## Jak wygląda sygnał

```
🔴 SHORT HYPE — sygnał SWING
Kontrakt: HYPE-USD_UM_XPERP-310801

Wejście:  76.950  (limit lub rynek)
SL:       77.880
TP1:      75.555  (1,5R — zamknij 30%, SL przesuń na wejście)
Trailing: reszta (70%) — aktywacja 75.555, callback 2.4%
          (bez górnego TP — trailing zamknie pozycję)

Ilość:    5 340 kontraktów
Wartość:  ~41 100 USD | marża izolowana 3x ≈ 13 700 USDC
Ryzyko:   ~497 USDC (0.5% kapitału)

Trend 4H (EMA50/200): spadkowy | RSI 1H 44 | ATR 1H 0.62
Ważny do: 24.10 14:00 — potem nie wchodź
Przycisk: Sprzedaj (krótkie), zaznacz TP/SL (TP1 na 30% ilości), potem dodaj Trailing stop
```

Po otrzymaniu sygnału w aplikacji OKX (**Handel demo** → Futures → para):
1. Izolowane 3x → Z limitem → cena i ilość z sygnału → zaznacz TP/SL: SL z sygnału, TP1 → przycisk z sygnału.
2. Gdy pozycja wejdzie: **Trailing stop** na 70% ilości, cena aktywacji = TP1, callback z sygnału
   (opcja „Tylko redukcja”). TP1 ustaw na 30% ilości (zaawansowane TP/SL).
3. Po TP1 przesuń SL na cenę wejścia.

---

## Scalping z Claude (gdy siedzisz przy komputerze)

Otwórz OKX demo w Chrome, panel Claude i wklej:

```
Sesja scalpingu na OKX demo. Ja składam zlecenia, Ty dajesz sygnały.
1. Przeskanuj kontrakty X-Perp dostępne na demo (nagłówek x-simulated-trading:1).
   Wybierz 1–2 z najlepszym stosunkiem ruchu 1m do spreadu.
2. Na 1m/5m/15m policz EMA9/21/50, RSI, ATR, VWAP i ściany w order booku.
3. Podaj setup tylko z trendem 15m: wejście z limitem, SL min. 1 ATR(15m),
   TP1/TP2, R:R min. 1:1,5, ilość kontraktów przy ryzyku 0,25% kapitału, ważność w minutach.
4. Gdy napiszę „sprawdź”, zweryfikuj formularz zrzutem ekranu, zanim kliknę.
5. Gdy napiszę „stan”, podaj P&L i co zrobić z SL i TP.
Zasady: maks. 2 stratne transakcje z rzędu, potem przerwa. Stop czasowy 15 min.
```
