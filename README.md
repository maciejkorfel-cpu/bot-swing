# Bot swing: sygnały OKX na Telegram

> **Zegar:** cron GitHuba bywa uruchamiany tylko co kilka godzin, dlatego bota co 5 min
> uruchamia darmowy [cron-job.org](https://cron-job.org) (zapytanie POST do GitHub API,
> `workflow_dispatch` z `cicho=true`). Cron GitHuba co 15 min zostaje jako zapas.

Bez kluczy API OKX bot tylko wysyła sygnały; z kluczami konta demo sam otwiera pozycje. Co 5 minut skanuje 13 par
z kontraktem X-Perp na demo (BTC, ETH, XRP, HYPE, DOGE, AVAX, LINK, SUI, BCH, DOT, FIL, NEAR, UNI).

## Strategia v2 (od 11.2026): wybicie kanału 4H, tylko LONG, stop na zamknięciu świecy

| Krok | Warunek |
|---|---|
| Wejście LONG | Zamknięcie 4H > najwyższe high z 40 poprzednich świec **i** zamknięcie > EMA200 (4H) tej pary |
| SL startowy | 2 x ATR(14) 4H od wejścia = 1R (od tego liczona wielkość) |
| Wyjście | Po każdej świecy 4H SL = max(SL, najniższe low z 20 świec), tylko w górę. Gdy świeca 4H rynku **spot** ZAMKNIE się pod SL → zamknięcie rynkowe. Bez TP. |
| SL awaryjny | Na giełdzie demo, 2R od wejścia (ochrona przed krachem między przebiegami) |
| Wielkość | RISK_PCT (0,5%) kapitału na transakcję, marża izolowana 3x |
| Limit | maks. 6 pozycji w jedną stronę (MAX_SAME_DIR), jedna pozycja na parę |

### Dlaczego zmiana (analiza 19.11.2026)

1. **Konto demo psuło stopy.** Cena kontraktów X-Perp w demo odbiega od rynku. Na żywo bot zamknął
   13 z 14 transakcji na minusie, kilka po -2R (np. short ETH 9.11: na demo -2,04R, a na rynku spot ta sama
   pozycja była nadal otwarta na +4,9R). Backtest starej strategii na tym samym okresie dał +19R.
   Dlatego stop jest teraz liczony na zamknięciu świecy spot, a na giełdzie stoi tylko szeroki SL awaryjny.
2. **Stara strategia słabła poza próbą.** Test: parametry dobrane na 2019-2023, sprawdzone na 2024-11.2026.
   Short i trailing 5,5 ATR traciły przewagę; long z filtrem EMA200 i wyjściem po kanale 20 świec trzymał się
   w każdym wariancie parametrów (30-60 świec wejścia, stop 1,5-3 ATR).

### Wyniki backtestu (OKX spot 4H, 2019 - 11.2026, 20 par, prowizje+poślizg 0,08%/stronę, funding 0,01%/8h, ryzyko 0,5%, procent składany)

| Wariant | Okres | Transakcje | Trafność | PF | Wynik | Obsunięcie |
|---|---|---|---|---|---|---|
| **v2 (obecna)** | 2019-23 (dobór) | 666 | 35% | 2,45 | +1636% | 31% |
| **v2 (obecna)** | **2024-11.2026 (poza próbą)** | 466 | 30% | **1,71** | **+155%** | 26% |
| **v2 (obecna)** | ostatnie 12 mies. (bessa) | 174 | 25% | 1,17 | +5% | 23% |
| v1 (stara, L/S 20, trailing 5,5 ATR) | 2024-11.2026 | 889 | 25% | 1,27 | +79% | 36% |
| v1 (stara) | ostatnie 12 mies. | 335 | 22% | 1,04 | -2% | 36% |

Na samych 13 parach bota (poza próbą): PF 2,00, +187%, obsunięcie 15%. Na 7 parach spoza listy: PF 1,35, +39%.

**Czego się spodziewać:** 65-75% transakcji to małe straty (~1R), zysk robią nieliczne długie trendy.
W bessie (cena pod EMA200) bot prawie nie handluje, tak ma być. W 2022 strategia traciła (ok. -10%),
w ostatnich 12 miesiącach wyszła na niewielki plus. Wyniki z przeszłości niczego nie gwarantują.

Stare pozycje otwarte przed zmianą (short, strategia v1) dalej mają trailing 5,5 ATR, aż się zamkną.

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
   python swing_bot.py --backtest    # strategia v2, ok. 500 dni historii 4H na parę
   python swing_bot.py --dry         # bieżący skan bez wysyłania
   ```

## Jak wygląda sygnał

```
🟢 LONG ETH — wybicie kanału 40 świec 4H (nad EMA200)
Kontrakt: ETH-USD_UM_XPERP-310801

Wejście:  3 001.00  (rynek)
SL:       2 900.00  (2 x ATR 4H) — zamknij, gdy świeca 4H ZAMKNIE się pod nim
SL awaryjny na giełdzie: 2 799.00
Wyjście:  po każdej świecy 4H podnieś SL do najniższego low z 20 świec (tylko w górę), bez TP

Ilość:    49 kontraktów
Wartość:  ~14 700 USD | marża izolowana 3x ≈ 4 900 USDC
Ryzyko:   ~495 USDC (0.5% kapitału)
```

Ręcznie w aplikacji OKX (**Handel demo** → Futures → para):
1. Izolowane 3x → Rynek → ilość z sygnału → zaznacz SL = **SL awaryjny** z sygnału.
2. Po każdej zamkniętej świecy 4H: jeśli zamknięcie jest pod SL z sygnału, zamknij pozycję.
   W przeciwnym razie podnieś SL do najniższego low z 20 ostatnich świec 4H (tylko w górę).

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
