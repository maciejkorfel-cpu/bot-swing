"""Bot DEMO OKX (paper trading): S2 (swing BTC/ETH 4H) + carry C2. Zastępuje starą strategię Donchian 20 4H.

Logika w pakiecie demo/ (opis: README.md). Tylko konto DEMO — twarda blokada w demo/okx.py.
  python swing_bot.py          → jeden przebieg (GitHub Actions; wymaga OKX_DEMO=1 i kluczy demo w zmiennych środowiskowych)
  python swing_bot.py --dry    → bez kluczy i bez zleceń: bieżący stan reguł S2 i C2 z danych publicznych
"""
import sys
from demo.bot import run, dry_report

if __name__ == "__main__":
    if "--dry" in sys.argv:
        dry_report()
    else:
        run()
